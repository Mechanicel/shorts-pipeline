"""Short-Skript aus Quelltext erzeugen (Anthropic-API, Structured Outputs)."""
from __future__ import annotations

import json
import logging
import re
from typing import Protocol

from pydantic import BaseModel, Field

from .config import ChannelConfig, Settings

log = logging.getLogger(__name__)

MIN_WORDS, MAX_WORDS = 70, 95  # bei TTS_SPEED 1.15 ≈ 32–43 s


class Evidence(BaseModel):
    claim: str = Field(description="Eine konkrete Aussage aus dem Sprechtext (Zahl, Datum, Regel).")
    quote: str = Field(description="Wörtliches Zitat aus dem Quelltext, das die Aussage belegt.")


class KeyFact(BaseModel):
    value: str = Field(description="Kernzahl für die Grafik in Ziffern, max. 12 Zeichen, z. B. '30 Tage', '+4,2 %', '49 €'.")
    label: str = Field(description="Was die Zahl bedeutet, max. 28 Zeichen, z. B. 'Rückgabefrist'.")


class ShortScript(BaseModel):
    discard: bool = Field(description="true, wenn das Thema verworfen werden soll.")
    discard_reason: str = Field(description="Begründung beim Verwerfen, sonst leer.")
    title: str = Field(description="YouTube-Titel, max. 70 Zeichen, ohne Clickbait-Lügen.")
    overlay_title: str = Field(description="Sehr kurzer Bild-Titel, max. 35 Zeichen.")
    narration: str = Field(description="Kompletter Sprechtext, beginnt mit dem Hook-Satz.")
    description: str = Field(description="YouTube-Beschreibung, 2–4 Sätze, ohne URL.")
    hashtags: list[str] = Field(description="3–6 Hashtags, jeweils mit #.")
    evidence: list[Evidence] = Field(description="Belege für jede Zahl/jedes Datum/jede Regel.")
    key_facts: list[KeyFact] = Field(description="1–3 Kernzahlen für die Bildschirmgrafik, in Sprechreihenfolge.")
    date_label: str = Field(description="Stichtag/Frist für die Grafik, max. 22 Zeichen, z. B. 'AB 1. MÄRZ 2027' oder 'BIS ENDE 2027'; leer, wenn die Quelle keinen nennt.")


SYSTEM_PROMPT = """\
You write scripts for the faceless YouTube Shorts channel "{channel_name}". Audience: ordinary \
people in {locale}. Every short answers one question: what changes for me, when, and what do I \
have to do? Write everything viewers see or hear in language "{language}".

Hard rules, in this order of priority:
1. Use only facts that are explicitly stated in the SOURCE TEXT. No outside knowledge, no \
assumptions, no rounded or extrapolated numbers, no invented dates. If the text is vague, the \
script stays vague or the topic is discarded.
2. Discard the topic (discard=true, with a short reason) if any of these apply: no concrete \
change, deadline, price, amount or right for private individuals; purely political/diplomatic \
news; the decisive facts (what/when/who) are missing or uncertain; the text is only a teaser.
3. Every number, date, amount, deadline or rule in the narration needs an evidence entry whose \
quote is copied verbatim from the source text.

Script shape (when not discarded):
- The narration is {min_words}–{max_words} words. That is 30–45 seconds when spoken.
- The first sentence is the hook: concrete, personal, max. 12 words, e.g. a direct question or \
the most surprising concrete fact. No "In this video...".
- Then: what exactly changes, from when, who is affected, what to do. Short spoken sentences, \
numbers written as people say them, no abbreviations the voice would stumble over, no emojis, \
no URLs.
- Close with one clear takeaway sentence. Do not ask for likes or subscriptions.
- The title is factual and specific. overlay_title is a few punchy words for the screen.

On-screen graphic (shown large in the foreground, so it must be exact):
- key_facts: the 1–3 most important numbers, in the order they are spoken. value uses digits and \
units exactly as in the source (e.g. "30 Tage", "4,2 %", "49 €"); label says in a few words what it is.
- date_label: the effective date or deadline exactly as stated in the source, in capitals \
(e.g. "AB 1. MÄRZ 2027", "BIS ENDE 2027"). Empty string if the source names none. Never guess a year.
- Do not repeat the date_label as a key fact; key_facts are amounts, durations, percentages or counts. \
If the date is the only number, give one key fact that names what happens (e.g. value "Pflicht", \
label "für neue Smart-Geräte") or leave key_facts with the single date-free number that matters most.
- Both follow rule 1: only what the source text states.
"""

USER_TEMPLATE = """\
SOURCE ({source_name}): {url}
HEADLINE: {headline}

SOURCE TEXT:
<<<
{text}
>>>"""


