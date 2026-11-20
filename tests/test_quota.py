"""Kontingent-Logik: Googles Fehler entscheiden, keine eigene Vorab-Blockade (Auftrag vom 20.11.2026)."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

import httplib2
import pytest
from googleapiclient.errors import HttpError

from shorts import sample_data, upload
from shorts.config import Settings, load_channels
from shorts.db import Database, Status
from shorts.upload import (active_block, http_error_reason, quota_reset, run_uploads, upload_status,
                           upload_with_retry)


def utc(*a: int) -> datetime:
    return datetime(*a, tzinfo=timezone.utc)


def http_error(status: int, reason: str) -> HttpError:
    resp = httplib2.Response({"status": status})
    content = json.dumps({"error": {"code": status, "message": reason,
                                    "errors": [{"reason": reason, "domain": "youtube"}]}}).encode()
    return HttpError(resp, content)


@pytest.fixture
def quiet() -> Settings:
    return replace(Settings(), telegram_bot_token=None, telegram_chat_id=None)


def _approved(db: Database, n: int) -> int:
    tid = db.add_topic("de", f"https://bund.de/{n}", "Q", f"T{n}")
    db.transition(tid, Status.SKRIPT, script_json=json.dumps(sample_data.SAMPLE_SCRIPT))
    db.transition(tid, Status.VIDEO, video_path="/tmp/x.mp4")
    db.transition(tid, Status.WARTET)
    db.transition(tid, Status.FREIGEGEBEN)
    return tid


def fake_uploads(monkeypatch, outcomes: list) -> list:
    """Jeder Aufruf von upload_video liefert das nächste Ergebnis (Video-ID oder Exception)."""
    calls = []
    seq = iter(outcomes)

    def _upload(yt, path, body):
        calls.append(body)
        item = next(seq)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(upload, "youtube_client", lambda ch: object())
    monkeypatch.setattr(upload, "upload_video", _upload)
    return calls


# --- Fehler klassifizieren ------------------------------------------------------------------------
def test_reason_aus_httperror() -> None:
    assert http_error_reason(http_error(403, "quotaExceeded")) == ("quotaExceeded", 403)
    assert http_error_reason(http_error(400, "uploadLimitExceeded")) == ("uploadLimitExceeded", 400)


def test_quota_exceeded_stoppt_ohne_retry(monkeypatch) -> None:
    calls = fake_uploads(monkeypatch, [http_error(403, "quotaExceeded"), "nie"])
    with pytest.raises(upload.UploadStopped) as exc:
        upload_with_retry(object(), "x", {}, sleep=lambda s: None)
    assert exc.value.reason == "quotaExceeded" and len(calls) == 1


def test_upload_limit_stoppt_mit_eigener_meldung(monkeypatch) -> None:
    fake_uploads(monkeypatch, [http_error(400, "uploadLimitExceeded")])
    with pytest.raises(upload.UploadStopped) as exc:
        upload_with_retry(object(), "x", {}, sleep=lambda s: None)
    assert "Upload-Limit des Kanals" in str(exc.value)


def test_rate_limit_wird_mit_backoff_wiederholt(monkeypatch) -> None:
    waits = []
    calls = fake_uploads(monkeypatch, [http_error(403, "rateLimitExceeded"), http_error(403, "userRateLimitExceeded"),
                                       "VID"])
    assert upload_with_retry(object(), "x", {}, sleep=waits.append) == "VID"
    assert len(calls) == 3 and waits == [2, 4]  # exponentieller Backoff


def test_500er_nach_drei_versuchen_aufgeben(monkeypatch) -> None:
    calls = fake_uploads(monkeypatch, [http_error(500, "backendError")] * 3)
    with pytest.raises(HttpError):
        upload_with_retry(object(), "x", {}, sleep=lambda s: None)
    assert len(calls) == 3


# --- Reset-Berechnung (sommerzeitfest) ------------------------------------------------------------
def test_reset_winter_sommer_und_usa_schon_umgestellt() -> None:
    def berlin(dt):
        return quota_reset(dt).strftime("%Y-%m-%d %H:%M")
    assert berlin(utc(2026, 11, 20, 19)) == "2026-11-21 09:00"   # beide Winterzeit
    assert berlin(utc(2026, 7, 6, 12)) == "2026-07-07 09:00"     # beide Sommerzeit
    assert berlin(utc(2026, 3, 10, 12)) == "2026-03-11 08:00"    # USA schon Sommerzeit (8.3.), EU erst 29.3.
    assert berlin(utc(2026, 10, 28, 12)) == "2026-10-29 08:00"   # EU schon Winterzeit (25.10.), USA erst 1.11.


# --- Batch-Verhalten ------------------------------------------------------------------------------
def test_zweites_video_quota_exceeded(quiet, monkeypatch, capsys) -> None:
    db = Database(":memory:")
    a, b, c = (_approved(db, i) for i in range(3))
    fake_uploads(monkeypatch, ["VID1", http_error(403, "quotaExceeded"), "nie"])
    now = utc(2026, 11, 20, 19, 15)
    rc = run_uploads(quiet, db, load_channels()["de"], now=now, sleep=lambda s: None)
    out = capsys.readouterr().out
    assert rc == 0
    assert db.get(a).status == Status.HOCHGELADEN and db.get(a).youtube_id == "VID1"
    for tid in (b, c):  # kein halber Zustand: unverändert freigegeben, kein Termin reserviert
        t = db.get(tid)
        assert t.status == Status.FREIGEGEBEN and t.publish_at is None
    assert "ERGEBNIS [warn]: 1 von 3 Videos hochgeladen – Google meldet quotaExceeded, Reset 09:00" in out
    assert active_block(db, now)["reason"] == "quotaExceeded"


def test_anderer_fehler_setzt_status_fehler(quiet, monkeypatch, capsys) -> None:
    db = Database(":memory:")
    a, b = _approved(db, 1), _approved(db, 2)
    fake_uploads(monkeypatch, [http_error(400, "invalidTitle"), "VID2"])
    run_uploads(quiet, db, load_channels()["de"], now=utc(2026, 11, 20, 8), sleep=lambda s: None)
    assert db.get(a).status == Status.FEHLER and "HttpError" in db.get(a).note
    assert db.get(b).status == Status.HOCHGELADEN  # Batch läuft weiter
    assert "1 von 2 Videos hochgeladen – 1 Fehler" in capsys.readouterr().out


def test_keine_vorab_blockade_trotz_eigener_zaehlung(quiet, monkeypatch) -> None:
    db = Database(":memory:")
    tid = _approved(db, 1)
    now = utc(2026, 11, 20, 19)
    for _ in range(150):
        db.count_upload(upload.quota_day(now))  # eigene Zählung weit über 100
    fake_uploads(monkeypatch, ["VID"])
    run_uploads(quiet, db, load_channels()["de"], now=now)
    assert db.get(tid).status == Status.HOCHGELADEN


def test_timer_lauf_pausiert_bis_reset_und_meldet_einmal(quiet, monkeypatch, capsys) -> None:
    db = Database(":memory:")
    tid = _approved(db, 1)
    now = utc(2026, 11, 20, 19)
    upload.set_block(db, "quotaExceeded", now)
    calls = fake_uploads(monkeypatch, ["VID"])
    sent = []
    monkeypatch.setattr(upload, "notify", lambda s, text: sent.append(text))
    run_uploads(quiet, db, load_channels()["de"], now=now, scheduled=True)
    run_uploads(quiet, db, load_channels()["de"], now=now, scheduled=True)
    assert calls == [] and db.get(tid).status == Status.FREIGEGEBEN
    assert len(sent) == 1 and "Übersprungen" in capsys.readouterr().out
    # Manuell (nicht scheduled) wird trotzdem versucht
    run_uploads(quiet, db, load_channels()["de"], now=now)
    assert db.get(tid).status == Status.HOCHGELADEN
    # Nach dem Reset ist die Sperre weg
    assert active_block(db, utc(2026, 11, 21, 8, 1)) is None


def test_status_anzeige(quiet) -> None:
    db = Database(":memory:")
    now = utc(2026, 11, 20, 19, 15)
    db.count_upload(upload.quota_day(now))
    info = upload_status(quiet, db, now)
    assert (info["today"], info["limit"], info["reset_time"], info["block"]) == (1, 100, "09:00", None)
    upload.set_block(db, "quotaExceeded", now)
    assert upload_status(quiet, db, now)["block"]["text"] == \
        "Google hat das Kontingent um 20:15 gemeldet (quotaExceeded), Reset um 09:00."
