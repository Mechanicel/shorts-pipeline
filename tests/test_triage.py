from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

from shorts import sample_data
from shorts.config import Settings, load_channels
from shorts.db import Database, Status
from shorts.pipeline import Pipeline, RunReport
from shorts.script import ShortScript
from shorts.triage import Candidate, HaikuTriage, TriageBatch, TriageItem, prefilter_reason
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
                                TriageItem(id=2, relevant=True, reason="neue Frist")])
    calls = []

    def parse(**kw):
        calls.append(kw)
        return SimpleNamespace(model="claude-haiku-5-5", stop_reason="end_turn", parsed_output=parsed,
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=200))

    triage._client = SimpleNamespace(messages=SimpleNamespace(parse=parse))
    out = triage.screen([Candidate(1, "A", ""), Candidate(2, "B", "x"), Candidate(3, "C", "")])
    assert out[1] == (False, "Diplomatie") and out[2][0] is True
    assert out[3][0] is True  # keine Einschätzung → im Zweifel durchlassen
    assert len(calls) == 1 and calls[0]["thinking"] == {"type": "disabled"}
    assert abs(triage.spent[0] - (1000 * 0.10 + 200 * 0.50) / 1e6) < 1e-12


class FakeTriage:
    def __init__(self, verdicts):
        self.verdicts, self.spent, self.seen = verdicts, [0.0002], []

    def screen(self, candidates):
        self.seen += [c.id for c in candidates]
        return {c.id: self.verdicts.get(c.id, (True, "ok")) for c in candidates}


class FakeWriter:
    def __init__(self):
        self.calls, self.spent = [], []

    def write(self, **kw):
        self.calls.append(kw["headline"])
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
    triage = FakeTriage({ids["Kulturminister im Gespräch"]: (False, "Diplomatie")})
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
