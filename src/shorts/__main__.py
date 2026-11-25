"""CLI-Einstieg:  python -m shorts <befehl>  (im Container: docker compose run --rm job <befehl>)."""
from __future__ import annotations

import argparse
import fcntl
import logging
import shutil
import sys
import traceback
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Iterator

from .config import get_channel, load_settings
from .db import Database, Status
from .logging_setup import setup_logging
from .notify import notify

log = logging.getLogger("shorts")


def _db(settings, dry_run: bool) -> Database:
    # Dry-Run nutzt eine eigene Datenbank, damit echte Daten unberührt bleiben
    name = "dryrun.sqlite3" if dry_run else "pipeline.sqlite3"
    return Database(settings.data_dir / name)


@contextmanager
def job_lock(settings, name: str = "job") -> Iterator[bool]:
    """Exklusive Sperre über Container-Grenzen (data/ ist gemountet): verhindert, dass
    Timer- und Handläufe gleichzeitig dieselben Themen bearbeiten. Liefert False, wenn belegt."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with open(settings.data_dir / f"{name}.lock", "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _locked(fn):
    """Decorator: echter Lauf nur mit Sperre; ein zweiter gleichzeitiger Lauf beendet sich sauber."""
    def wrapper(args, settings) -> int:
        if getattr(args, "dry_run", False):
            return fn(args, settings)
        with job_lock(settings) as acquired:
            if not acquired:
                log.warning("Ein anderer Pipeline-/Upload-Lauf ist aktiv – breche ab, ohne etwas zu tun.")
                return 0
            return fn(args, settings)
    return wrapper


@_locked
def cmd_pipeline(args, settings) -> int:
    from .pipeline import Pipeline

    if args.dry_run:
        settings = replace(settings, output_dir=settings.output_dir / "dryrun")
    channel = get_channel(args.channel)
    if args.dry_run:  # Test-DB und Test-Ausgabe bei jedem Lauf frisch (nur data/dryrun*, output/dryrun)
        shutil.rmtree(settings.output_dir, ignore_errors=True)
        for f in settings.data_dir.glob("dryrun.sqlite3*"):
            f.unlink()
    db = _db(settings, args.dry_run)
    if args.limit:  # z. B. Testlauf mit genau einem Video; bis zu 4 Claude-Aufrufe je Video
        channel = replace(channel, max_videos_per_run=args.limit,
                          max_new_topics_per_run=min(channel.max_new_topics_per_run, args.limit * 4))
    pipe = Pipeline(settings, channel, db, dry_run=args.dry_run)
    pipe.style_pick = args.style if args.style in ("v2", "v3") else None
    report = pipe.run()
    for v in report.videos:  # relativer Pfad statt Container-Pfad; "#ID" verlinkt das Webinterface
        rel = v.relative_to(settings.root) if v.is_relative_to(settings.root) else v
        print(f"Video #{int(v.parent.name)} fertig: {rel}")
    return 1 if report.errors and not report.rendered else 0


def cmd_collect(args, settings) -> int:
    from .sources import collect_topics

    new, errors = collect_topics(_db(settings, False), get_channel(args.channel))
    print(f"{new} neue Themen, {len(errors)} Quellenfehler")
    for e in errors:
        print("  -", e)
    return 0


def cmd_status(args, settings) -> int:
    db = _db(settings, args.dry_run)
    for status, n in sorted(db.counts(args.channel).items()):
        print(f"{status:22} {n}")
    return 0


def cmd_retry(args, settings) -> int:
    db = _db(settings, False)
    t = db.get(args.topic_id)
    if t.status != Status.FEHLER:
        print(f"Thema {t.id} ist im Status {t.status}, nicht 'fehler'.")
        return 1
    back = {"neu": Status.NEU, "skript": Status.SKRIPT, "video": Status.SKRIPT,
            "freigegeben": Status.FREIGEGEBEN}.get(t.prev_status or "neu", Status.NEU)
    if t.prev_status == Status.VIDEO and t.video_path and Path(t.video_path).exists():
        back = Status.VIDEO  # fertiges Video wiederverwenden, statt TTS/Render erneut zu bezahlen
    db.transition(t.id, back, note=None)
    print(f"Thema {t.id} → {back}")
    return 0


def cmd_regenerate(args, settings) -> int:
    """Thema (wartend/abgelehnt/fehler) mit neuem Skript + Video neu einreihen."""
    from .actions import regenerate

    try:
        regenerate(settings, _db(settings, False), args.topic_id, style_pick=args.style)
    except ValueError as exc:
        print(exc)
        return 1
    print(f"Thema {args.topic_id} → neu (wird beim nächsten Pipeline-Lauf bevorzugt neu erzeugt)")
    return 0


def cmd_refresh_dates(args, settings) -> int:
    """Trägt fehlende Quelldaten (z. B. Verbraucherzentrale) für offene Themen von der Artikelseite nach."""
    from .sources import fetch_article

    db = _db(settings, False)
    open_states = [Status.NEU, Status.SKRIPT, Status.VIDEO, Status.WARTET, Status.FREIGEGEBEN]
    todo = [t for st in open_states for t in db.by_status(st, args.channel) if not t.published_at]
    for t in todo:
        try:
            date_str = fetch_article(t.url).date
        except Exception as exc:
            print(f"#{t.id}: {type(exc).__name__}")
            continue
        db.update_fields(t.id, published_at=date_str)
        print(f"#{t.id} {t.status}: {date_str or 'kein Datum gefunden'}  {t.title[:60]}")
    return 0


def cmd_auth_check(args, settings) -> int:
    """Prüft die YouTube-Token-Datei (Refresh, ohne Quota-Verbrauch)."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    from .upload import SCOPES

    path = get_channel(args.channel).youtube_token
    if not path.exists():
        print(f"{path} fehlt – auth_youtube.py auf dem Laptop ausführen und per scp kopieren.")
        return 1
    # Scopes aus der Datei übernehmen – sonst würde das Zurückschreiben das Analytics-Recht aus der Datei streichen
    creds = Credentials.from_authorized_user_file(str(path))
    if SCOPES[0] not in (creds.scopes or []):
        print("Token ohne Upload-Recht – auth_youtube.py erneut ausführen.")
        return 1
    if not creds.refresh_token:
        print("Token ohne refresh_token – auth_youtube.py erneut ausführen.")
        return 1
    creds.refresh(Request())  # erzwungener Refresh: prüft Client + Refresh-Token gegen Google
    path.write_text(creds.to_json(), encoding="utf-8")
    path.chmod(0o600)
    print(f"Token für Kanal {args.channel} gültig (Refresh ok, Scopes: {', '.join(creds.scopes or [])}).")
    return 0


