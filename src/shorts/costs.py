"""Kosten je Video: Claude exakt aus Token-Zahlen, TTS/Whisper geschätzt aus der Audiolänge.

Preise in USD. Claude-Preise pro Million Tokens (Stand 2026-11, Anthropic-Preisliste); Thinking-Tokens
werden als Output-Tokens abgerechnet und sind in usage.output_tokens enthalten.
TTS/Whisper-Minutenpreise sind Schätzwerte und per .env anpassbar.
"""
from __future__ import annotations

import json

CLAUDE_USD_PER_MTOK: dict[str, tuple[float, float]] = {  # (input, output)
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-2": (8.00, 40.00),
}

LABELS = {"claude": "Claude", "tts": "Stimme", "whisper": "Whisper"}


def claude_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    price = CLAUDE_USD_PER_MTOK.get(model)
    if price is None:
        return None  # unbekanntes Modell → lieber keine Zahl als eine falsche
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


def per_minute_cost(seconds: float, usd_per_min: float) -> float:
    return seconds / 60 * usd_per_min


def add_part(cost_json: str | None, part: str, usd: float | None) -> str:
    data = json.loads(cost_json) if cost_json else {}
    if usd is None:
        data.setdefault("_unbekannt", []).append(part)
    else:
        data[part] = round(data.get(part, 0.0) + usd, 6)
    return json.dumps(data)


def total(cost_json: str | None) -> float:
    data = json.loads(cost_json) if cost_json else {}
    return sum(v for k, v in data.items() if not k.startswith("_"))


def _eur(usd: float, eur_per_usd: float) -> str:
    return f"{usd * eur_per_usd:.3f}".replace(".", ",") + " €"


def format_cost(cost_json: str | None, eur_per_usd: float) -> str:
    """'ca. 0,052 € (Claude 0,043 € · Stimme 0,007 € · Whisper 0,003 €)'"""
    data = json.loads(cost_json) if cost_json else {}
    parts = [f"{LABELS.get(k, k)} {_eur(v, eur_per_usd)}" for k, v in data.items() if not k.startswith("_")]
    if not parts:
        return "keine Angaben"
    text = f"ca. {_eur(total(cost_json), eur_per_usd)} ({' · '.join(parts)})"
    if data.get("_unbekannt"):
        text += " + unbekannt: " + ", ".join(sorted(set(data["_unbekannt"])))
    return text
