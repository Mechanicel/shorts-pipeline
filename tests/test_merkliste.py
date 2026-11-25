from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from shorts import sample_data
from shorts.config import Settings, load_channels
from shorts.db import Database, Status
from shorts.merkliste import (MAX_SECONDS, MIN_SECONDS, STYLE_V2, STYLE_V3, MerklisteScript, cta_problems,
                              description_v3, highlight_windows, layout_problems, narration_problems,
                              status_warnings, text_limit_problems, unverified_items, validate_v3, video_length)
from shorts.pipeline import Pipeline, RunReport
from shorts.script import ShortScript
from shorts.tts import EspeakTTS


def v3(**over) -> dict:
    data = copy.deepcopy(sample_data.SAMPLE_V3)
    data.update(over)
    return data


def test_beispiel_merkliste_ist_gueltig() -> None:
    assert validate_v3(MerklisteScript.model_validate(v3())) == []


def test_textlaengen_und_anzahl() -> None:
    s = MerklisteScript.model_validate(v3(headline="X" * 33))
    assert any("headline" in p for p in text_limit_problems(s))
    items = v3()["items"]
    items[0]["label"] = "L" * 29
    items[1]["new_value"] = ""
    probs = text_limit_problems(MerklisteScript.model_validate(v3(items=items)))
    assert any("Punkt 1: label" in p for p in probs) and any("Punkt 2: new_value fehlt" in p for p in probs)
    assert any("Punkte" in p for p in text_limit_problems(MerklisteScript.model_validate(v3(items=items[:2]))))


def test_layout_pruefung_erkennt_ueberlauf() -> None:
    assert layout_problems(v3()) == []
    items = v3()["items"]
    full = [dict(items[0], label=f"Sehr lange Bezeichnung {i}", note="Zusatz mit sehr vielen Wörtern am Ende hier")
            for i in range(6)]
    assert any("zu hoch" in p for p in layout_problems(v3(items=full)))
    wide = [dict(items[0], old_value="1.234.567,89 €", new_value="9.876.543,21 €")] + items[1:]
    assert any("Wert" in p for p in layout_problems(v3(items=wide)))


def test_sprechtext_und_cta() -> None:
    assert narration_problems(v3()["narration"]) == []
    assert any("Wörter" in p for p in narration_problems("Dein Geld wird mehr. Hier alles."))
    assert any("Datums" in p for p in narration_problems("Ab Januar bekommst du mehr Geld. " + "Wort " * 20))
    assert cta_problems("Schick das jemandem, der Kinder hat.") == []
    assert cta_problems("Abonniere für mehr!") and cta_problems("Schick das jemandem und like das Video.")


def test_status_mapping_und_belege() -> None:
    items = [{"label": "Mindestlohn", "status": "beschlossen", "evidence": "Der Mindestlohn soll steigen.",
              "old_value": "13,90 €", "new_value": "14,60 €"},
             {"label": "Minijob", "status": "geplant", "evidence": "Die Grenze soll auf 633 Euro steigen.",
              "old_value": "", "new_value": "633 €"},
             {"label": "Rente", "status": "in Kraft", "evidence": "Seit Juli gilt der neue Rentenwert.",
              "old_value": "", "new_value": "+3,7 %"}]
    warns = status_warnings(items)
    assert len(warns) == 1 and "Mindestlohn" in warns[0]           # „soll“ ≠ beschlossen
    src = "Die Grenze soll auf 633 Euro steigen. Seit Juli gilt der neue Rentenwert. Von 13,90 Euro auf 14,60 Euro."
    problems = unverified_items(items, src)
    assert "Punkt 1: Zitat nicht wörtlich im Quelltext" in problems   # Zitat 1 steht so nicht drin
    assert any("Punkt 3: Zahl 3,7" in p for p in problems)             # +3,7 % kommt nicht vor
    assert not any(p.startswith("Punkt 2") for p in problems)


def test_laenge_hart_begrenzt_und_loop() -> None:
    assert video_length(3.0) == MIN_SECONDS
    assert video_length(6.5) == 9.5
    assert video_length(11.0) == MAX_SECONDS
    windows = highlight_windows(4, 2.0, 10.0)
    assert len(windows) == 4 and windows[0][1] == 2.0
    assert windows[-1][2] < 10.0 - 0.5                     # Ende ohne Hervorhebung → letzter Frame = erster
    assert all(a < b for _, a, b in windows)
    assert highlight_windows(6, 9.0, 10.0) == []           # zu wenig Zeit → keine Animation


