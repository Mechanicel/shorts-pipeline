"""Günstige Vorauswahl vor dem teuren Skript-Modell.

Stufe 1 – Vorfilter (kostenlos): Titel mit eindeutig irrelevanten Begriffen (Diplomatie, Termine, Glückwünsche).
Stufe 2 – Vorauswahl (Claude Haiku): ein Aufruf für viele Themen, nur Titel + Anriss. Im Zweifel durchlassen –
          das Skript-Modell prüft danach den vollen Quelltext.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, Field

from .config import ChannelConfig, Settings
from .costs import claude_cost

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ Stufe 1: Vorfilter
def prefilter_reason(title: str, blocklist: list[str]) -> str | None:
    """Gibt den gefundenen Begriff zurück, wenn der Titel eindeutig nicht passt."""
    for term in blocklist:
        if re.search(rf"(?<!\w){re.escape(term)}", title, re.IGNORECASE):
            return term
    return None


# ------------------------------------------------------------------ Stufe 2: Vorauswahl
class TriageItem(BaseModel):
    id: int
    relevant: bool = Field(description="true, wenn das Thema plausibel eine konkrete Änderung für Privatpersonen enthält.")
    reason: str = Field(description="Begründung auf Deutsch, max. 12 Wörter.")


class TriageBatch(BaseModel):
    items: list[TriageItem]


SYSTEM = """\
You pre-screen news items for the YouTube Shorts channel "{channel}" about concrete changes for ordinary \
people in {locale}: laws, prices, fees, deadlines, money, benefits, consumer rights and obligations.

For every item decide:
- relevant=true if it plausibly announces or explains a concrete change, deadline, price, amount, right or \
obligation that affects private individuals.
- relevant=false for diplomacy, visits, phone calls, congratulations, condolences, conferences, speeches, \
research events, party politics, personnel news and purely institutional news without a concrete effect.
When unsure, choose relevant=true – a later step reads the full article.
Return exactly one entry per input id. Write the reason in German, max. 12 words."""


@dataclass(frozen=True)
class Candidate:
    id: int
    title: str
    summary: str


class Triage(Protocol):
    spent: list[float | None]

    def screen(self, candidates: list[Candidate]) -> dict[int, tuple[bool, str]]: ...


class HaikuTriage:
    def __init__(self, settings: Settings, channel: ChannelConfig) -> None:
        import anthropic

        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY fehlt in .env")
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=3)
        self._model = settings.anthropic_triage_model
        self._system = SYSTEM.format(channel=channel.name, locale=channel.locale_hint or channel.language)
        self.spent: list[float | None] = []

    def screen(self, candidates: list[Candidate]) -> dict[int, tuple[bool, str]]:
        self.spent = []
        if not candidates:
            return {}
        listing = "\n\n".join(f"[{c.id}] {c.title}\n{(c.summary or '').strip()[:400]}" for c in candidates)
        response = self._client.messages.parse(
            model=self._model,
            max_tokens=4000,
            system=self._system,
            messages=[{"role": "user", "content": f"ITEMS:\n\n{listing}"}],
            output_format=TriageBatch,
            output_config={"effort": "low"},
            thinking={"type": "disabled"},  # einfache Einordnung; bei Haiku 5.5 bis Effort high erlaubt
        )
        self.spent.append(claude_cost(response.model, response.usage.input_tokens, response.usage.output_tokens))
        log.info("Vorauswahl: model=%s stop=%s in=%s out=%s (%d Themen)", response.model, response.stop_reason,
                 response.usage.input_tokens, response.usage.output_tokens, len(candidates))
        if response.stop_reason != "end_turn" or response.parsed_output is None:
            raise RuntimeError(f"Vorauswahl ohne gültige Antwort (stop_reason={response.stop_reason})")
        result = {item.id: (item.relevant, item.reason) for item in response.parsed_output.items}
        # Fehlende IDs nicht verwerfen – im Zweifel durchlassen
        return {c.id: result.get(c.id, (True, "keine Einschätzung – durchgelassen")) for c in candidates}


class PassAllTriage:
    """Dry-Run/Fallback: lässt alles durch, kostet nichts."""
    spent: list[float | None] = []

    def screen(self, candidates: list[Candidate]) -> dict[int, tuple[bool, str]]:
        return {c.id: (True, "ohne Vorauswahl") for c in candidates}
