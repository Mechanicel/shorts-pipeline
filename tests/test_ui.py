from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shorts import sample_data
from shorts.config import Settings
from shorts.db import Database, Status
from shorts.publiccheck import Visibility, run_check
from shorts.web.app import create_app
from shorts.web.views import (fmt_dt, fmt_rel, humanize_error, parse_rate, scatter_svg, video_rows)

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
PW = "richtig-langes-passwort"


def _video(db: Database, n: int, publish_at: str | None, visibility: str | None, **extra) -> int:
    tid = db.add_topic("de", f"https://a.de/{n}", "Q", f"Thema {n}")
    db.transition(tid, Status.SKRIPT, script_json=json.dumps({**sample_data.SAMPLE_SCRIPT, "title": f"Titel {n}"}))
    db.transition(tid, Status.VIDEO, video_path="/tmp/x.mp4")
    db.transition(tid, Status.WARTET)
    db.transition(tid, Status.FREIGEGEBEN)
    db.transition(tid, Status.HOCHGELADEN, youtube_id=f"YT{n}", publish_at=publish_at, yt_visibility=visibility, **extra)
    return tid


def _fixture_db() -> tuple[Database, dict[str, int]]:
    db = Database(":memory:")
    ids = {
        "geplant_spaet": _video(db, 1, "2026-11-30T16:00:00Z", "privat"),
        "geplant_frueh": _video(db, 2, "2026-11-26T16:00:00Z", "privat"),
        "public_alt": _video(db, 3, "2026-11-20T16:00:00Z", "öffentlich", yt_published_at="2026-11-20T16:00:05Z"),
        "public_neu": _video(db, 4, "2026-11-24T16:00:00Z", "öffentlich", yt_published_at="2026-11-24T16:00:05Z"),
        "geloescht": _video(db, 5, "2026-11-23T16:00:00Z", "gelöscht", yt_deleted_at="2026-11-23T11:00:00+00:00"),
    }
    rej = db.add_topic("de", "https://a.de/r", "Q", "Abgelehnt")
    db.transition(rej, Status.SKRIPT, script_json=json.dumps(sample_data.SAMPLE_SCRIPT))
    db.transition(rej, Status.VIDEO, video_path="/tmp/x.mp4")
    db.transition(rej, Status.WARTET)
    db.transition(rej, Status.ABGELEHNT)
    ids["abgelehnt"] = rej
    return db, ids


def test_reiter_filtern_und_sortieren() -> None:
    db, ids = _fixture_db()
    geplant = [r.topic.id for r in video_rows(db, "de", "geplant", NOW, {})]
    assert geplant == [ids["geplant_frueh"], ids["geplant_spaet"]]                  # aufsteigend nach Termin
    public = [r.topic.id for r in video_rows(db, "de", "oeffentlich", NOW, {})]
    assert public == [ids["public_neu"], ids["public_alt"]]                          # absteigend
    deleted = video_rows(db, "de", "geloescht", NOW, {})
    assert [r.topic.id for r in deleted] == [ids["geloescht"]]
    assert deleted[0].date_label.startswith("gelöscht (erkannt Mo 23.11.")         # nie „geplant …“
    assert ids["geloescht"] not in geplant + public                                  # gelöscht zählt nirgends mit
    assert [r.topic.id for r in video_rows(db, "de", "abgelehnt", NOW, {})] == [ids["abgelehnt"]]


def test_bekannte_loeschung_warnt_nicht_erneut(capsys) -> None:
    db, ids = _fixture_db()
    states = {"YT1": "privat", "YT2": "privat", "YT3": "öffentlich", "YT4": "öffentlich", "YT5": "gelöscht"}

    def checker(vid):
        return Visibility(vid, 200, {"öffentlich": "OK", "privat": "LOGIN_REQUIRED"}.get(states[vid], "ERROR"),
                          "", None)
    import shorts.publiccheck as pc
    reported = []
    orig = pc.report
    pc.report = lambda status, summary: reported.append((status, summary))
    try:
        run_check(db, "de", checker=checker)
        assert reported[-1][0] == "ok" and "1 bekannt gelöscht" in reported[-1][1]   # bekannte Löschung: keine Warnung
        states["YT4"] = "gelöscht"                                                    # neu gelöscht
        run_check(db, "de", checker=checker)
        assert reported[-1][0] == "warn" and f"#{ids['public_neu']} neu gelöscht" in reported[-1][1]
        assert db.get(ids["public_neu"]).yt_deleted_at
        run_check(db, "de", checker=checker)                                         # am nächsten Tag: bekannt
        assert reported[-1][0] == "ok"
        hist = db.visibility_history(ids["public_neu"])
        assert [h["visibility"] for h in hist] == ["öffentlich", "gelöscht"]          # nur Wechsel protokolliert
    finally:
        pc.report = orig