def test_beschreibung_enthaelt_liste_mit_status() -> None:
    text = description_v3(v3(), "https://bund.de/x", "Bild: Pixabay https://pixabay.com/x")
    assert "✅ Rücksendefrist: 14 Tage → 30 Tage (für alle Bestellungen) – beschlossen" in text
    assert "🕒 Stichtag: 1. März – geplant" in text
    assert "Quelle: https://bund.de/x" in text and "Bild: Pixabay" in text and "ohne Gewähr" in text


# ---------------------------------------------------------------- Pipeline: Stilwahl
class Writer:
    def __init__(self, list_possible=True):
        self.list_possible, self.calls, self.spent = list_possible, [], []

    def write(self, **kw):
        self.calls.append("v2")
        return ShortScript.model_validate(sample_data.SAMPLE_SCRIPT)

    def write_v3(self, **kw):
        self.calls.append("v3")
        data = v3()
        if not self.list_possible:
            data.update(list_possible=False, list_impossible_reason="nur eine Zahl im Text")
        return MerklisteScript.model_validate(data)


def _setup(tmp_path, n: int, style_mode="alternate", writer=None):
    settings = replace(Settings(), data_dir=tmp_path, output_dir=tmp_path / "out", telegram_bot_token=None,
                       telegram_chat_id=None, style_mode=style_mode)
    ch = replace(load_channels()["de"], max_videos_per_run=n, max_new_topics_per_run=n)
    db = Database(tmp_path / "p.sqlite3")
    today = datetime.now(timezone.utc).isoformat()
    ids = []
    for i in range(n):
        tid = db.add_topic("de", f"https://bund.de/{i}", "Q", f"Thema {i}", published_at=today)
        db.update_fields(tid, source_text=sample_data.SAMPLE_SOURCE_TEXT, triage="ok: x")
        ids.append(tid)

    class NoTriage:
        spent: list = []

        def screen(self, c, covered="(none)"):
            return {}
    writer = writer or Writer()
    Pipeline(settings, ch, db, writer=writer, tts=EspeakTTS(), triage=NoTriage()).step_script(RunReport())
    return db, ids, writer


def test_alternate_wechselt_ab(tmp_path) -> None:
    db, ids, writer = _setup(tmp_path, 4)
    styles = [db.get(i).style for i in ids]
    assert sorted(styles) == [STYLE_V2, STYLE_V2, STYLE_V3, STYLE_V3]
    assert db.style_counts("de") == {STYLE_V2: 2, STYLE_V3: 2}
    t = db.get(next(i for i in ids if db.get(i).style == STYLE_V3))
    assert t.status == Status.SKRIPT and t.script["style"] == STYLE_V3 and len(t.script["items"]) == 4


@pytest.mark.parametrize("mode,expected", [("v2", STYLE_V2), ("v3", STYLE_V3)])
def test_fester_stil(tmp_path, mode, expected) -> None:
    db, ids, _ = _setup(tmp_path, 2, style_mode=mode)
    assert {db.get(i).style for i in ids} == {expected}


def test_ohne_listenfaehige_fakten_als_v2(tmp_path) -> None:
    db, ids, writer = _setup(tmp_path, 1, style_mode="v3", writer=Writer(list_possible=False))
    t = db.get(ids[0])
    assert writer.calls == ["v3", "v2"] and t.style == STYLE_V2
    assert t.note.startswith("v3 nicht möglich: nur eine Zahl im Text")


def test_kopfzeile_neu_ohne_aenderung_wird_gewarnt() -> None:
    items = [{"label": "Mindestlohn", "status": "beschlossen", "old_value": "13,90 €", "evidence": "x"},
             {"label": "Bußgeld", "status": "in Kraft", "old_value": "", "evidence": "x"}]
    warns = status_warnings(items, "AB 1. JANUAR 2027 GILT NEU")
    assert any("Bußgeld" in w for w in warns)
    assert status_warnings(items, "MINDESTLOHN: DAS GILT") == []