def cmd_check_public(args, settings) -> int:
    from .deadline import planned_stock, stock_warning, utc_now
    from .publiccheck import run_check

    db = _db(settings, False)
    rc = run_check(db, args.channel, args.id)
    # Vorrats-Prüfung (läuft täglich mit der Sichtbarkeitsprüfung): < 3 Tage geplant → Telegram
    stock = planned_stock(db.planned_publish_times(args.channel), utc_now())
    if warning := stock_warning(stock):
        log.warning(warning)
        notify(settings, f"[{args.channel}] {warning}")
    else:
        log.info("Vorrat: %d Videos an %d Tagen geplant", stock.videos, stock.days)
    _fetch_analytics(settings, db, args.channel)  # täglich mit der Sichtbarkeitsprüfung; Fehler stoppen nichts
    return rc


def _fetch_analytics(settings, db, channel_key: str) -> dict | None:
    from .analytics import MissingScope, analytics_client, fetch_stats

    try:
        client = analytics_client(get_channel(channel_key))
        counts = fetch_stats(db, channel_key, lambda **p: client.reports().query(**p).execute())
        log.info("Analytics: %d Videos, %d Zeilen, %d× 72-h-Wert neu", counts["videos"], counts["rows"], counts["h72"])
        return counts
    except MissingScope as exc:
        log.warning("Analytics übersprungen: %s", exc)
    except Exception as exc:
        log.warning("Analytics fehlgeschlagen: %s", type(exc).__name__)
    return None


