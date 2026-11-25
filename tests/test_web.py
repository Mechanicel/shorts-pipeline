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
                                       "upload_time": "10:00", "slot_day_0": "tue", "slot_time_0": "18:00",
                                       "slot_day_1": "fri", "slot_time_1": "18:00", "slot_day_2": "", "slot_time_2": ""})
    assert "Zeitplan gespeichert" in r.text
    s = load_schedule(Database(settings.db_path), load_channels()["de"])
    assert s["pipeline"] == {"enabled": True, "days": ["tue", "fri"], "time": "07:15", "limit": 3}
    assert s["upload"]["enabled"] is False and s["publish_slots"] == ["Tue 18:00", "Fri 18:00"]
    r = client.post("/settings", data={"pipeline_time": "25:00", "pipeline_limit": "3", "upload_time": "10:00"})
    assert "Wochentag" in r.text and "HH:MM" in r.text and "Veröffentlichungstermin" in r.text


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
    assert '<span class="count" aria-label="1 warten">1</span>' in client.get("/").text  # Badge in der Navigation
    page = client.get("/freigabe").text
    assert f"#{tid}" in page and "Freigeben" in page
    assert client.get(f"/media/{tid}.mp4").content == b"mp4"
    r = client.post(f"/topics/{tid}/approve")
    assert "freigegeben" in r.text and r.url.path == "/freigabe"
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


# ------------------------------------------------------------------ P1: Bedienung
def test_metadaten_bearbeiten_und_freigabe_zuruecknehmen(env) -> None:
    settings, client = env
    _setup(client)
    video = settings.output_dir / "short.mp4"
    video.write_bytes(b"mp4")
    tid = _waiting_topic(settings, video)
    r = client.post(f"/topics/{tid}/meta", data={"title": "Neuer  Titel", "description": "Text",
                                                 "hashtags": "#a, b #c"})
    assert "gespeichert" in r.text
    s = Database(settings.db_path).get(tid).script
    assert (s["title"], s["description"], s["hashtags"]) == ("Neuer Titel", "Text", ["#a", "#b", "#c"])
    assert "spitzen Klammern" in client.post(f"/topics/{tid}/meta", data={"title": "<b>x</b>"}).text
    client.post(f"/topics/{tid}/approve")
    client.post(f"/topics/{tid}/unapprove")
    assert Database(settings.db_path).get(tid).status == Status.WARTET


def test_freigabe_direkt_aus_status_video(env) -> None:
    settings, client = env
    _setup(client)
    db = Database(settings.db_path)
    tid = db.add_topic("de", "https://bund.de/v", "Q", "T")
    db.transition(tid, Status.SKRIPT, script_json=json.dumps(sample_data.SAMPLE_SCRIPT))
    db.transition(tid, Status.VIDEO, video_path=str(settings.output_dir / "x.mp4"))
    assert "noch nicht in Telegram" in client.get("/freigabe").text   # sofort sichtbar, kein "In Arbeit" mehr
    client.post(f"/topics/{tid}/approve")
    assert Database(settings.db_path).get(tid).status == Status.FREIGEGEBEN


def test_warn_badges() -> None:
    from shorts.web.app import warning_badges
    from datetime import date
    db = Database(":memory:")
    tid = db.add_topic("de", "https://a.de", "Q", "T", published_at="2026-10-22")
    db.update_fields(tid, source_text="Der Gesetzentwurf sieht vor ...", note="Grafik-Zahlen nicht im Quelltext: 10 €",
                     script_json=json.dumps({"narration": "Du bekommst ab Januar mehr Geld."}))
    badges = warning_badges(db.get(tid), today=date(2026, 11, 20))
    assert "Zahl nicht im Quelltext" in badges and "Quelle 29 Tage alt" in badges
    assert any("Rechtsstatus" in b for b in badges)


def test_job_namen_und_log_links() -> None:
    from shorts.web.app import job_label, linkify_topics
    assert job_label(json.dumps(["upload", "--channel", "de", "--sofort", "--mode", "api"])) == "Upload · sofort"
    assert job_label(json.dumps(["upload", "--channel", "de", "--only", "623"])) == "Upload · nächste Termine · #623"
    assert job_label(json.dumps(["pipeline", "--channel", "de", "--limit", "3"])) == "Videos erstellen (3)"
    assert linkify_topics("Video #102 fertig") == 'Video <a href="/videos/102">#102</a> fertig'


def test_404_als_html_seite(env) -> None:
    _, client = env
    _setup(client)
    r = client.get("/jobs/99999")
    assert r.status_code == 404 and "Fehler 404" in r.text and "Seite oder Eintrag nicht gefunden" in r.text
    assert "Zu Heute" in r.text