class ScriptWriter(Protocol):
    def write(self, *, headline: str, url: str, source_name: str, text: str) -> ShortScript: ...


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def _norm(s: str) -> str:
    s = s.lower().replace("„", '"').replace("“", '"').replace("”", '"').replace("’", "'")
    return re.sub(r"\s+", " ", s).strip()


def unverified_quotes(script: ShortScript, source_text: str) -> list[str]:
    """Belege, deren Zitat nicht wörtlich im Quelltext steht – Hinweis für den Faktencheck."""
    src = _norm(source_text)
    return [e.quote for e in script.evidence if _norm(e.quote).strip('".') not in src]


def unverified_numbers(script: ShortScript, source_text: str) -> list[str]:
    """Zahlen aus Grafik (key_facts, date_label), die so nicht im Quelltext vorkommen."""
    src_numbers = set(re.findall(r"\d+(?:[.,]\d+)?", source_text))
    shown = [f.value for f in script.key_facts] + ([script.date_label] if script.date_label else [])
    return [text for text in shown
            if any(n not in src_numbers for n in re.findall(r"\d+(?:[.,]\d+)?", text))]


def validate_script(script: ShortScript) -> list[str]:
    problems: list[str] = []
    if script.discard:
        return problems
    n = word_count(script.narration)
    if not MIN_WORDS <= n <= MAX_WORDS:
        problems.append(f"Sprechtext hat {n} Wörter, erlaubt sind {MIN_WORDS}–{MAX_WORDS}.")
    if len(script.title) > 100:
        problems.append("Titel länger als 100 Zeichen (YouTube-Limit).")
    if not script.evidence:
        problems.append("Keine Belege angegeben.")
    if not 1 <= len(script.key_facts) <= 3:
        problems.append("key_facts muss 1–3 Einträge haben.")
    for f in script.key_facts:
        if len(f.value) > 14 or len(f.label) > 32:
            problems.append(f"Grafiktext zu lang: '{f.value}' / '{f.label}'.")
    if len(script.date_label) > 24:
        problems.append("date_label zu lang.")
    return problems


class ClaudeScriptWriter:
    def __init__(self, settings: Settings, channel: ChannelConfig) -> None:
        import anthropic  # lazy: Dry-Run braucht kein SDK-Setup

        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY fehlt in .env")
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=3)
        self._model = settings.anthropic_model
        self._effort = settings.anthropic_effort
        self._system = SYSTEM_PROMPT.format(
            channel_name=channel.name, locale=channel.locale_hint or channel.language,
            language=channel.language, min_words=MIN_WORDS, max_words=MAX_WORDS)

    def _call(self, user_content: str) -> ShortScript:
        kwargs: dict = dict(
            model=self._model,
            max_tokens=16000,
            system=self._system,
            messages=[{"role": "user", "content": user_content}],
            output_format=ShortScript,
        )
        if self._effort:
            kwargs["output_config"] = {"effort": self._effort}
        # Kein Refusal-Fallback (bewusst): eine Ablehnung verwirft das Thema.
        response = self._client.messages.parse(**kwargs)

        log.info("Claude: model=%s stop=%s in=%s out=%s", response.model, response.stop_reason,
                 response.usage.input_tokens, response.usage.output_tokens)
        if response.stop_reason == "refusal":
            return ShortScript(discard=True, discard_reason="Modell hat die Anfrage abgelehnt.",
                               title="", overlay_title="", narration="", description="",
                               hashtags=[], evidence=[], key_facts=[], date_label="")
        if response.stop_reason == "max_tokens" or response.parsed_output is None:
            raise RuntimeError(f"Keine gültige Antwort (stop_reason={response.stop_reason})")
        return response.parsed_output

    def write(self, *, headline: str, url: str, source_name: str, text: str) -> ShortScript:
        user = USER_TEMPLATE.format(source_name=source_name, url=url, headline=headline, text=text)
        script = self._call(user)
        problems = validate_script(script)
        if problems:  # ein Korrekturversuch
            log.info("Skript wird nachgebessert: %s", problems)
            script = self._call(user + "\n\nA previous attempt had these problems, fix them: "
                                + " ".join(problems))
            problems = validate_script(script)
            if problems:
                raise RuntimeError("Skript ungültig: " + " ".join(problems))
        return script


class SampleScriptWriter:
    """Dry-Run: liefert ein festes Beispielskript, ohne API-Aufruf."""

    def __init__(self, sample: dict) -> None:
        self._sample = sample

    def write(self, *, headline: str, url: str, source_name: str, text: str) -> ShortScript:
        return ShortScript.model_validate(self._sample)


def script_to_json(script: ShortScript) -> str:
    return json.dumps(script.model_dump(), ensure_ascii=False, indent=2)