def cmd_analytics(args, settings) -> int:
    from .result import ERROR, OK, report

    counts = _fetch_analytics(settings, _db(settings, False), args.channel)
    if counts is None:
        report(ERROR, "Analytics nicht abrufbar – Details im Log (Token mit Analytics-Recht nötig?)")
        return 1
    report(OK, f"Analytics: {counts['videos']} Videos aktualisiert, {counts['h72']}× Aufrufe nach 72 h neu")
    return 0


def cmd_research(args, settings) -> int:
    from datetime import date

    from .research import (MIN_REVIEWS_FOR_ANALYSIS, OUR_FORMAT, ResearchAnalyst, ResearchResult, ResearchStore,
                           TopicClassifier, YouTubeAPI, classify_outliers, load_research_config, pattern_stats,
                           render_report, run_research, telegram_summary, write_csv)
    from .result import ERROR, OK, WARN, report

    cfg = load_research_config()
    store = ResearchStore(_db(settings, False))
    out_dir = settings.root / "docs" / "research"
    today = date.today().isoformat()

    if args.analyze_reviews:  # manuelle Sichtung → Muster (Knopf im Webinterface)
        reviews = store.reviewed()
        if len(reviews) < MIN_REVIEWS_FOR_ANALYSIS:
            report(WARN, f"Erst {len(reviews)} von {MIN_REVIEWS_FOR_ANALYSIS} Videos gesichtet.")
            return 0
        analyst = ResearchAnalyst(settings, cfg)
        text = analyst.compare_reviews(reviews, OUR_FORMAT)
        cost = sum(x or 0 for x in analyst.spent)
        path = out_dir / f"review-patterns-{today}.md"
        out_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# Muster aus der manuellen Sichtung {today}\n\n{len(reviews)} gesichtete Ausreißer.\n\n"
                        f"{text}\n", encoding="utf-8")
        _db(settings, False).set_setting("research_review_analysis",
                                         {"date": today, "n": len(reviews), "text": text, "path": str(path)})
        print(text)
        report(OK, f"Muster aus {len(reviews)} gesichteten Videos ausgewertet ({cost * settings.eur_per_usd:.2f} €)"
               .replace(".", ",", 1))
        return 0

    if args.reanalyze:  # vorhandene Daten neu filtern und auswerten, ohne search-Kontingent
        removed = store.drop_small_median(cfg.min_channel_median)
        result = ResearchResult([], 0, 0, 0, 0, note=f"Neuauswertung der gespeicherten Ausreißer ohne neue Suche; "
                                f"{removed} entfernt (Kanal-Median < {cfg.min_channel_median:.0f}).")
        pool = store.outliers()
        run_id = None
    else:
        if not settings.yt_api_key:
            report(ERROR, "YT_API_KEY fehlt in .env (API-Schlüssel für die YouTube Data API v3).")
            return 1
        terms = args.term or cfg.search_terms[: args.terms or None]
        run_id = store.start_run(len(terms))
        result = run_research(YouTubeAPI(settings.yt_api_key), store, cfg, terms)
        found = {c.video_id for c in result.outliers}
        pool = [v for v in store.outliers() if v["video_id"] in found]

    cost = 0.0
    if settings.anthropic_api_key and pool:  # Themen-Vorauswahl: Politik/Satire/Unterhaltung aussortieren
        classifier = TopicClassifier(settings)
        try:
            kept, dropped = classify_outliers(store, classifier)
            log.info("Themen-Vorauswahl: %d behalten, %d aussortiert", kept, dropped)
        except Exception as exc:
            log.warning("Themen-Vorauswahl fehlgeschlagen (%s) – alle Ausreißer werden ausgewertet", type(exc).__name__)
        cost += sum(x or 0 for x in classifier.spent)
        relevance = {v["video_id"]: v for v in store.outliers()}
        pool = [relevance.get(v["video_id"], v) for v in pool]
    excluded = [v for v in pool if v.get("relevant") == "nein"]
    top = [v for v in pool if v.get("relevant") != "nein"][: cfg.top_n]
    stats = pattern_stats(top)
    analysis = None
    if top and not args.no_analysis and settings.anthropic_api_key:
        analyst = ResearchAnalyst(settings, cfg)
        try:
            ch = get_channel(args.channel)
            analysis = analyst.analyse(top, stats, ch.name, [src.name for src in ch.sources])
        except Exception as exc:  # Auswertung ist Zugabe – Daten und Bericht gibt es trotzdem
            log.warning("Claude-Auswertung fehlgeschlagen: %s", type(exc).__name__)
        cost += sum(x or 0 for x in analyst.spent)
    path = out_dir / f"outliers-{today}.md"
    out_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(result, top, stats, analysis, cost, settings.eur_per_usd, today, excluded,
                                  cfg.min_channel_median), encoding="utf-8")
    write_csv(top, path.with_suffix(".csv"))
    if run_id:
        store.finish_run(run_id, search_calls=result.search_calls, units=result.units, candidates=result.candidates,
                         outliers=len(result.outliers), quota_stop=result.quota_stop, report_path=str(path))
    notify(settings, telegram_summary(result, top, path, len(excluded)))
    print(path.read_text(encoding="utf-8"))
    summary = (f"{len(top)} passende Ausreißer ({len(excluded)} Politik/Satire aussortiert), "
               f"{result.search_calls} search-Aufrufe")
    if result.quota_stop:
        report(WARN, f"{summary} – abgebrochen: YouTube meldet {result.quota_stop}")
    else:
        report(OK if top else WARN, summary)
    return 0


