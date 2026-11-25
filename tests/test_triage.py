from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

from shorts import sample_data
from shorts.config import Settings, load_channels
from shorts.db import Database, Status
from shorts.pipeline import Pipeline, RunReport
from shorts.script import SCRIPT_STYLE, ShortScript
from shorts.triage import (Candidate, HaikuTriage, TriageBatch, TriageItem, Verdict, covered_listing,
                          prefilter_reason, similar_scripts)
from shorts.tts import EspeakTTS

BLOCK = load_channels()["de"].title_blocklist


def test_vorfilter() -> None:
    assert prefilter_reason("Bundeskanzler Merz gratuliert dem Ministerpräsidenten", BLOCK) == "gratuliert"
    assert prefilter_reason("Erstes Treffen mit neuer französischer Amtskollegin", BLOCK) == "Treffen mit"
    assert prefilter_reason("Beitragsbemessungsgrenzen steigen 2027", BLOCK) is None
    assert prefilter_reason("Betrifft Rentner: neue Regeln", BLOCK) is None  # 'trifft' nur als Wortanfang


def test_haiku_vorauswahl_mit_fehlender_id_laesst_durch() -> None:
    triage = HaikuTriage.__new__(HaikuTriage)
    triage._model, triage._system, triage.spent = "claude-haiku-5-5", "sys", []
    parsed = TriageBatch(items=[TriageItem(id=1, relevant=False, reason="Diplomatie"),
                                TriageItem(id=2, relevant=True, reason="neue Frist", score=9, duplicate_of=3),
                                TriageItem(id=3, relevant=True, reason="gleiches Paket", duplicate_of=2),
                                TriageItem(id=5, relevant=True, reason="schon da", duplicate_of=99)])
    calls = []

    def parse(**kw):
        calls.append(kw)
        return SimpleNamespace(model="claude-haiku-5-5", stop_reason="end_turn", parsed_output=parsed,
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=200))

    triage._client = SimpleNamespace(messages=SimpleNamespace(parse=parse))
    out = triage.screen([Candidate(1, "A", ""), Candidate(2, "B", "x"), Candidate(3, "C", ""),
                         Candidate(4, "D", ""), Candidate(5, "E", "")], covered="[#99] Stromsteuer sinkt")
    assert out[1].relevant is False and out[1].reason == "Diplomatie"
    assert out[2].relevant and out[2].score == 5  # auf 1–5 begrenzt
    assert out[2].duplicate_of is None  # darf nicht auf ein späteres Item zeigen (sonst gegenseitiges Verwerfen)
    assert out[3].duplicate_of == 2
    assert out[4].relevant and out[4].reason.startswith("keine Einschätzung")  # fehlende ID → durchlassen
    assert out[5].duplicate_of == 99
    assert len(calls) == 1 and calls[0]["thinking"] == {"type": "disabled"}
    assert "[#99] Stromsteuer sinkt" in calls[0]["messages"][0]["content"]
    assert abs(triage.spent[0] - (1000 * 0.10 + 200 * 0.50) / 1e6) < 1e-12


class FakeTriage:
    def __init__(self, verdicts):
        self.verdicts, self.spent, self.seen, self.covered = verdicts, [0.0002], [], None

    def screen(self, candidates, covered="(none)"):
        self.seen += [c.id for c in candidates]
        self.covered = covered
        return {c.id: self.verdicts.get(c.id, Verdict(True, "ok")) for c in candidates}


class FakeWriter:
    def __init__(self):
        self.calls, self.spent, self.covered = [], [], []

    def write(self, **kw):
        self.calls.append(kw["headline"])
        self.covered.append(kw["covered"])
        self.spent = [0.02]
        return ShortScript.model_validate({**sample_data.SAMPLE_SCRIPT, "discard": True,
                                           "discard_reason": "kein Verbraucherbezug"})