def test_einstellungen_warnt_bei_ungleichgewicht(env) -> None:
    _, client = env
    _setup(client)
    r = client.post("/settings", data={"pipeline_enabled": "on", "day_mon": "on", "day_tue": "on",
                                       "pipeline_time": "06:00", "pipeline_limit": "5", "upload_time": "09:30",
                                       "slot_day_0": "mon", "slot_time_0": "17:00"}, follow_redirects=True)
    assert "10 Videos pro Woche" in r.text and "nur 1 Termin –" in r.text
    assert "Nächste freie Termine" in r.text


# ------------------------------------------------------------------ Recherche-Seite
def test_recherche_seite_und_sichtung(env) -> None:
    from shorts.research import ResearchStore, YouTubeAPI, run_research
    from tests.test_research import CFG, NOW, FakeYouTube

    settings, client = env
    _setup(client)
    store = ResearchStore(Database(settings.db_path))
    run_research(YouTubeAPI("k", get=FakeYouTube()), store, CFG, now=NOW)
    r = client.get("/research?sort=views")   # alte Adresse leitet weiter
    assert r.status_code == 200 and r.url.path == "/recherche"
    assert "Achtung: Rente steigt um 50 €" in r.text and "/recherche/v1" in r.text
    assert "versteckt" in r.text and "Muster auswerten" in r.text and "0 von 15 auswertbar" in r.text
    detail = client.get("/recherche/v1").text
    assert "https://www.youtube.com/shorts/v1" in detail and "Sichtung der ersten 3 Sekunden" in detail
    r = client.post("/research/v1/review", data={"face": "nein", "voice": "KI", "first_frame": "Text-Tafel",
                                                 "hook_type": "Zahl", "music": "nein", "notes": "x"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert store.review_count() == 1 and "1 von 15 auswertbar" in client.get("/recherche").text
    assert client.post("/research/..%2Fetc/review", data={}).status_code == 404


# ------------------------------------------------------------------ Mehrere Kanäle
@pytest.fixture
def two_channels(tmp_path: Path):
    import yaml
    raw = yaml.safe_load(Settings().channels_file.read_text(encoding="utf-8"))
    raw["channels"]["en"] = {**raw["channels"]["de"], "name": "What changes for you", "language": "en"}
    cfile = tmp_path / "channels.yaml"
    cfile.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    settings = replace(Settings(), data_dir=tmp_path / "data", output_dir=tmp_path / "output",
                       telegram_bot_token=None, telegram_chat_id=None, channels_file=cfile)
    settings.data_dir.mkdir()
    settings.output_dir.mkdir()
    client = TestClient(create_app(settings, start_scheduler=False))
    _setup(client)
    return settings, client


def test_ein_kanal_ohne_umschalter(env) -> None:
    _, client = env
    _setup(client)
    assert 'action="/kanal"' not in client.get("/").text


def test_kanal_umschalten_filtert_seiten(two_channels) -> None:
    settings, client = two_channels
    video = settings.output_dir / "short.mp4"
    video.write_bytes(b"mp4")
    tid = _waiting_topic(settings, video)  # wartet im Kanal de
    page = client.get("/freigabe").text
    assert 'action="/kanal"' in page and "Nichts wartet auf Freigabe" not in page
    r = client.post("/kanal", data={"key": "en", "next": f"/videos/{tid}"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/videos"  # Detailseite → Liste
    page = client.get("/freigabe").text
    assert "Nichts wartet auf Freigabe" in page
    assert "· 1 warten" in page  # Hinweis auf den anderen Kanal im Umschalter
    assert client.post("/kanal", data={"key": "xx"}).status_code == 404


def test_erstellen_mit_kanal_und_stil(two_channels) -> None:
    _, client = two_channels
    started = []
    client.app.state.runner.launch = lambda kind, args, trigger: started.append(args) or 1
    client.post("/kanal", data={"key": "en"})
    client.post("/actions/new-video", data={"count": "2", "style": "v3"})
    client.post("/actions/new-video", data={"count": "1", "style": "auto"})
    assert started == [["pipeline", "--channel", "en", "--limit", "2", "--style", "v3"],
                       ["pipeline", "--channel", "en", "--limit", "1"]]
    from shorts.web.app import job_label
    assert job_label(json.dumps(started[0])) == "Videos erstellen (2) · v3"


def test_neu_erzeugen_mit_stil_im_web(env) -> None:
    settings, client = env
    _setup(client)
    tid = _waiting_topic(settings, settings.output_dir / "x.mp4")
    assert 'name="style"' in client.get("/freigabe").text
    client.post(f"/topics/{tid}/regenerate", data={"style": "v2"})
    t = Database(settings.db_path).get(tid)
    assert t.status == Status.NEU and t.style_pick == "v2"
