from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from shorts import sample_data
from shorts.config import Settings, load_channels
from shorts.db import Database, Status
from shorts.manual import is_due, package_messages, run_manual
from shorts.review_bot import parse_callback


class FakeSender:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    def send_document(self, path: Path, caption: str) -> int:
        if self.fail:
            raise ConnectionError("Telegram down")
        self.calls.append(("doc", path.name))
        return 1

    def send_message(self, text: str, reply_markup: dict | None = None) -> int:
        self.calls.append(("msg", text))
        return 100 + len(self.calls)


def utc(*a: int) -> datetime:
    return datetime(*a, tzinfo=timezone.utc)


def _approved(db: Database, n: int, video: Path) -> int:
    tid = db.add_topic("de", f"https://bund.de/{n}", "Q", f"T{n}")
    db.transition(tid, Status.SKRIPT, script_json=json.dumps(sample_data.SAMPLE_SCRIPT))
    db.transition(tid, Status.VIDEO, video_path=str(video))
    db.transition(tid, Status.WARTET)
    db.transition(tid, Status.FREIGEGEBEN)
    db.update_fields(tid, background_credit="Video: Pixabay https://pixabay.com/videos/id-1/")
    return tid


def test_faelligkeit_nach_berliner_datum() -> None:
    assert is_due("2026-11-23T16:00:00Z", utc(2026, 11, 23, 8, 30))      # Mo 09:30 → Termin Mo 17:00
    assert not is_due("2026-11-24T16:00:00Z", utc(2026, 11, 23, 8, 30))  # Di-Termin noch nicht
    assert is_due("2026-11-22T10:00:00Z", utc(2026, 11, 23, 8, 30))      # verpasst → sofort


def test_paket_inhalt() -> None:
    db = Database(":memory:")
    topic = db.get(_approved(db, 1, Path("/x.mp4")))
    m = package_messages(topic, load_channels()["de"], "2026-11-23T16:00:00Z")
    assert m["title"] == sample_data.SAMPLE_SCRIPT["title"]
    assert "Quelle: https://bund.de/1" in m["description"]
    assert "Video: Pixabay https://pixabay.com/videos/id-1/" in m["description"]
    assert m["tags"].startswith("shorts, test") and "#" not in m["tags"]
    assert "Mo 23.11. 17:00" in m["final"]
    assert m["button"]["inline_keyboard"][0][0]["callback_data"] == f"up:{topic.id}"


def test_taeglicher_lauf_schickt_nur_faellige_pakete(tmp_path: Path) -> None:
    video = tmp_path / "short.mp4"
    video.write_bytes(b"mp4")
    db = Database(":memory:")
    a, b = _approved(db, 1, video), _approved(db, 2, video)
    sender = FakeSender()
    now = utc(2026, 11, 23, 8, 30)  # Mo 09:30 Berlin
    run_manual(Settings(), db, load_channels()["de"], now=now, sender=sender)
    # a → Mo 17:00 (heute, fällig), b → Di 17:00 (morgen)
    assert db.get(a).status == Status.PAKET and db.get(a).publish_at == "2026-11-23T16:00:00Z"
    assert db.get(b).status == Status.FREIGEGEBEN and db.get(b).publish_at == "2026-11-24T16:00:00Z"
    kinds = [k for k, _ in sender.calls]
    assert kinds == ["doc", "msg", "msg", "msg", "msg"]  # MP4, Titel, Beschreibung, Tags, Termin+Button
    assert db.get(a).telegram_message_id == 105
    # Am Dienstag kommt b, a wird nicht erneut geschickt; Termin von b bleibt stabil
    sender2 = FakeSender()
    run_manual(Settings(), db, load_channels()["de"], now=utc(2026, 11, 24, 8, 30), sender=sender2)
    assert db.get(b).status == Status.PAKET and len(sender2.calls) == 5


def test_versandfehler_laesst_video_freigegeben(tmp_path: Path) -> None:
    video = tmp_path / "short.mp4"
    video.write_bytes(b"mp4")
    db = Database(":memory:")
    a = _approved(db, 1, video)
    settings = replace(Settings(), telegram_bot_token=None, telegram_chat_id=None)  # notify nur ins Log
    rc = run_manual(settings, db, load_channels()["de"], now=utc(2026, 11, 23, 8, 30), sender=FakeSender(fail=True))
    assert rc == 1 and db.get(a).status == Status.FREIGEGEBEN


def test_button_hochgeladen() -> None:
    assert parse_callback("up:7") == (Status.HOCHGELADEN, 7)
    db = Database(":memory:")
    tid = _approved(db, 1, Path("/x.mp4"))
    db.transition(tid, Status.PAKET)
    assert db.transition(tid, Status.HOCHGELADEN).status == Status.HOCHGELADEN
