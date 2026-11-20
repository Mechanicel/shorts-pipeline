from __future__ import annotations

import json

import pytest

from shorts import sample_data
from shorts.costs import claude_cost, format_cost, per_minute_cost, total
from shorts.db import Database, Status
from shorts.review_bot import format_review


def test_claude_kosten_aus_tokens() -> None:
    # Opus 5.5: 4 $ / 20 $ pro Mio. Tokens
    assert claude_cost("claude-opus-5-5", 4000, 3000) == pytest.approx(0.016 + 0.06)
    assert claude_cost("unbekanntes-modell", 1, 1) is None


def test_minutenpreis() -> None:
    assert per_minute_cost(30, 0.015) == pytest.approx(0.0075)


def test_kosten_summieren_und_formatieren() -> None:
    db = Database(":memory:")
    tid = db.add_topic("de", "https://a.de", "Q", "T")
    db.add_cost(tid, "claude", 0.04)
    db.add_cost(tid, "claude", 0.02)   # Korrekturversuch
    db.add_cost(tid, "tts", 0.0075)
    db.add_cost(tid, "whisper", 0.003)
    cj = db.get(tid).cost_json
    assert json.loads(cj)["claude"] == pytest.approx(0.06)
    assert total(cj) == pytest.approx(0.0705)
    text = format_cost(cj, eur_per_usd=1.0)
    assert text.startswith("ca. 0,071 €") or text.startswith("ca. 0,070 €")
    assert "Claude 0,060 €" in text and "Stimme" in text and "Whisper" in text
    db.add_cost(tid, "claude", None)    # unbekanntes Modell wird sichtbar gemacht
    assert "unbekannt: claude" in format_cost(db.get(tid).cost_json, 1.0)


def test_review_nachricht_zeigt_kosten() -> None:
    db = Database(":memory:")
    tid = db.add_topic("de", "https://a.de", "Q", "T")
    db.transition(tid, Status.SKRIPT, script_json=json.dumps(sample_data.SAMPLE_SCRIPT))
    db.add_cost(tid, "claude", 0.05)
    assert "💶 Kosten: ca. 0,045 €" in format_review(db.get(tid), eur_per_usd=0.9)