def cmd_preview(args, settings) -> int:
    """Beide Fassungen (v2 und v3) eines Artikels nur als Vorschau: eigene In-Memory-DB, eigener Ausgabeordner,
    nichts wird hochgeladen oder in die echte Datenbank geschrieben. Ergebnis per Telegram."""
    import re as _re

    import httpx

    from .pipeline import Pipeline, RunReport
    from .result import ERROR, OK, report
    from .sources import fetch_article

    ch = get_channel(args.channel)
    article = fetch_article(args.url)
    if not article.text:
        report(ERROR, "Kein Artikeltext gefunden.")
        return 1
    slug = _re.sub(r"[^a-z0-9]+", "-", args.url.lower().rstrip("/").split("/")[-1])[:40]
    out_root = settings.output_dir / "preview" / slug
    results = {}
    for style in (args.only,) if args.only else ("v2", "v3"):
        st = replace(settings, style_mode=style, output_dir=out_root / style)
        mem = Database(":memory:")
        tid = mem.add_topic(ch.key, args.url, args.source or "Vorschau", args.title or args.url,
                            published_at=article.date)
        mem.update_fields(tid, source_text=article.text, triage="ok: Vorschau")
        pipe = Pipeline(st, replace(ch, max_videos_per_run=1, max_new_topics_per_run=1), mem)
        pipe.skip_age_check = True  # Vorschau: auch ältere Übersichtsseiten erlaubt
        report_ = RunReport()
        try:
            pipe._script_one(mem.get(tid), report_)
        except Exception as exc:
            results[style] = (None, f"{style}: Skript fehlgeschlagen ({type(exc).__name__}: {exc})"[:900])
            continue
        t = mem.get(tid)
        if t.status != Status.SKRIPT:
            results[style] = (None, f"{style}: kein Skript ({t.note})")
            continue
        try:
            path = pipe._render_one(t)
        except Exception as exc:
            results[style] = (None, f"{style}: Render fehlgeschlagen ({type(exc).__name__}: {exc})")
            continue
        s = mem.get(tid).script or {}
        results[style] = (path, f"{style} · {s.get('style')} · {s.get('title')}\n{s.get('narration')}"
                          + (f"\n⚠️ {mem.get(tid).note}" if mem.get(tid).note else ""))
        print(f"{style}: {path}")
    if settings.telegram_bot_token and settings.telegram_chat_id:
        api = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
        for style, (path, text) in results.items():
            if path is None:
                httpx.post(f"{api}/sendMessage", data={"chat_id": settings.telegram_chat_id,
                                                       "text": f"VORSCHAU {text}"}, timeout=30)
                continue
            with open(path, "rb") as fh:
                httpx.post(f"{api}/sendVideo", data={"chat_id": settings.telegram_chat_id,
                                                     "caption": f"VORSCHAU (wird nicht hochgeladen)\n{text}"[:1000],
                                                     "supports_streaming": "true"},
                           files={"video": (f"vorschau_{style}.mp4", fh, "video/mp4")}, timeout=180)
            board = path.parent / "board.png"
            if board.exists():
                with open(board, "rb") as fh:
                    httpx.post(f"{api}/sendPhoto", data={"chat_id": settings.telegram_chat_id,
                                                         "caption": "Tafel v3 in voller Auflösung"},
                               files={"photo": ("tafel.png", fh, "image/png")}, timeout=60)
    ok = [s for s, (p, _) in results.items() if p]
    report(OK if len(ok) == len(results) else ERROR, f"Vorschau: {', '.join(ok) or 'keine'} erstellt ({out_root})")
    return 0 if ok else 1


