from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from shorts import sample_data
from shorts.config import load_channels
from shorts.db import Database, Status
from shorts.review_bot import format_review, parse_callback
from shorts.upload import build_metadata, next_free_slot, parse_slot, quota_day, run_uploads

SLOTS = ["Mon 17:00", "Wed 17:00", "Sat 11:00"]


def utc(*a: int) -> datetime:
    return datetime(*a, tzinfo=timezone.utc)


# ------------------------------------------------------------------ Slots
def test_parse_slot() -> None:
    assert parse_slot("Mon 17:00") == (0, 17, 0)
    assert parse_slot("sunday 9:05") == (6, 9, 5)


def test_naechster_slot_beruecksichtigt_zeitzone() -> None:
    # Fr 20.11.2026 12:00 UTC → nächster Slot Sa 21.11. 11:00 Berlin (MEZ, UTC+1) = 10:00 UTC
    assert next_free_slot(SLOTS, set(), utc(2026, 11, 20, 12)) == "2026-11-21T10:00:00Z"


def test_belegte_slots_werden_uebersprungen() -> None:
    taken = {"2026-11-21T10:00:00Z", "2026-11-23T16:00:00Z"}
    assert next_free_slot(SLOTS, taken, utc(2026, 11, 20, 12)) == "2026-11-25T16:00:00Z"  # Mi 17:00


def test_slot_braucht_vorlauf() -> None:
    # Mo 23.11. 15:30 UTC = 16:30 Berlin → Slot 17:00 liegt < 2 h entfernt → Mittwoch
    assert next_free_slot(SLOTS, set(), utc(2026, 11, 23, 15, 30)) == "2026-11-25T16:00:00Z"


def test_sommerzeit() -> None:
    # Mo 6.7.2026 17:00 MESZ = 15:00 UTC
    assert next_free_slot(["Mon 17:00"], set(), utc(2026, 7, 6, 8)) == "2026-07-06T15:00:00Z"


def test_quota_tag_ist_pacific_time() -> None:
    assert quota_day(utc(2026, 11, 20, 7, 59)) == "2026-11-19"  # 23:59 PST
    assert quota_day(utc(2026, 11, 20, 8, 0)) == "2026-11-20"


# ------------------------------------------------------------------ Upload-Planung
def _approved_topic(db: Database, n: int) -> int:
    tid = db.add_topic("de", f"https://a.de/{n}", "Q", f"T{n}")
    db.transition(tid, Status.SKRIPT, script_json=json.dumps(sample_data.SAMPLE_SCRIPT))
    db.transition(tid, Status.VIDEO, video_path="/tmp/x.mp4")
    db.transition(tid, Status.WARTET)
    db.transition(tid, Status.FREIGEGEBEN)
    return tid


def test_metadaten() -> None:
    db = Database(":memory:")
    topic = db.get(_approved_topic(db, 1))
    ch = load_channels()["de"]
    body = build_metadata(topic, ch, "2026-11-21T10:00:00Z")
    assert body["status"] == {"privacyStatus": "private", "publishAt": "2026-11-21T10:00:00Z",
                              "selfDeclaredMadeForKids": False,
                              "containsSyntheticMedia": ch.contains_synthetic_media}
    assert "Quelle: https://a.de/1" in body["snippet"]["description"]
    assert len(body["snippet"]["title"]) <= 100
    assert body["snippet"]["description"].count("#shorts") == 1  # Hashtags dedupliziert


def test_dry_run_upload_respektiert_quota(capsys: pytest.CaptureFixture[str]) -> None:
    from dataclasses import replace
    from shorts.config import Settings

    db = Database(":memory:")
    for i in range(4):
        _approved_topic(db, i)
    now = utc(2026, 11, 20, 12)
    db.add_quota(quota_day(now), 6000)  # noch 4000 frei → 2 Uploads
    settings = replace(Settings(), youtube_max_uploads_per_day=5, youtube_daily_quota=10000)
    run_uploads(settings, db, load_channels()["de"], dry_run=True, now=now)
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 2
    slots = [line.split("→ ")[1].split()[0] for line in out]
    assert len(set(slots)) == 2  # unterschiedliche Slots
    assert all(t.status == Status.FREIGEGEBEN for t in db.by_status(Status.FREIGEGEBEN))  # nichts verändert


# ------------------------------------------------------------------ Review-Bot
def test_parse_callback() -> None:
    assert parse_callback("ok:12") == (Status.FREIGEGEBEN, 12)
    assert parse_callback("no:3") == (Status.ABGELEHNT, 3)
    assert parse_callback("ok:abc") is None
    assert parse_callback("drop:1") is None


def test_review_text_enthaelt_quelle_belege_und_warnung() -> None:
    db = Database(":memory:")
    tid = _approved_topic(db, 7)
    db.update_fields(tid, note="Zitate nicht wörtlich im Quelltext: X")
    text = format_review(db.get(tid))
    assert "https://a.de/7" in text
    assert "Bisher lag die Frist bei 14 Tagen." in text
    assert "⚠️ Zitate nicht wörtlich" in text
    assert len(text) <= 4000
