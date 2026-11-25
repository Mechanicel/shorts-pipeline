"""Stil v3 „Merkliste“: eine dichte Infotafel mit 3–6 Punkten, ab Frame 0 komplett sichtbar, 8–12 s.

Grundlage: Ausreißer-Recherche 25.11. (kurze Shorts mit Text-/Infotafel ab Frame 0 und CTA laufen überdurchschnittlich).
Die Tafel trägt die Information; die Stimme leitet nur an (Hook + „Hier alles auf einen Blick.“).
v2 bleibt unverändert; welcher Stil ein Thema bekommt, entscheidet STYLE_MODE (pipeline.choose_style).
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from pydantic import BaseModel, Field

from .graphics import FONT_BLACK, FONT_XBOLD, NAVY, WHITE, YELLOW
from .script import HOOK_MAX_WORDS, _DATE_OPENING, _DU, _norm, hook_of, word_count

log = logging.getLogger(__name__)

STYLE_V2 = "v2-hook-frame0"
STYLE_V3 = "v3-merkliste"
W, H = 1080, 1920
FPS = 30
ICON_FONT = Path(__file__).resolve().parents[2] / "assets" / "icons" / "lucide.ttf"

# Festes Icon-Set (Lucide, ISC) – Schlüssel = Lucide-Name, Wert = Codepoint in lucide.ttf (lucide-static 1.68.0)
ICONS: dict[str, int] = {
    "euro": 57528, "banknote": 57426, "piggy-bank": 57658, "wallet": 57860, "coins": 57495, "hand-coins": 58808,
    "receipt": 58323, "percent": 57650, "calendar": 57443, "clock": 57479, "file-text": 57548, "landmark": 57914,
    "scale": 57874, "gavel": 57568, "heart-pulse": 58222, "pill": 58301, "stethoscope": 58097, "hospital": 58840,
    "baby": 58062, "users": 57764, "user": 57759, "house": 57589, "car": 57813, "fuel": 58031, "zap": 57780,
    "flame": 57554, "bus": 57812, "train-front": 58630, "ticket": 57871, "briefcase": 57442,
    "graduation-cap": 57908, "shopping-cart": 57692, "smartphone": 57699, "credit-card": 57514, "id-card": 58903,
    "mail": 57615, "package": 57641, "shield-check": 57855, "trending-up": 57745, "trending-down": 57744,
    "circle-alert": 57463, "info": 57593, "leaf": 58078, "accessibility": 58007,
}
IconName = Literal[
    "euro", "banknote", "piggy-bank", "wallet", "coins", "hand-coins", "receipt", "percent", "calendar", "clock",
    "file-text", "landmark", "scale", "gavel", "heart-pulse", "pill", "stethoscope", "hospital", "baby", "users",
    "user", "house", "car", "fuel", "zap", "flame", "bus", "train-front", "ticket", "briefcase", "graduation-cap",
    "shopping-cart", "smartphone", "credit-card", "id-card", "mail", "package", "shield-check", "trending-up",
    "trending-down", "circle-alert", "info", "leaf", "accessibility"]
Status = Literal["in Kraft", "beschlossen", "Entwurf", "geplant"]
STATUS_COLORS = {"in Kraft": (46, 160, 90), "beschlossen": (46, 160, 90), "Entwurf": YELLOW, "geplant": (120, 132, 150)}
STATUS_TEXT = {"in Kraft": WHITE, "beschlossen": WHITE, "Entwurf": NAVY, "geplant": WHITE}

MIN_ITEMS, MAX_ITEMS = 3, 6
MIN_WORDS_V3, MAX_WORDS_V3 = 18, 28
LIMITS = {"headline": 32, "label": 28, "old_value": 14, "new_value": 16, "note": 40}
READ_SECONDS = 3.0
MIN_SECONDS, MAX_SECONDS = 8.0, 12.0
MIN_FONT = 42


# ------------------------------------------------------------------ Skript-Modell
class MerkItem(BaseModel):
    icon: IconName
    label: str = Field(description="Was sich ändert, max. 28 Zeichen, z. B. 'Mindestlohn pro Stunde'.")
    old_value: str = Field(description="Bisheriger Wert wie in der Quelle, max. 14 Zeichen; leer, wenn keiner genannt.")
    new_value: str = Field(description="Neuer Wert/Regel wie in der Quelle, max. 16 Zeichen, z. B. '14,60 €'.")
    note: str = Field(description="Optionaler Zusatz, max. 40 Zeichen, z. B. 'für alle unter 25'; sonst leer.")
    status: Status = Field(description="Rechtsstatus exakt nach Quelle: 'soll'/'plant' → geplant oder Entwurf.")
    evidence: str = Field(description="Wörtliches Zitat aus dem Quelltext, das Wert und Status belegt.")


class MerklisteScript(BaseModel):
    discard: bool = Field(description="true, wenn das Thema ganz verworfen werden soll (gleiche Regeln wie v2).")
    discard_reason: str = Field(description="Begründung beim Verwerfen, sonst leer.")
    list_possible: bool = Field(description="false, wenn der Quelltext weniger als 3 listenfähige Fakten hat.")
    list_impossible_reason: str = Field(description="Warum keine Liste möglich ist, sonst leer.")
    title: str = Field(description="YouTube-Titel: offizieller Suchbegriff vorn, dann Anzahl und Nutzen, max. 90 Zeichen.")
    headline: str = Field(description="Kopfzeile der Tafel, max. 32 Zeichen, Großbuchstaben erlaubt.")
    items: list[MerkItem] = Field(description="3–6 Punkte in sinnvoller Reihenfolge.")
    date_label: str = Field(description="Stichtag/Frist, max. 22 Zeichen, z. B. 'AB 1. JANUAR 2027'; leer, wenn keiner.")
    source_label: str = Field(description="Kurze Quellenangabe für die Tafel, z. B. 'Quelle: Bundesregierung'.")
    cta: str = Field(description="Ein Satz 'Schick das jemandem, der …' passend zur betroffenen Gruppe.")
    narration: str = Field(description="18–28 Wörter: Hook-Satz, dann kurze Anleitung wie 'Hier alles auf einen Blick.'")
    description: str = Field(description="1–2 Sätze Einleitung für die YouTube-Beschreibung, ohne URL.")
    hashtags: list[str] = Field(description="3–6 Hashtags mit #.")
    background_query: str = Field(description="1–2 englische Wörter für ein ruhiges Stock-Video passend zum Thema, "
                                              "z. B. 'pharmacy', 'pay slip', 'electricity meter'.")


V3_SYSTEM = """\
You write "Merkliste" Shorts for the faceless YouTube channel "{channel_name}" (audience: ordinary people in \
{locale}). One dense info board carries all information; the voice only introduces it. Write everything viewers \
see or hear in language "{language}".