def test_stil_von_hand_fuer_den_lauf_wird_am_thema_gemerkt(tmp_path) -> None:
    settings = replace(Settings(), data_dir=tmp_path, output_dir=tmp_path / "out", telegram_bot_token=None,
                       telegram_chat_id=None, style_mode="alternate")
    ch = replace(load_channels()["de"], max_videos_per_run=2, max_new_topics_per_run=2)
    db = Database(tmp_path / "p.sqlite3")
    today = datetime.now(timezone.utc).isoformat()
    ids = [db.add_topic("de", f"https://bund.de/{i}", "Q", f"Thema {i}", published_at=today) for i in range(2)]
    for tid in ids:
        db.update_fields(tid, source_text=sample_data.SAMPLE_SOURCE_TEXT, triage="ok: x")
    db.update_fields(ids[1], style_pick="v2")  # beim Neuerzeugen gewählt → hat Vorrang vor dem Lauf

    class NoTriage:
        spent: list = []

        def screen(self, c, covered="(none)"):
            return {}
    pipe = Pipeline(settings, ch, db, writer=Writer(), tts=EspeakTTS(), triage=NoTriage())
    pipe.style_pick = "v3"
    pipe.step_script(RunReport())
    assert (db.get(ids[0]).style, db.get(ids[0]).style_pick) == (STYLE_V3, "v3")
    assert (db.get(ids[1]).style, db.get(ids[1]).style_pick) == (STYLE_V2, "v2")


def test_neu_erzeugen_mit_stil() -> None:
    db = Database(":memory:")
    tid = db.add_topic("de", "https://a.de/x", "Q", "X")
    db.transition(tid, Status.SKRIPT, script_json="{}")
    db.transition(tid, Status.FEHLER)
    assert db.regenerate(tid, style_pick="v3").style_pick == "v3"
    db.transition(tid, Status.FEHLER)
    assert db.regenerate(tid).style_pick is None  # ohne Wahl wieder automatisch


def _forced_pipe(tmp_path, writer):
    settings = replace(Settings(), data_dir=tmp_path, output_dir=tmp_path / "out", telegram_bot_token=None,
                       telegram_chat_id=None, style_mode="alternate")
    ch = replace(load_channels()["de"], max_videos_per_run=1, max_new_topics_per_run=1)
    db = Database(tmp_path / "p.sqlite3")

    class NoTriage:
        spent: list = []

        def screen(self, c, covered="(none)"):
            return {}
    return db, Pipeline(settings, ch, db, writer=writer, tts=EspeakTTS(), triage=NoTriage())


def _open_topic(db) -> int:
    tid = db.add_topic("de", "https://bund.de/x", "Q", "Thema", published_at=datetime.now(timezone.utc).isoformat())
    db.update_fields(tid, source_text=sample_data.SAMPLE_SOURCE_TEXT, triage="ok: x")
    return tid


def test_v3_fest_gewaehlt_ohne_liste_kein_ersatzvideo(tmp_path) -> None:
    writer = Writer(list_possible=False)
    db, pipe = _forced_pipe(tmp_path, writer)
    tid = _open_topic(db)
    pipe.style_pick = "v3"
    report = RunReport()
    pipe.step_script(report)
    t = db.get(tid)
    assert writer.calls == ["v3"]  # kein v2-Versuch
    assert t.status == Status.NEU and t.note.startswith("v3 nicht möglich") and t.style_pick is None
    status, text = report.result()
    assert text.startswith("Kein Video: kein Thema passt zu v3") and "bleiben" in text
    pipe.step_script(RunReport())  # nächster v3-Lauf bezahlt dasselbe Thema nicht noch einmal
    assert writer.calls == ["v3"]
    pipe.style_pick = None  # automatisch → darf als v2 laufen
    pipe.step_script(RunReport())
    assert db.get(tid).status == Status.SKRIPT and db.get(tid).style == STYLE_V2


def test_thema_als_v3_neu_erzeugt_ohne_liste_wird_fehler(tmp_path) -> None:
    writer = Writer(list_possible=False)
    db, pipe = _forced_pipe(tmp_path, writer)
    tid = _open_topic(db)
    db.update_fields(tid, style_pick="v3")
    pipe.step_script(RunReport())
    t = db.get(tid)
    assert writer.calls == ["v3"] and t.status == Status.FEHLER and "Neu erzeugen" in t.note