def cmd_web(args, settings) -> int:
    from .web.app import run_web

    run_web(settings, port=args.port)
    return 0


def cmd_web_reset_password(args, settings) -> int:
    db = _db(settings, False)
    db.delete_setting("password_hash")
    db.delete_setting("session_secret")
    print("Passwort zurückgesetzt – beim nächsten Aufruf des Webinterface neues Passwort festlegen.")
    return 0


def cmd_bot(args, settings) -> int:
    from .review_bot import run_bot

    run_bot(settings)
    return 0


@_locked
def cmd_upload(args, settings) -> int:
    db, channel = _db(settings, False), get_channel(args.channel)
    from .web.schedule import load_schedule
    channel = replace(channel, publish_slots=load_schedule(db, channel)["publish_slots"])  # Termine aus dem Web
    if args.mode:  # Kommandozeile schlägt .env (z. B. für einen gezielten API-Test)
        settings = replace(settings, upload_mode=args.mode)
    log.info("Upload-Modus: %s", settings.upload_mode)
    if settings.upload_mode == "api":
        from datetime import datetime, timezone

        from .upload import parse_publish_at, run_uploads
        if args.sofort and args.publish_at:
            raise ValueError("--sofort und --publish-at schließen sich aus")
        at = parse_publish_at(args.publish_at, datetime.now(timezone.utc)) if args.publish_at else None
        return run_uploads(settings, db, channel, dry_run=args.dry_run, publish_at=at, immediate=args.sofort,
                           only=args.only, scheduled=args.scheduled)
    if args.publish_at or args.sofort:
        raise ValueError("--publish-at/--sofort gibt es nur im API-Modus (--mode api)")
    if settings.upload_mode == "manual":
        from .manual import run_manual
        return run_manual(settings, db, channel, dry_run=args.dry_run)
    raise ValueError(f"UPLOAD_MODE '{settings.upload_mode}' unbekannt (manual | api)")


