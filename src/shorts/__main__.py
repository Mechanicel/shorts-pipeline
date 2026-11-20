"""CLI-Einstieg:  python -m shorts <befehl>  (im Container: docker compose run --rm job <befehl>)."""
from __future__ import annotations

import argparse
import shutil
import logging
import sys
import traceback
from dataclasses import replace

from .config import get_channel, load_settings
from .db import Database, Status
from .logging_setup import setup_logging
from .notify import notify

log = logging.getLogger("shorts")


def _db(settings, dry_run: bool) -> Database:
    # Dry-Run nutzt eine eigene Datenbank, damit echte Daten unberührt bleiben
    name = "dryrun.sqlite3" if dry_run else "pipeline.sqlite3"
    return Database(settings.data_dir / name)


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
    db.transition(t.id, back, note=None)
    print(f"Thema {t.id} → {back}")
    return 0


def cmd_bot(args, settings) -> int:
    from .review_bot import run_bot

    run_bot(settings)
    return 0


def cmd_upload(args, settings) -> int:
    from .upload import run_uploads

    return run_uploads(settings, _db(settings, False), get_channel(args.channel), dry_run=args.dry_run)


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
    add("upload", cmd_upload, "freigegebene Videos zu YouTube hochladen", dry=True)
    add("bot", cmd_bot, "Telegram-Review-Bot (dauerhaft)", channel=False)
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