Hard rules (same as for all our videos):
1. Use only facts explicitly stated in the SOURCE TEXT. No outside knowledge, no rounding, no invented dates.
2. Discard the topic (discard=true) if the core change is already covered in ALREADY COVERED and the source adds \
nothing substantial (discard_reason starts with "Dublette von #<id>"), if it is old news (in force for more than \
4 weeks before TODAY), purely political/diplomatic, or the decisive facts are missing.
3. Every item needs an evidence quote copied verbatim from the source text that supports its value AND its status.
4. Status exactly as the source states it: in force now → "in Kraft"; finally passed but not yet in force → \
"beschlossen"; cabinet draft/bill → "Entwurf"; "soll", "plant", "geplant", proposals → "geplant" or "Entwurf". \
Never show a plan or draft as "beschlossen" or "in Kraft". If one source item bundles several changes with \
different status, split them.
5. If the source has fewer than 3 separate facts that fit a list (amount, limit, date, rule, right), set \
list_possible=false with a short reason and leave the rest minimal – the topic will be done in our other format.

Board:
- headline: max. 32 characters, the date or the topic, e.g. "AB 1. JANUAR 2027 GILT NEU", "DAS ÄNDERT SICH BEIM \
KINDERGELD".
- Every item must fit the headline. Under a headline about a date or "what changes" ("AB 1. JANUAR 2027 GILT \
NEU", "DAS ÄNDERT SICH …"), list only changes that take effect then – never pad the list with rules that are \
already in force or unchanged. If that leaves fewer than 3 items, set list_possible=false.
- items: 3–6. label max. 28 chars, old_value max. 14 (empty if the source names none), new_value max. 16, note \
max. 40 (optional, empty otherwise). Values use digits and units exactly as in the source ("14,60 €", "556 €").
- icon: pick the best fitting name from the allowed list.
- date_label: effective date or deadline exactly as in the source, in capitals; empty if none. Never guess a year.
- source_label: "Quelle: " plus the institution, e.g. "Quelle: Bundesregierung".
- cta: one sentence starting with "Schick das jemandem, der" addressed to the affected group. Never ask to \
subscribe or like.

