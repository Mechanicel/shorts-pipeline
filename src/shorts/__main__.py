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
    report = Pipeline(settings, channel, db, dry_run=args.dry_run).run()
    for v in report.videos:
        print(f"Video: {v}")
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
    db = _db(settings, False)
    t = db.get(args.topic_id)
    if t.status == Status.WARTET:
        db.transition(t.id, Status.ABGELEHNT, note="abgelehnt zur Neuerzeugung")
    elif t.status not in (Status.ABGELEHNT, Status.FEHLER):
        print(f"Thema {t.id} ist im Status {t.status} – regenerate geht nur für wartend/abgelehnt/fehler.")
        return 1
    old_msg = t.telegram_message_id
    db.regenerate(t.id)
    if old_msg and settings.telegram_bot_token and settings.telegram_chat_id:
        _retire_review_message(settings, old_msg, t.id)
    print(f"Thema {t.id} → neu (wird beim nächsten Pipeline-Lauf bevorzugt neu erzeugt)")
    return 0


def _retire_review_message(settings, message_id: int, topic_id: int) -> None:
    """Buttons der alten Review-Nachricht entfernen, damit niemand die veraltete Fassung freigibt."""
    import httpx

    base = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
    try:
        httpx.post(f"{base}/editMessageReplyMarkup", timeout=15, json={
            "chat_id": settings.telegram_chat_id, "message_id": message_id,
            "reply_markup": {"inline_keyboard": []}})
        httpx.post(f"{base}/sendMessage", timeout=15, json={
            "chat_id": settings.telegram_chat_id, "reply_to_message_id": message_id,
            "text": f"❌ #{topic_id} abgelehnt – wird beim nächsten Lauf neu erzeugt (neues Skript, Faktenkarten)."})
    except Exception as exc:
        log.warning("Alte Review-Nachricht nicht aktualisiert: %s", type(exc).__name__)


def cmd_auth_check(args, settings) -> int:
    """Prüft die YouTube-Token-Datei (Refresh, ohne Quota-Verbrauch)."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    from .upload import SCOPES

    path = get_channel(args.channel).youtube_token
    if not path.exists():
        print(f"{path} fehlt – auth_youtube.py auf dem Laptop ausführen und per scp kopieren.")
        return 1
    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if not creds.refresh_token:
        print("Token ohne refresh_token – auth_youtube.py erneut ausführen.")
        return 1
    creds.refresh(Request())  # erzwungener Refresh: prüft Client + Refresh-Token gegen Google
    path.write_text(creds.to_json(), encoding="utf-8")
    path.chmod(0o600)
    print(f"Token für Kanal {args.channel} gültig (Refresh ok, Scopes: {', '.join(creds.scopes or [])}).")
    return 0


def cmd_check_public(args, settings) -> int:
    from .publiccheck import run_check

    return run_check(_db(settings, False), args.channel, args.id)


def cmd_bot(args, settings) -> int:
    from .review_bot import run_bot

    run_bot(settings)
    return 0


@_locked
def cmd_upload(args, settings) -> int:
    db, channel = _db(settings, False), get_channel(args.channel)
    if args.mode:  # Kommandozeile schlägt .env (z. B. für einen gezielten API-Test)
        settings = replace(settings, upload_mode=args.mode)
    log.info("Upload-Modus: %s", settings.upload_mode)
    if settings.upload_mode == "api":
        from datetime import datetime, timezone

        from .upload import parse_publish_at, run_uploads
        at = parse_publish_at(args.publish_at, datetime.now(timezone.utc)) if args.publish_at else None
        return run_uploads(settings, db, channel, dry_run=args.dry_run, publish_at=at)
    if args.publish_at:
        raise ValueError("--publish-at gibt es nur im API-Modus (--mode api)")
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
    add("collect", cmd_collect, "nur Quellen abrufen")
    add("status", cmd_status, "Anzahl Themen je Status", dry=True)
    sp = add("upload", cmd_upload, "fällige Videos: Upload-Paket an Telegram (manual) bzw. YouTube-Upload (api)",
             dry=True)
    sp.add_argument("--mode", choices=["manual", "api"], help="überschreibt UPLOAD_MODE aus .env")
    sp.add_argument("--publish-at", metavar="'JJJJ-MM-TT HH:MM'",
                    help="Wunschtermin (Berliner Zeit) für das erste freigegebene Video, z. B. für einen Test")
    sp = add("check-public", cmd_check_public, "prüft von außen, ob hochgeladene Videos öffentlich sind")
    sp.add_argument("--id", action="append", default=[], help="zusätzliche YouTube-Video-ID")
    add("bot", cmd_bot, "Telegram-Review-Bot (dauerhaft)", channel=False)
    sp = add("regenerate", cmd_regenerate, "Thema mit neuem Skript + Video neu einreihen", channel=False)
    sp.add_argument("topic_id", type=int)
    add("auth-check", cmd_auth_check, "YouTube-Token prüfen (ohne Quota)")
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