def main(argv: list[str] | None = None) -> int:
    settings = load_settings()
    setup_logging(settings.log_level)

    p = argparse.ArgumentParser(prog="shorts", description="YouTube-Shorts-Pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, fn, help_: str, channel: bool = True, dry: bool = False):
        sp = sub.add_parser(name, help=help_)
        if channel:
            sp.add_argument("--channel", default=settings.default_channel)
        if dry:
            sp.add_argument("--dry-run", action="store_true", help="ohne API-Keys, mit Beispieldaten")
        sp.set_defaults(fn=fn)
        return sp

    sp = add("pipeline", cmd_pipeline, "Quellen abrufen, Skripte schreiben, Videos rendern", dry=True)
    sp.add_argument("--limit", type=int, default=0, help="max. Anzahl Videos in diesem Lauf")
    sp.add_argument("--style", choices=["auto", "v2", "v3"], default="auto",
                    help="Stil für alle Videos dieses Laufs (auto = STYLE_MODE aus .env)")
    add("collect", cmd_collect, "nur Quellen abrufen")
    add("status", cmd_status, "Anzahl Themen je Status", dry=True)
    sp = add("upload", cmd_upload, "fällige Videos: Upload-Paket an Telegram (manual) bzw. YouTube-Upload (api)",
             dry=True)
    sp.add_argument("--mode", choices=["manual", "api"], help="überschreibt UPLOAD_MODE aus .env")
    sp.add_argument("--publish-at", metavar="'JJJJ-MM-TT HH:MM'",
                    help="Wunschtermin (Berliner Zeit) für das erste freigegebene Video, z. B. für einen Test")
    sp.add_argument("--only", type=int, metavar="ID", help="nur dieses freigegebene Thema hochladen")
    sp.add_argument("--scheduled", action="store_true",
                    help="Timer-Lauf: bei von Google gemeldeter Sperre bis zum Reset überspringen")
    sp.add_argument("--sofort", action="store_true",
                    help="alle Videos dieses Laufs sofort öffentlich hochladen (ohne Termin)")
    sp = add("check-public", cmd_check_public, "prüft von außen, ob hochgeladene Videos öffentlich sind")
    sp.add_argument("--id", action="append", default=[], help="zusätzliche YouTube-Video-ID")
    sp = add("research", cmd_research, "Ausreißer-Recherche: erfolgreiche Shorts kleiner Kanäle (YouTube Data API)")
    sp.add_argument("--terms", type=int, default=0, help="nur die ersten N Suchbegriffe aus research.yaml")
    sp.add_argument("--term", action="append", default=[], help="eigener Suchbegriff (mehrfach möglich)")
    sp.add_argument("--no-analysis", action="store_true", help="ohne Claude-Auswertung")
    sp.add_argument("--analyze-reviews", action="store_true", help="manuell gesichtete Videos auswerten")
    sp.add_argument("--reanalyze", action="store_true",
                    help="gespeicherte Ausreißer neu filtern und auswerten (ohne search-Kontingent)")
    add("analytics", cmd_analytics, "YouTube-Statistiken pro Video abrufen (Analytics API)")
    sp = add("preview", cmd_preview, "v2 und v3 eines Artikels als Vorschau (kein Upload, keine echte DB)")
    sp.add_argument("url")
    sp.add_argument("--title", default="")
    sp.add_argument("--source", default="")
    sp.add_argument("--only", choices=["v2", "v3"], help="nur eine Fassung erzeugen")
    add("bot", cmd_bot, "Telegram-Review-Bot (dauerhaft)", channel=False)
    sp = add("web", cmd_web, "Webinterface + Zeitplan (dauerhaft)", channel=False)
    sp.add_argument("--port", type=int, default=8080)
    add("web-reset-password", cmd_web_reset_password, "Webinterface-Passwort zurücksetzen", channel=False)
    sp = add("regenerate", cmd_regenerate, "Thema mit neuem Skript + Video neu einreihen", channel=False)
    sp.add_argument("topic_id", type=int)
    sp.add_argument("--style", choices=["v2", "v3"], help="beim Neuerzeugen diesen Stil verwenden")
    add("auth-check", cmd_auth_check, "YouTube-Token prüfen (ohne Quota)")
    add("refresh-dates", cmd_refresh_dates, "fehlende Quelldaten offener Themen nachtragen")
    sp = add("retry", cmd_retry, "Thema im Status 'fehler' erneut einreihen", channel=False)
    sp.add_argument("topic_id", type=int)

    args = p.parse_args(argv)
    try:
        return args.fn(args, settings)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        log.exception("Befehl '%s' abgebrochen", args.cmd)
        if not getattr(args, "dry_run", False):
            notify(settings, f"❌ shorts {args.cmd} abgebrochen: {type(exc).__name__}: {exc}\n"
                             + traceback.format_exc()[-1500:])
        return 1


if __name__ == "__main__":
    sys.exit(main())