Voice:
- narration: {min_words}–{max_words} words. First sentence = hook: max. {hook_max} words, talks to the viewer \
as "du", names a concrete gain/loss or opens a curiosity gap, does NOT start with a date phrase, keeps the legal \
status ("soll" stays "soll"). Then a short pointer to the board, e.g. "Hier alles auf einen Blick." No numbers \
beyond what the board shows, no outro.

Metadata:
- title: official search term first, then count and benefit, e.g. "Ab 1. Januar 2027: 5 Änderungen bei Minijob, \
Rente & Co." Promise nothing the board does not show.
- description: 1–2 sentences intro; the full list is appended automatically.
- background_query: 1–2 English words for a calm stock video matching the topic."""


# ------------------------------------------------------------------ Prüfungen
_STATUS_PLAN = re.compile(r"\b(soll|sollen|sollte|geplant\w*|plant|planen|entwurf\w*|vorschlag\w*|vorgesehen)\b",
                          re.IGNORECASE)


def text_limit_problems(s: MerklisteScript) -> list[str]:
    problems = []
    if len(s.headline) > LIMITS["headline"]:
        problems.append(f"headline hat {len(s.headline)} Zeichen, max. {LIMITS['headline']}.")
    if not MIN_ITEMS <= len(s.items) <= MAX_ITEMS:
        problems.append(f"{len(s.items)} Punkte, erlaubt {MIN_ITEMS}–{MAX_ITEMS}.")
    for i, it in enumerate(s.items, 1):
        for key in ("label", "old_value", "new_value", "note"):
            value = getattr(it, key)
            if len(value) > LIMITS[key]:
                problems.append(f"Punkt {i}: {key} hat {len(value)} Zeichen, max. {LIMITS[key]}.")
        if not it.new_value.strip():
            problems.append(f"Punkt {i}: new_value fehlt.")
        if not it.evidence.strip():
            problems.append(f"Punkt {i}: Beleg fehlt.")
    return problems


def narration_problems(narration: str) -> list[str]:
    problems = []
    n = word_count(narration)
    if not MIN_WORDS_V3 <= n <= MAX_WORDS_V3:
        problems.append(f"Sprechtext hat {n} Wörter, erlaubt {MIN_WORDS_V3}–{MAX_WORDS_V3}.")
    hook = hook_of(narration)
    if word_count(hook) > HOOK_MAX_WORDS:
        problems.append(f"Erster Satz hat {word_count(hook)} Wörter, max. {HOOK_MAX_WORDS}.")
    if not _DU.search(hook):
        problems.append("Erster Satz spricht nicht mit 'du' an.")
    if _DATE_OPENING.match(hook):
        problems.append("Erster Satz beginnt mit einer Datums-/Zeitangabe.")
    return problems


def cta_problems(cta: str) -> list[str]:
    problems = []
    if not cta.lower().startswith("schick das jemandem"):
        problems.append("CTA muss mit 'Schick das jemandem' beginnen.")
    if re.search(r"abonn|like|folg", cta, re.IGNORECASE):
        problems.append("CTA darf nicht zum Abonnieren/Liken/Folgen auffordern.")
    return problems


def status_warnings(items: list[dict], headline: str = "") -> list[str]:
    """Status, der nicht eindeutig zum Beleg passt (Faktencheck-Hinweis in Freigabe und Telegram)."""
    out = []
    if re.search(r"\bneu\b|ändert|ab \d", headline, re.IGNORECASE):
        unchanged = [it.get("label", "") for it in items if it.get("status") == "in Kraft" and not it.get("old_value")]
        if unchanged:
            out.append("Kopfzeile verspricht Neues, aber ohne alten Wert und schon in Kraft: " + ", ".join(unchanged))
    for i, it in enumerate(items, 1):
        quote, status = it.get("evidence", ""), it.get("status", "")
        if status in ("in Kraft", "beschlossen") and _STATUS_PLAN.search(quote):
            out.append(f"Punkt {i} „{it.get('label')}“: Status {status}, Beleg spricht von Plan/Entwurf.")
    return out


def unverified_items(items: list[dict], source_text: str) -> list[str]:
    """Belege, die nicht wörtlich im Quelltext stehen, und Werte, deren Zahlen nicht im Quelltext vorkommen."""
    src = _norm(source_text)
    numbers = set(re.findall(r"\d+(?:[.,]\d+)?", source_text))
    out = []
    for i, it in enumerate(items, 1):
        if _norm(it.get("evidence", "")).strip('".') not in src:
            out.append(f"Punkt {i}: Zitat nicht wörtlich im Quelltext")
        for key in ("old_value", "new_value"):
            missing = [n for n in re.findall(r"\d+(?:[.,]\d+)?", it.get(key, "")) if n not in numbers]
            if missing:
                out.append(f"Punkt {i}: Zahl {', '.join(missing)} ({key}) nicht im Quelltext")
    return out


def validate_v3(s: MerklisteScript) -> list[str]:
    if s.discard or not s.list_possible:
        return []
    problems = text_limit_problems(s) + narration_problems(s.narration) + cta_problems(s.cta)
    if len(s.title) > 100:
        problems.append("Titel länger als 100 Zeichen.")
    if not problems:
        problems += layout_problems(s.model_dump())
    return problems


# ------------------------------------------------------------------ Tafel-Layout
PANEL_X0, PANEL_X1 = 40, 1040
CONTENT_X0, CONTENT_X1 = 70, 950          # rechts Platz für YouTubes Buttons
TOP, BOTTOM = 190, 1580                   # oben Status-/Suchleiste, unten Titel/Kanalzeile von YouTube
ICON_COL = 96


def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if font.getlength(trial) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


@dataclass
class Block:
    kind: str
    y: int
    h: int
    data: dict


def _fit_size(text: str, path: Path, start: int, width: int, max_lines: int) -> tuple[int, list[str]] | None:
    size = start
    while size >= MIN_FONT:
        lines = _wrap(text, _font(path, size), width)
        if len(lines) <= max_lines and all(_font(path, size).getlength(ln) <= width for ln in lines):
            return size, lines
        size -= 2
    return None


def layout(script: dict) -> tuple[list[Block], list[str]]:
    """Berechnet die Tafel. Liefert Blöcke und Probleme (Text passt nicht, Tafel zu hoch)."""
    problems: list[str] = []
    blocks: list[Block] = []
    y = TOP + 50
    inner = CONTENT_X1 - CONTENT_X0

    head = _fit_size(script.get("headline", "").upper(), FONT_BLACK, 68, inner, 1) or \
        _fit_size(script.get("headline", "").upper(), FONT_BLACK, 60, inner, 2)
    if head is None:
        problems.append("headline passt nicht in 2 Zeilen (min. 42 px).")
        head = (MIN_FONT, [script.get("headline", "").upper()])
    h = len(head[1]) * int(head[0] * 1.15)
    blocks.append(Block("headline", y, h, {"size": head[0], "lines": head[1]}))
    y += h + 24
    date = script.get("date_label", "").upper()
    if date and " ".join(date.split()) not in " ".join(script.get("headline", "").upper().split()):
        blocks.append(Block("pill", y, 78, {"text": date}))
        y += 78 + 30

    text_w = CONTENT_X1 - CONTENT_X0 - ICON_COL
    for i, it in enumerate(script.get("items", []), 1):
        label = _fit_size(it.get("label", ""), FONT_XBOLD, 46, text_w, 1)
        if label is None:
            problems.append(f"Punkt {i}: label passt nicht in eine Zeile.")
            label = (MIN_FONT, [it.get("label", "")])
        note_lines = _wrap(it["note"], _font(FONT_XBOLD, MIN_FONT), text_w) if it.get("note") else []
        if len(note_lines) > 2:
            problems.append(f"Punkt {i}: note braucht mehr als 2 Zeilen.")
        chip_w = int(_font(FONT_XBOLD, MIN_FONT).getlength(it.get("status", ""))) + 40
        old = it.get("old_value", "")
        f_old, f_new = _font(FONT_XBOLD, 44), _font(FONT_BLACK, 56)
        values_w = (f_old.getlength(old) + f_old.getlength("  →  ") if old else 0) + f_new.getlength(it.get("new_value", ""))
        label_w = _font(FONT_XBOLD, label[0]).getlength(label[1][0])
        if values_w > text_w:
            problems.append(f"Punkt {i}: alter und neuer Wert passen nicht in eine Zeile.")
        # Status-Chip: hinter die Werte, sonst hinter das Label, sonst in eine eigene Zeile
        chip = "values" if values_w + chip_w + 24 <= text_w else ("label" if label_w + chip_w + 24 <= text_w else "own")
        h = 12 + int(label[0] * 1.2) + 6 + 70 + (68 if chip == "own" else 0) + len(note_lines) * 52 + 10
        blocks.append(Block("item", y, h, {"label": label, "notes": note_lines, "item": it, "index": i - 1,
                                           "chip": chip}))
        y += h + 6

    y += 12
    cta_lines = _wrap(script.get("cta", ""), _font(FONT_XBOLD, MIN_FONT + 2), inner)
    if len(cta_lines) > 3:
        problems.append("cta braucht mehr als 3 Zeilen.")
    h = len(cta_lines) * 54
    blocks.append(Block("cta", y, h, {"lines": cta_lines}))
    y += h + 14
    blocks.append(Block("source", y, 52, {"text": script.get("source_label", "")}))
    y += 52 + 40
    if y > BOTTOM:
        problems.append(f"Tafel um {y - BOTTOM} px zu hoch ({y - TOP} von {BOTTOM - TOP} px). Platzbedarf: Punkt ohne "
                        "Notiz ca. 150 px, jede Notizzeile 52 px, Status in eigener Zeile 68 px (passiert bei langen "
                        "Werten), CTA-Zeile 54 px – Notizen streichen, Werte kürzen oder einen Punkt weglassen.")
    return blocks, problems


def layout_problems(script: dict) -> list[str]:
    return layout(script)[1]


def _gradient() -> Image.Image:
    img = Image.new("RGB", (W, H), NAVY)
    d = ImageDraw.Draw(img)
    for yy in range(H):
        t = yy / H
        d.line([(0, yy), (W, yy)], fill=(int(11 + 14 * t), int(31 + 30 * t), int(58 + 40 * t)))
    return img


def background_still(video: Path | None, out_dir: Path) -> Image.Image:
    """Ein Standbild aus dem Hintergrund-Video, auf 9:16 zugeschnitten, unscharf und abgedunkelt.
    Statisch, damit der letzte Frame exakt dem ersten entspricht (sauberer Loop)."""
    if video is None:
        return _gradient()
    still = out_dir / "bg_still.png"
    try:
        if video.suffix.lower() in (".png", ".jpg", ".jpeg"):
            src = Image.open(video)
        else:
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "1", "-i", str(video), "-frames:v", "1",
                            str(still)], check=True, capture_output=True)
            src = Image.open(still)
    except Exception:
        log.warning("Standbild aus %s fehlgeschlagen – nutze Navy-Verlauf", video.name)
        return _gradient()
    src = src.convert("RGB")
    scale = max(W / src.width, H / src.height)
    src = src.resize((int(src.width * scale) + 1, int(src.height * scale) + 1))
    left, top = (src.width - W) // 2, (src.height - H) // 2
    img = src.crop((left, top, left + W, top + H)).filter(ImageFilter.GaussianBlur(18))
    dark = Image.new("RGB", (W, H), (5, 12, 24))
    return Image.blend(img, dark, 0.55)


def render_board(script: dict, background: Image.Image, out: Path, highlight: int | None = None) -> Path:
    """Komplette Tafel (1080×1920) als Bild. highlight = Index der hervorgehobenen Zeile (Animation)."""
    blocks, _ = layout(script)
    img = background.copy().convert("RGBA")
    over = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    bottom = blocks[-1].y + blocks[-1].h + 34
    d.rounded_rectangle([PANEL_X0, TOP, PANEL_X1, bottom], radius=44, fill=NAVY + (232,))
    d.rounded_rectangle([PANEL_X0, TOP, PANEL_X1, TOP + 14], radius=7, fill=YELLOW + (255,))
    icon_font = _font(ICON_FONT, 60)
    for b in blocks:
        if b.kind == "headline":
            f = _font(FONT_BLACK, b.data["size"])
            for n, line in enumerate(b.data["lines"]):
                d.text((CONTENT_X0, b.y + n * int(b.data["size"] * 1.15)), line, font=f, fill=WHITE)
        elif b.kind == "pill":
            f = _font(FONT_XBOLD, 44)
            w = f.getlength(b.data["text"]) + 56
            d.rounded_rectangle([CONTENT_X0, b.y, CONTENT_X0 + w, b.y + 78], radius=39, fill=YELLOW + (255,))
            d.text((CONTENT_X0 + w / 2, b.y + 39), b.data["text"], font=f, fill=NAVY, anchor="mm")
        elif b.kind == "item":
            it = b.data["item"]
            if highlight == b.data["index"]:
                d.rounded_rectangle([CONTENT_X0 - 14, b.y, CONTENT_X1 + 14, b.y + b.h], radius=22,
                                    fill=(40, 70, 115, 255))
                d.rounded_rectangle([CONTENT_X0 - 14, b.y, CONTENT_X0 - 4, b.y + b.h], radius=5, fill=YELLOW + (255,))
            else:
                d.line([CONTENT_X0, b.y + b.h + 5, CONTENT_X1, b.y + b.h + 5], fill=(255, 255, 255, 40), width=2)
            code = ICONS.get(it.get("icon", ""), ICONS["info"])
            d.text((CONTENT_X0 + 30, b.y + 12 + 30), chr(code), font=icon_font, fill=YELLOW, anchor="mm")
            x = CONTENT_X0 + ICON_COL
            size, lines = b.data["label"]
            yy = b.y + 12
            d.text((x, yy), lines[0], font=_font(FONT_XBOLD, size), fill=WHITE)
            label_y = yy - 4
            yy += int(size * 1.2) + 6
            f_old, f_new = _font(FONT_XBOLD, 44), _font(FONT_BLACK, 56)
            vx = x
            if it.get("old_value"):
                d.text((vx, yy + 10), it["old_value"], font=f_old, fill=(150, 160, 178))
                ow = f_old.getlength(it["old_value"])
                d.line([vx, yy + 38, vx + ow, yy + 38], fill=(150, 160, 178), width=4)  # durchgestrichen
                vx += ow
                d.text((vx, yy + 10), "  →  ", font=f_old, fill=(150, 160, 178))
                vx += f_old.getlength("  →  ")
            d.text((vx, yy), it.get("new_value", ""), font=f_new, fill=YELLOW)
            chip_font = _font(FONT_XBOLD, MIN_FONT)
            status = it.get("status", "")
            cw = chip_font.getlength(status) + 40
            yy += 70
            cy = {"values": yy - 66, "label": label_y, "own": yy}[b.data["chip"]]
            cx0 = x if b.data["chip"] == "own" else CONTENT_X1 - cw
            d.rounded_rectangle([cx0, cy, cx0 + cw, cy + 60], radius=30,
                                fill=STATUS_COLORS.get(status, (120, 132, 150)) + (255,))
            d.text((cx0 + cw / 2, cy + 30), status, font=chip_font, fill=STATUS_TEXT.get(status, WHITE), anchor="mm")
            if b.data["chip"] == "own":
                yy += 68
            for line in b.data["notes"]:
                d.text((x, yy), line, font=_font(FONT_XBOLD, MIN_FONT), fill=(200, 208, 222))
                yy += 52
        elif b.kind == "cta":
            for n, line in enumerate(b.data["lines"]):
                d.text((CONTENT_X0, b.y + n * 54), line, font=_font(FONT_XBOLD, MIN_FONT + 2), fill=YELLOW)
        elif b.kind == "source":
            d.text((CONTENT_X0, b.y), b.data["text"], font=_font(FONT_XBOLD, MIN_FONT), fill=(170, 180, 198))
    Image.alpha_composite(img, over).convert("RGB").save(out)
    return out


# ------------------------------------------------------------------ Video
def video_length(speech: float) -> float:
    """Sprechzeit + Lesezeit, hart begrenzt auf 8–12 s."""
    return round(min(max(speech + READ_SECONDS, MIN_SECONDS), MAX_SECONDS), 2)


def highlight_windows(n_items: int, start: float, total: float) -> list[tuple[int, float, float]]:
    """Zeilen nacheinander kurz hervorheben, nach dem Hook bis kurz vor Schluss; Anfang und Ende ohne Hervorhebung
    (letzter Frame = erster Frame → sauberer Loop)."""
    end = total - 0.6
    if n_items == 0 or end - start < 0.4 * n_items:
        return []
    span = (end - start) / n_items
    return [(i, round(start + i * span, 2), round(start + (i + 1) * span - 0.08, 2)) for i in range(n_items)]


def render_v3(script: dict, audio: Path, speech: float, hook_end: float, background: Path | None,
              out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    total = video_length(speech)
    bg = background_still(background, out_dir)
    base = render_board(script, bg, out_dir / "board.png")
    windows = highlight_windows(len(script.get("items", [])), max(hook_end, 1.0), total)
    hl = [(render_board(script, bg, out_dir / f"board_hl{i}.png", highlight=i), a, b) for i, a, b in windows]
    inputs = ["-loop", "1", "-framerate", str(FPS), "-i", str(base.resolve()), "-i", str(audio.resolve())]
    for path, _, _ in hl:
        inputs += ["-loop", "1", "-framerate", str(FPS), "-i", str(path.resolve())]
    parts, last = ["[0:v]format=yuv420p[b0]"], "b0"
    for n, (_, a, b) in enumerate(hl):
        parts.append(f"[{last}][{n + 2}:v]overlay=0:0:enable='between(t,{a},{b})'[b{n + 1}]")
        last = f"b{n + 1}"
    parts.append(f"[{last}]format=yuv420p[v]")
    parts.append("[1:a]loudnorm=I=-14:TP=-1.5:LRA=11,apad[a]")
    out = (out_dir / "short.mp4").resolve()
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs, "-filter_complex", ";".join(parts),
           "-map", "[v]", "-map", "[a]", "-t", f"{total:.2f}", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
           "-pix_fmt", "yuv420p", "-r", str(FPS), "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
           "-movflags", "+faststart", str(out)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg (v3) fehlgeschlagen: {res.stderr.strip()[-1500:]}")
    log.info("Merkliste gerendert: %s (%.1fs, %d Punkte)", out, total, len(script.get("items", [])))
    return out


# ------------------------------------------------------------------ Metadaten
STATUS_EMOJI = {"in Kraft": "✅", "beschlossen": "✅", "Entwurf": "📝", "geplant": "🕒"}


def description_v3(script: dict, source_url: str, credit: str | None) -> str:
    lines = [script.get("description", "").strip(), ""]
    if script.get("date_label"):
        lines.append(script["date_label"])
    for it in script.get("items", []):
        change = f"{it['old_value']} → {it['new_value']}" if it.get("old_value") else it.get("new_value", "")
        note = f" ({it['note']})" if it.get("note") else ""
        lines.append(f"{STATUS_EMOJI.get(it.get('status'), '•')} {it.get('label')}: {change}{note} – {it.get('status')}")
    lines += ["", f"Quelle: {source_url}"] + ([credit] if credit else []) + ["",
              "Alle Angaben ohne Gewähr, Stand bei Veröffentlichung."]
    return "\n".join(lines)


def script_json_v3(s: MerklisteScript) -> str:
    data = s.model_dump()
    data["style"] = STYLE_V3
    return json.dumps(data, ensure_ascii=False, indent=2)