def test_unerwartet_privat_wird_gemeldet() -> None:
    db = Database(":memory:")
    tid = _video(db, 9, None, None)
    import shorts.publiccheck as pc
    reported = []
    orig = pc.report
    pc.report = lambda status, summary: reported.append((status, summary))
    try:
        run_check(db, "de", checker=lambda vid: Visibility(vid, 401, "LOGIN_REQUIRED", "", None))
    finally:
        pc.report = orig
    assert reported[-1][0] == "warn" and f"#{tid} unerwartet privat" in reported[-1][1]


def test_klartext_fehler_und_datum() -> None:
    e = humanize_error("#6 Skript: RuntimeError: Skript ungültig: Erster Satz spricht nicht mit 'du' an.")
    assert e.text == "Skript abgelehnt: Der erste Satz spricht den Zuschauer nicht direkt an."
    assert "RuntimeError" in e.detail
    assert humanize_error("#7 Render: RuntimeError: Sprechtext zu lang für v3 (13.2s)").text.startswith(
        "Video konnte nicht erzeugt werden: Der Sprechtext")
    assert humanize_error("#8 Skript: ValueError: irgendwas Neues").text == "Skript abgelehnt: irgendwas Neues"
    assert fmt_dt("2026-11-23T16:00:00Z") == "Mo 23.11., 17:00"
    assert fmt_rel("2026-11-27T16:00:00Z", NOW) == "in 2 Tagen" and fmt_rel("2026-11-25T20:00:00Z", NOW) == "heute"


def test_prozentwerte_und_diagramm() -> None:
    assert parse_rate("47,5 %") == 47.5 and parse_rate("") is None
    with pytest.raises(ValueError):
        parse_rate("120")
    svg = scatter_svg([(NOW, 40.0, "v3", "Titel <x>")])
    assert "<circle" in svg and "Titel &lt;x&gt;" in svg and "#d4a017" in svg
    assert "Noch keine Werte" in scatter_svg([])


# ---------------------------------------------------------------- Seiten im Webinterface
@pytest.fixture
def client(tmp_path: Path):
    settings = replace(Settings(), data_dir=tmp_path / "data", output_dir=tmp_path / "output",
                       telegram_bot_token=None, telegram_chat_id=None)
    settings.data_dir.mkdir()
    settings.output_dir.mkdir()
    c = TestClient(create_app(settings, start_scheduler=False))
    c.post("/setup", data={"password": PW, "password2": PW})
    return settings, c


def test_sammelformular_speichert_alle_werte(client) -> None:
    settings, c = client
    db = Database(settings.db_path)
    a = _video(db, 1, "2026-11-20T16:00:00Z", "öffentlich", yt_published_at="2026-11-20T16:00:05Z")
    b = _video(db, 2, "2026-11-21T16:00:00Z", "öffentlich", yt_published_at="2026-11-21T16:00:05Z")
    page = c.get("/statistik").text
    assert f'name="rate_{a}"' in page and f'name="rate_{b}"' in page
    r = c.post("/statistik/studio-werte", data={f"rate_{a}": "41,5", f"rate_{b}": ""})
    assert "1 Wert gespeichert" in r.text
    assert Database(settings.db_path).get(a).yt_viewed_rate == "41.5"
    assert Database(settings.db_path).get(b).yt_viewed_rate is None
    page = c.get("/statistik").text
    assert f'name="rate_{a}"' not in page and f'name="rate_{b}"' in page          # nur Videos ohne Wert
    assert "<circle" in page                                                        # Punkt im Diagramm
    r = c.post("/statistik/studio-werte", data={f"rate_{b}": "130"})
    assert "Ungültige Werte" in r.text


def test_alle_seiten_und_alte_adressen(client) -> None:
    settings, c = client
    db = Database(settings.db_path)
    tid = _video(db, 1, "2026-11-30T16:00:00Z", "privat")
    for path in ("/", "/freigabe", "/videos", "/videos?tab=geloescht", f"/videos/{tid}", "/statistik",
                 "/recherche", "/jobs", "/jobs?filter=fehler", "/einstellungen"):
        r = c.get(path)
        assert r.status_code == 200, path
        assert "Heute" in r.text and "Einstellungen" in r.text                     # Navigation überall
    assert c.get("/settings").url.path == "/einstellungen"
    assert c.get("/research").url.path == "/recherche"
    heute = c.get("/").text
    assert "Nächste 7 Termine" in heute and "Vorrat knapp" in heute          # nur 1 Video geplant → braucht dich
    assert "Videoarchiv" not in heute and "Stil-Vergleich" not in heute and "Letzte Jobs" not in heute
