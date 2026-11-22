"""Günstige Vorauswahl vor dem teuren Skript-Modell.

Stufe 1 – Vorfilter (kostenlos): Titel mit eindeutig irrelevanten Begriffen (Diplomatie, Termine, Glückwünsche).
Stufe 2 – Vorauswahl (Claude Haiku): ein Aufruf für viele Themen, nur Titel + Anriss. Im Zweifel durchlassen –
          das Skript-Modell prüft danach den vollen Quelltext. Erkennt außerdem Dubletten zu schon behandelten
          Themen und vergibt eine Priorität (1–5).
Dazu ein KI-freies Sicherheitsnetz: `similar_scripts` vergleicht fertige Skripte über ihre Inhaltswörter.
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


# ------------------------------------------------------------------ Dubletten ohne KI
_STOP = set("""der die das und oder für von vom auf aus mit bei bis ab im in am an zu zum zur ein eine einen dem den des
ist sind soll sollen wird werden kann neue neuer neues mehr weniger was wer wie so cent euro prozent jahr pro über
unter nach vor nicht auch noch geplant geplante geplanter ende ohne bald dich dir dein deine wer""".split())


def content_words(script: dict) -> set[str]:
    """Inhaltswörter aus Titel, Bild-Titel und Grafik-Beschriftungen eines Skripts."""
    text = " ".join([script.get("title", ""), script.get("overlay_title", "")]
                    + [f.get("label", "") for f in script.get("key_facts", [])])
    return {w for w in re.findall(r"[a-zäöüß][a-zäöüß-]{2,}", text.lower()) if w not in _STOP}


def similar_scripts(script: dict, others: list[tuple[int, dict]]) -> list[int]:
    """IDs der Skripte, die dasselbe Thema behandeln (≥ 2 gemeinsame Inhaltswörter, Überlappung ≥ 30 %).
    Kalibriert an echten Dubletten (Stromsteuer 4×, Tankrabatt 2×, Befristung 2×) ohne Fehltreffer."""
    mine = content_words(script)
    hits = []
    for other_id, other in others:
        theirs = content_words(other)
        shared = mine & theirs
        if len(shared) >= 2 and len(shared) / max(min(len(mine), len(theirs)), 1) >= 0.3:
            hits.append(other_id)
    return hits


def covered_listing(covered: list[tuple[int, dict]]) -> str:
    """Kurzliste schon behandelter Themen für die Prompts: [#id] Titel – Kernzahlen."""
    lines = []
    for tid, s in covered:
        facts = ", ".join(f"{f.get('value', '')} {f.get('label', '')}".strip() for f in s.get("key_facts", []))
        lines.append(f"[#{tid}] {s.get('title', '')}" + (f" – {facts}" if facts else ""))
    return "\n".join(lines) or "(none)"


# ------------------------------------------------------------------ Stufe 2: Vorauswahl
class TriageItem(BaseModel):
    id: int
    relevant: bool = Field(description="true, wenn das Thema plausibel eine konkrete Änderung für Privatpersonen enthält.")
    duplicate_of: int | None = Field(
        default=None, description="ID eines schon behandelten Themas (COVERED) oder eines früheren ITEMS mit derselben "
                                  "Kernänderung, sonst null.")
    score: int = Field(default=3, description="Priorität 1–5: wie stark betrifft es viele Menschen konkret und spürbar.")
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

Duplicates: the channel must never publish the same change twice. Set duplicate_of to the id of a COVERED topic \
when the item is mainly about the same core change (same tax, benefit, rule or relief package), even if the headline \
is worded differently. Within ITEMS, if several items cover the same core change, keep the most specific one and set \
duplicate_of on the others to its id. Different aspects of one package that are each big enough for a video \
(e.g. electricity tax vs. fuel price cap) are not duplicates. When unsure, leave duplicate_of null.

score (1–5): 5 = affects very many people directly with money, health, pension, family benefits or a deadline \
(e.g. pension, child benefit, health insurance, emergency number, money back from a lawsuit); 3 = narrower group \
or indirect effect; 1 = barely noticeable for individuals.
Return exactly one entry per input id. Write the reason in German, max. 12 words."""


@dataclass(frozen=True)
class Candidate:
    id: int
    title: str
    summary: str


@dataclass(frozen=True)
class Verdict:
    relevant: bool
    reason: str
    duplicate_of: int | None = None
    score: int = 3


class Triage(Protocol):
    spent: list[float | None]

    def screen(self, candidates: list[Candidate], covered: str = "(none)") -> dict[int, Verdict]: ...


class HaikuTriage:
    def __init__(self, settings: Settings, channel: ChannelConfig) -> None:
        import anthropic

        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY fehlt in .env")
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=3)
        self._model = settings.anthropic_triage_model
        self._system = SYSTEM.format(channel=channel.name, locale=channel.locale_hint or channel.language)
        self.spent: list[float | None] = []

    def screen(self, candidates: list[Candidate], covered: str = "(none)") -> dict[int, Verdict]:
        self.spent = []
        if not candidates:
            return {}
        listing = "\n\n".join(f"[{c.id}] {c.title}\n{(c.summary or '').strip()[:400]}" for c in candidates)
        response = self._client.messages.parse(
            model=self._model,
            max_tokens=6000,
            system=self._system,
            messages=[{"role": "user", "content": f"COVERED (already on the channel, last 30 days):\n{covered}"
                                                  f"\n\nITEMS:\n\n{listing}"}],
            output_format=TriageBatch,
            output_config={"effort": "low"},
            thinking={"type": "disabled"},  # einfache Einordnung; bei Haiku 5.5 bis Effort high erlaubt
        )
        self.spent.append(claude_cost(response.model, response.usage.input_tokens, response.usage.output_tokens))
        log.info("Vorauswahl: model=%s stop=%s in=%s out=%s (%d Themen)", response.model, response.stop_reason,
                 response.usage.input_tokens, response.usage.output_tokens, len(candidates))
        if response.stop_reason != "end_turn" or response.parsed_output is None:
            raise RuntimeError(f"Vorauswahl ohne gültige Antwort (stop_reason={response.stop_reason})")
        pos = {c.id: i for i, c in enumerate(candidates)}
        result = {}
        for item in response.parsed_output.items:
            dup = item.duplicate_of if item.duplicate_of not in (None, item.id) else None
            if dup in pos and pos[dup] >= pos.get(item.id, -1):
                dup = None  # nur auf frühere Items verweisen, sonst könnten sich zwei gegenseitig verwerfen
            result[item.id] = Verdict(item.relevant, item.reason, dup, min(max(item.score, 1), 5))
        # Fehlende IDs nicht verwerfen – im Zweifel durchlassen
        return {c.id: result.get(c.id, Verdict(True, "keine Einschätzung – durchgelassen")) for c in candidates}


class PassAllTriage:
    """Dry-Run/Fallback: lässt alles durch, kostet nichts."""
    spent: list[float | None] = []

    def screen(self, candidates: list[Candidate], covered: str = "(none)") -> dict[int, Verdict]:
        return {c.id: Verdict(True, "ohne Vorauswahl") for c in candidates}
