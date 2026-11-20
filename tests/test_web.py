from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shorts import sample_data
from shorts.config import Settings, load_channels
from shorts.db import Database, Status
from shorts.upload import BERLIN
from shorts.web import auth
from shorts.web.app import create_app
from shorts.web.jobs import JobRunner
from shorts.web.schedule import default_schedule, load_schedule, trigger_for, validate_schedule

PW = "richtig-langes-passwort"


@pytest.fixture
def env(tmp_path: Path):
    settings = replace(Settings(), data_dir=tmp_path / "data", output_dir=tmp_path / "output",
                       telegram_bot_token=None, telegram_chat_id=None)
    settings.data_dir.mkdir()
    settings.output_dir.mkdir()
    client = TestClient(create_app(settings, start_scheduler=False))
    return settings, client


def _setup(client: TestClient) -> None:
    r = client.post("/setup", data={"password": PW, "password2": PW}, follow_redirects=False)
    assert r.status_code == 303 and auth.COOKIE in r.cookies


# ------------------------------------------------------------------ Passwort & Sitzung
def test_passwort_hash() -> None:
    h = auth.hash_password(PW)
    assert h.startswith("scrypt$") and PW not in h
    assert auth.verify_password(PW, h) and not auth.verify_password("falsch", h)


def test_cookie_laeuft_ab(tmp_path: Path) -> None:
    db = Database(tmp_path / "x.sqlite3")
    auth.set_password(db, PW)
    c = auth.make_cookie(db, now=1000.0)
    assert auth.valid_cookie(db, c, now=1001.0)
    assert not auth.valid_cookie(db, c, now=1000.0 + auth.SESSION_SECONDS + 1)
    assert not auth.valid_cookie(db, c.replace(".", ".x"), now=1001.0)  # manipulierte Signatur


def test_passwortwechsel_macht_alte_sitzungen_ungueltig(tmp_path: Path) -> None:
    db = Database(tmp_path / "x.sqlite3")
    auth.set_password(db, PW)
    old = auth.make_cookie(db)
    auth.set_password(db, PW + "-neu")
    assert not auth.valid_cookie(db, old)


def test_erster_start_fuehrt_zur_einrichtung(env) -> None:
    _, client = env
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    r = client.post("/setup", data={"password": "kurz", "password2": "kurz"})
    assert "mindestens" in r.text
    _setup(client)
    assert client.get("/").status_code == 200
    # Einrichtung ist danach gesperrt (kein Überschreiben des Passworts ohne Anmeldung)
    r = TestClient(client.app).post("/setup", data={"password": "x" * 12, "password2": "x" * 12},
                                    follow_redirects=False)
    assert r.headers["location"] == "/login"


def test_ohne_anmeldung_kein_zugriff(env) -> None:
    _, client = env
    _setup(client)
    anon = TestClient(client.app)
    for path in ("/", "/settings", "/media/1.mp4", "/jobs/1"):
        r = anon.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login", path
    r = anon.post("/actions/new-video", follow_redirects=False)
    assert r.headers["location"] == "/login"


def test_login_und_sperre_nach_fehlversuchen(env) -> None:
    _, client = env
    _setup(client)
    anon = TestClient(client.app)
    for _ in range(auth.MAX_FAILS):
        assert "Falsches Passwort" in anon.post("/login", data={"password": "falsch"}).text
    assert "Fehlversuche" in anon.post("/login", data={"password": PW}).text  # auch richtig → gesperrt
    fresh = TestClient(create_app(client.app.state.runner.s, start_scheduler=False))
    r = fresh.post("/login", data={"password": PW}, follow_redirects=False)
    assert r.status_code == 303 and auth.COOKIE in r.cookies


# ------------------------------------------------------------------ Zeitplan
def test_zeitplan_validierung_und_speichern(env) -> None:
    settings, client = env
    _setup(client)
    r = client.post("/settings", data={"pipeline_enabled": "on", "day_tue": "on", "day_fri": "on",
                                       "pipeline_time": "07:15", "pipeline_limit": "3",
                                       "upload_time": "10:00", "publish_slots": "Tue 18:00\nFri 18:00"})
    assert "Zeitplan gespeichert" in r.text
    s = load_schedule(Database(settings.db_path), load_channels()["de"])
    assert s["pipeline"] == {"enabled": True, "days": ["tue", "fri"], "time": "07:15", "limit": 3}
    assert s["upload"]["enabled"] is False and s["publish_slots"] == ["Tue 18:00", "Fri 18:00"]
    r = client.post("/settings", data={"pipeline_time": "25:00", "pipeline_limit": "3", "upload_time": "10:00",
                                       "publish_slots": "Funday 9:00"})
    assert "Wochentag" in r.text and "HH:MM" in r.text and "ungültig" in r.text


def test_standard_zeitplan_ist_aus() -> None:
    s = default_schedule(load_channels()["de"])
    assert not s["pipeline"]["enabled"] and not s["upload"]["enabled"]
    assert validate_schedule(s) == []


def test_cron_trigger_berlin() -> None:
    t = trigger_for(["sun"], "06:00")
    nxt = t.get_next_fire_time(None, datetime(2026, 11, 20, 12, 0, tzinfo=BERLIN))
    assert nxt.isoformat().startswith("2026-11-22T06:00:00+01:00")


# ------------------------------------------------------------------ Aktionen & Medien
def _waiting_topic(settings: Settings, video: Path) -> int:
    db = Database(settings.db_path)
    tid = db.add_topic("de", "https://bund.de/x", "Q", "Thema")
    db.transition(tid, Status.SKRIPT, script_json=json.dumps(sample_data.SAMPLE_SCRIPT))
    db.transition(tid, Status.VIDEO, video_path=str(video))
    db.transition(tid, Status.WARTET)
    return tid


def test_freigeben_im_web(env) -> None:
    settings, client = env
    _setup(client)
    video = settings.output_dir / "short.mp4"
    video.write_bytes(b"mp4")
    tid = _waiting_topic(settings, video)
    page = client.get("/").text
    assert f"#{tid}" in page and "Freigeben" in page
    assert client.get(f"/media/{tid}.mp4").content == b"mp4"
    r = client.post(f"/topics/{tid}/approve")
    assert "freigegeben" in r.text
    assert Database(settings.db_path).get(tid).status == Status.FREIGEGEBEN


def test_video_ausserhalb_output_wird_nicht_ausgeliefert(env, tmp_path: Path) -> None:
    settings, client = env
    _setup(client)
    secret = tmp_path / "geheim.mp4"
    secret.write_bytes(b"nope")
    tid = _waiting_topic(settings, secret)
    assert client.get(f"/media/{tid}.mp4").status_code == 404


def test_wunschtermin_in_vergangenheit_abgelehnt(env) -> None:
    _, client = env
    _setup(client)
    r = client.post("/actions/upload", data={"when": "wunsch", "at": "2020-01-01T10:00"})
    assert "15 Minuten" in r.text


# ------------------------------------------------------------------ Job-Runner
def test_jobrunner_protokolliert_und_verhindert_parallele_jobs(env) -> None:
    settings, _ = env
    runner = JobRunner(settings, lambda: Database(settings.db_path))
    job_id = runner.launch("status", ["status"], "web")
    assert job_id is not None
    assert runner.launch("status", ["status"], "web") is None  # läuft noch → übersprungen
    for _ in range(100):
        if runner.running() is None:
            break
        time.sleep(0.1)
    job = Database(settings.db_path).get_job(job_id)
    assert job["returncode"] == 0 and Path(job["log_path"]).exists()