def test_pipeline_filtert_vor_dem_teuren_modell(tmp_path) -> None:
    settings = replace(Settings(), data_dir=tmp_path, output_dir=tmp_path / "out",
                       telegram_bot_token=None, telegram_chat_id=None)
    ch = replace(load_channels()["de"], max_videos_per_run=1, max_new_topics_per_run=4)
    db = Database(tmp_path / "p.sqlite3")
    today = datetime.now(timezone.utc).isoformat()
    ids = {}
    for title in ("Merz gratuliert Lettland", "Tagung Sozialpolitik", "Kulturminister im Gespräch",
                  "Neue Frist für Grundsteuer-Erklärung", "Rente steigt im Juli"):
        ids[title] = db.add_topic("de", f"https://bund.de/{len(ids)}", "Q", title, published_at=today)
        db.update_fields(ids[title], source_text="x" * 500)
    triage = FakeTriage({ids["Kulturminister im Gespräch"]: Verdict(False, "Diplomatie")})
    writer = FakeWriter()
    report = RunReport()
    Pipeline(settings, ch, db, writer=writer, tts=EspeakTTS(), triage=triage).step_script(report)

    # Vorfilter verwirft 2 kostenlos, Vorauswahl 1, nur 2 Themen gehen an das Skript-Modell
    assert db.get(ids["Merz gratuliert Lettland"]).note == "Vorfilter: Titel enthält „gratuliert“"
    assert ids["Merz gratuliert Lettland"] not in triage.seen
    assert db.get(ids["Kulturminister im Gespräch"]).note == "Vorauswahl: Diplomatie"
    assert sorted(writer.calls) == ["Neue Frist für Grundsteuer-Erklärung", "Rente steigt im Juli"]
    status, text = report.result()
    assert status == "warn" and text.startswith("Kein Video: 5 Themen geprüft und verworfen")
    assert "2× Vorfilter" in text and "1× Vorauswahl" in text and "2× Skript-Prüfung" in text
    assert db.get(ids["Rente steigt im Juli"]).cost_json  # Vorauswahl-Kosten anteilig erfasst


def _settings(tmp_path):
    return replace(Settings(), data_dir=tmp_path, output_dir=tmp_path / "out",
                   telegram_bot_token=None, telegram_chat_id=None)


STROM = {**sample_data.SAMPLE_SCRIPT, "title": "Stromsteuer soll 2027 sinken: von 2,05 auf 0,1 Cent pro kWh",
         "overlay_title": "Stromsteuer sinkt", "key_facts": [{"value": "0,1 Cent", "label": "Stromsteuer pro kWh"}]}


def test_dubletten_ohne_ki_an_echten_titeln() -> None:
    def sc(title, overlay="", labels=()):
        return {"title": title, "overlay_title": overlay, "key_facts": [{"value": "1", "label": x} for x in labels]}
    others = [(13, sc("Stromsteuer soll 2027 auf 0,1 Cent sinken: Das ist geplant", "Stromsteuer runter", ["pro kWh"])),
              (14, sc("Spritsteuer: etwa 17 Cent weniger pro Liter bis Ende 2026", "Sprit billiger",
                      ["Entlastung pro Liter", "geplanter Spritpreisdeckel"])),
              (12, sc("Rentenbeitrag bleibt 2027 bei 18,6 Prozent", "Beitrag bleibt", ["Rentenbeitrag"]))]
    assert similar_scripts(STROM, others) == [13]
    tank = sc("Tankrabatt endet Ende 2026 – Spritpreisdeckel für 2027 geplant", "Tankrabatt endet",
              ["Entlastung pro Liter", "Spritpreisdeckel"])
    assert similar_scripts(tank, others) == [14]
    assert "[#13] Stromsteuer" in covered_listing(others) and covered_listing([]) == "(none)"


def test_pipeline_dubletten_und_prioritaet(tmp_path) -> None:
    ch = replace(load_channels()["de"], max_videos_per_run=3, max_new_topics_per_run=5)
    db = Database(tmp_path / "p.sqlite3")
    today = datetime.now(timezone.utc).isoformat()
    old = db.add_topic("de", "https://bund.de/alt", "Q", "Entlastungen Energie", published_at=today)
    db.transition(old, Status.SKRIPT, script_json=json.dumps(STROM, ensure_ascii=False))
    ids = {}
    for title in ("Stromsteuer sinkt", "Rente: neue Regeln", "Porto steigt", "Kindergeld steigt"):
        ids[title] = db.add_topic("de", f"https://bund.de/{len(ids)}", "Q", title, published_at=today)
        db.update_fields(ids[title], source_text="x" * 500)
    triage = FakeTriage({ids["Stromsteuer sinkt"]: Verdict(True, "gleiche Senkung", duplicate_of=old),
                         ids["Porto steigt"]: Verdict(True, "Porto", score=2),
                         ids["Kindergeld steigt"]: Verdict(True, "Familie", score=5)})
    writer = FakeWriter()
    report = RunReport()
    Pipeline(_settings(tmp_path), ch, db, writer=writer, tts=EspeakTTS(), triage=triage).step_script(report)

    assert f"[#{old}] Stromsteuer soll 2027 sinken" in triage.covered  # Haiku sieht schon Behandeltes
    assert db.get(ids["Stromsteuer sinkt"]).note.startswith(f"Vorauswahl: Dublette von #{old}")
    assert writer.calls == ["Kindergeld steigt", "Rente: neue Regeln", "Porto steigt"]  # nach Priorität
    assert f"[#{old}]" in writer.covered[0]  # und das Skript-Modell ebenso
    assert db.get(ids["Kindergeld steigt"]).triage_score == "5"
    assert "1× Dublette" in report.result()[1]


