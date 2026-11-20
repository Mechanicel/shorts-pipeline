"""Abnahmekriterien P0 aus dem Testbericht vom 20.11.2026."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from shorts import sample_data, upload
from shorts.config import Settings, load_channels
from shorts.db import Database, Status
from shorts.pipeline import RunReport, source_age_problem
from shorts.publiccheck import Visibility, parse_publish_date, run_check
from shorts.upload import quota_reset, quota_status, run_uploads
from shorts.web.jobs import read_result


def utc(*a: int) -> datetime:
    return datetime(*a, tzinfo=timezone.utc)


def _approved(db: Database, n: int) -> int:
    tid = db.add_topic("de", f"https://bund.de/{n}", "Q", f"T{n}")
    db.transition(tid, Status.SKRIPT, script_json=json.dumps(sample_data.SAMPLE_SCRIPT))
    db.transition(tid, Status.VIDEO, video_path="/tmp/x.mp4")
    db.transition(tid, Status.WARTET)
    db.transition(tid, Status.FREIGEGEBEN)
    return tid


@pytest.fixture
def quiet() -> Settings:
    return replace(Settings(), telegram_bot_token=None, telegram_chat_id=None,
                   youtube_max_uploads_per_day=5, youtube_daily_quota=10000)


# --- Upload-Ergebnis ehrlich melden ---------------------------------------------------------------
def test_kontingent_leer_meldet_0_von_1(quiet, capsys) -> None:
    db = Database(":memory:")
    _approved(db, 1)
    now = utc(2026, 11, 20, 19, 15)  # 20:15 Berlin
    db.add_quota(upload.quota_day(now), 9600)
    rc = run_uploads(quiet, db, load_channels()["de"], now=now)
    out = capsys.readouterr().out
    assert rc == 0  # Befund, kein Absturz
    assert "ERGEBNIS [warn]: 0 von 1 Video hochgeladen – Kontingent (9600/10000), wieder ab Sa 21.11. 09:00" in out
    assert db.by_status(Status.FREIGEGEBEN)[0].publish_at is None  # nichts reserviert


def test_kontingent_reset_und_status(quiet) -> None:
    assert quota_reset(utc(2026, 11, 20, 19, 0)).strftime("%d.%m. %H:%M") == "21.11. 09:00"
    assert quota_reset(utc(2026, 7, 6, 12, 0)).strftime("%d.%m. %H:%M") == "07.07. 09:00"  # PDT/MESZ
    db = Database(":memory:")
    db.add_quota(upload.quota_day(utc(2026, 11, 20, 19)), 9600)
    q = quota_status(quiet, db, utc(2026, 11, 20, 19))
    assert (q["used"], q["free"], q["possible"]) == (9600, 400, 0)


def test_teilweise_hochgeladen_ist_warnung(quiet, capsys, monkeypatch) -> None:
    db = Database(":memory:")
    for i in range(3):
        _approved(db, i)
    now = utc(2026, 11, 20, 8, 30)
    db.add_quota(upload.quota_day(now), 6400)  # noch 2 Uploads möglich
    monkeypatch.setattr(upload, "youtube_client", lambda ch: object())
    ids = iter(["A", "B", "C"])
    monkeypatch.setattr(upload, "upload_video", lambda yt, path, body: next(ids))
    run_uploads(replace(quiet, youtube_max_uploads_per_day=10), db, load_channels()["de"], now=now)
    assert "ERGEBNIS [warn]: 2 von 3 Videos hochgeladen – Kontingent (6400/10000)" in capsys.readouterr().out


def test_tageslimit_wird_als_grund_genannt(quiet, capsys, monkeypatch) -> None:
    db = Database(":memory:")
    for i in range(3):
        _approved(db, i)
    now = utc(2026, 11, 20, 8, 30)
    db.add_quota(upload.quota_day(now), 6400)  # 4 von max. 5 Uploads verbraucht
    monkeypatch.setattr(upload, "youtube_client", lambda ch: object())
    monkeypatch.setattr(upload, "upload_video", lambda yt, path, body: "X")
    run_uploads(quiet, db, load_channels()["de"], now=now)
    assert "1 von 3 Videos hochgeladen – Tageslimit (5 Uploads)" in capsys.readouterr().out


# --- Termin-Vergabe ohne Kollision ----------------------------------------------------------------
def test_drei_videos_drei_verschiedene_termine(quiet, monkeypatch) -> None:
    db = Database(":memory:")
    for i in range(3):
        _approved(db, i)
    monkeypatch.setattr(upload, "youtube_client", lambda ch: object())
    ids = iter(["A", "B", "C"])
    monkeypatch.setattr(upload, "upload_video", lambda yt, path, body: next(ids))
    run_uploads(quiet, db, load_channels()["de"], now=utc(2026, 11, 20, 8, 30))
    slots = [t.publish_at for t in db.by_status(Status.HOCHGELADEN)]
    assert len(slots) == 3 and len(set(slots)) == 3


def test_sofort_speichert_keinen_schein_termin(quiet, monkeypatch) -> None:
    db = Database(":memory:")
    tid = _approved(db, 1)
    monkeypatch.setattr(upload, "youtube_client", lambda ch: object())
    monkeypatch.setattr(upload, "upload_video", lambda yt, path, body: "VID")
    run_uploads(quiet, db, load_channels()["de"], now=utc(2026, 11, 20, 18, 30), immediate=True)
    t = db.get(tid)
    assert t.publish_at is None and t.yt_published_at == "2026-11-20T18:30:00Z"
    assert "2026-11-20T18:30:00Z" not in db.taken_publish_slots("de")


def test_geloeschte_videos_geben_termin_frei() -> None:
    db = Database(":memory:")
    tid = _approved(db, 1)
    db.update_fields(tid, publish_at="2026-11-21T10:00:00Z")
    assert "2026-11-21T10:00:00Z" in db.taken_publish_slots("de")
    db.update_fields(tid, yt_visibility="gelöscht")
    assert "2026-11-21T10:00:00Z" not in db.taken_publish_slots("de")


# --- Thema-Datum erzwingen ------------------------------------------------------------------------
def test_quellen_alter() -> None:
    today = utc(2026, 11, 20, 12)
    assert source_age_problem("2026-11-20T10:43:09+00:00", today) is None
    assert source_age_problem("2026-10-30", today) is None                       # 21 Tage: noch ok
    assert source_age_problem("2026-10-22", today).startswith("zu alt (Quelle vom 22.10.2026, 29 Tage)")
    assert source_age_problem("2026-08-24", today).startswith("zu alt")          # #102
    assert source_age_problem(None, today) == "kein Datum (Ratgeber ohne Datum)"


def test_pipeline_ergebnis() -> None:
    assert RunReport(rendered=1).result()[0] == "ok"
    st, text = RunReport(rendered=0, discarded=2, discard_reasons=["zu alt", "zu alt"]).result()
    assert st == "warn" and "2× zu alt" in text and "keine neuen Videos" in text
    assert RunReport(rendered=0, errors=["x"]).result()[0] == "error"


# --- YouTube-Status in die Datenbank --------------------------------------------------------------
def test_check_public_schreibt_status_in_db(capsys) -> None:
    db = Database(":memory:")
    a, b = _approved(db, 1), _approved(db, 2)
    db.transition(a, Status.HOCHGELADEN, youtube_id="PUB", publish_at="2026-11-21T10:00:00Z")
    db.transition(b, Status.HOCHGELADEN, youtube_id="DEL", publish_at="2026-11-20T16:00:00Z")
    fake = {"PUB": Visibility("PUB", 200, "OK", "", "2026-11-20T12:42:23Z"),
            "DEL": Visibility("DEL", 403, "ERROR", "Video nicht verfügbar")}
    rc = run_check(db, "de", checker=lambda vid: fake[vid])
    out = capsys.readouterr().out
    assert rc == 0 and "ERGEBNIS [warn]" in out and "gelöscht" in out
    assert (db.get(a).yt_visibility, db.get(a).yt_published_at) == ("öffentlich", "2026-11-20T12:42:23Z")
    assert db.get(b).yt_visibility == "gelöscht" and db.get(b).yt_checked_at


def test_publish_date_parser() -> None:
    html = '..."publishDate":"2026-11-20T04:42:23-08:00"...'
    assert parse_publish_date(html) == "2026-11-20T12:42:23Z"
    assert parse_publish_date("nichts") is None


# --- Exit-Codes trennen ---------------------------------------------------------------------------
def test_job_ergebnis(tmp_path: Path) -> None:
    f = tmp_path / "r.json"
    f.write_text(json.dumps({"status": "warn", "summary": "0 von 1 hochgeladen"}))
    assert read_result(f, 0) == ("warn", "0 von 1 hochgeladen")
    assert read_result(f, 1)[0] == "error"                      # Exit-Code ≠ 0 = technischer Fehler
    assert read_result(tmp_path / "fehlt.json", 0) == ("ok", "fertig")