def test_skript_bekommt_dubletten_hinweis_und_stil(tmp_path) -> None:
    ch = replace(load_channels()["de"], max_videos_per_run=2, max_new_topics_per_run=1)
    db = Database(tmp_path / "p.sqlite3")
    today = datetime.now(timezone.utc).isoformat()
    old = db.add_topic("de", "https://bund.de/alt", "Q", "Alt", published_at=today)
    db.transition(old, Status.SKRIPT, script_json=json.dumps(STROM, ensure_ascii=False))
    new = db.add_topic("de", "https://bund.de/neu", "Q", "Neu", published_at=today)
    db.update_fields(new, source_text=sample_data.SAMPLE_SOURCE_TEXT, triage="ok: x")

    class Writer:
        spent: list = []

        def write(self, **kw):
            return ShortScript.model_validate({**STROM, "title": "Stromsteuer soll sinken – auch für Haushalte"})
    Pipeline(_settings(tmp_path), ch, db, writer=Writer(), tts=EspeakTTS(),
             triage=FakeTriage({})).step_script(RunReport())
    t = db.get(new)
    assert t.status == Status.SKRIPT and "Ähnliches Thema schon vorhanden: #%d" % old in t.note
    assert t.script["style"] == SCRIPT_STYLE


def test_zweiter_satz_ohne_behoerden_einstieg() -> None:
    from shorts.script import hook_problems
    assert any("Zweiter Satz" in p for p in hook_problems(
        "Deine Stromsteuer soll sinken. Die Bundesregierung hat beschlossen, dass sie fällt. Rest."))
    assert not any("Zweiter Satz" in p for p in hook_problems(
        "Deine Stromsteuer soll sinken. Geplant sind nur noch 0,1 Cent pro Kilowattstunde. Rest."))


def test_hook_tafel_ab_frame_0() -> None:
    from shorts.subtitles import build_ass, estimate_timings, hook_window, split_words
    narration = "Diese Nummer soll dir helfen. Die 116 117 soll ersteinschätzen."
    timings = estimate_timings(split_words(narration), 0.0, 6.0)
    hook, end = hook_window(narration, timings)
    assert hook == "Diese Nummer soll dir helfen." and end >= 1.6
    ass = build_ass(timings=timings, title="T", source_label="x", outro="", total_duration=6.4,
                    end_card_seconds=0, hook=hook, hook_end=end)
    assert "Dialogue: 3,0:00:00.00," in ass and "DIESE NUMMER SOLL DIR HELFEN." in ass
    captions = [line for line in ass.splitlines() if ",Caption," in line]
    assert captions and all("NUMMER" not in c for c in captions)  # Hook nicht doppelt als Untertitel
    title = next(line for line in ass.splitlines() if ",Title," in line)
    assert title.split(",")[1] != "0:00:00.00"


def test_alte_themen_verbrauchen_kein_claude_budget(tmp_path, monkeypatch) -> None:
    from datetime import timedelta
    ch = replace(load_channels()["de"], max_videos_per_run=1, max_new_topics_per_run=2)
    db = Database(tmp_path / "p.sqlite3")
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    today = datetime.now(timezone.utc).isoformat()
    for i in range(4):  # vier alte Themen ohne Datum in der DB, Datum kommt erst von der Artikelseite
        tid = db.add_topic("de", f"https://bund.de/alt{i}", "Q", f"Alt {i}")
        db.update_fields(tid, source_text="x" * 500, triage="ok: x", triage_score="5")
    fresh = db.add_topic("de", "https://bund.de/neu", "Q", "Neu", published_at=today)
    db.update_fields(fresh, source_text="x" * 500, triage="ok: x", triage_score="1")
    import shorts.pipeline as pl
    from shorts.sources import Article
    monkeypatch.setattr(pl, "fetch_article", lambda url: Article("x" * 500, old[:10]))  # altes Datum von der Seite
    writer = FakeWriter()
    Pipeline(_settings(tmp_path), ch, db, writer=writer, tts=EspeakTTS(), triage=FakeTriage({})).step_script(RunReport())
    assert writer.calls == ["Neu"]  # trotz Budget 2 kommt das frische Thema nach 4 kostenlosen Verwerfungen dran
