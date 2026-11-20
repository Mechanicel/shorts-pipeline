"""Faktenkarten im Kanal-Design (Navy #0B1F3A, Gelb #FFD23F, Montserrat) als transparente PNGs.

Die Karten stehen groß im Vordergrund; Stock-/Animationsmaterial ist nur Hintergrund.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

NAVY = (11, 31, 58)
YELLOW = (255, 210, 63)
WHITE = (255, 255, 255)
FONT_DIR = Path("/usr/share/fonts/opentype/montserrat")
FONT_BLACK = FONT_DIR / "Montserrat-Black.otf"
FONT_XBOLD = FONT_DIR / "Montserrat-ExtraBold.otf"

CARD_WIDTH = 920
CARD_Y = 470          # obere Kante im 1080x1920-Frame (unter dem Titel, über den Untertiteln)
FADE = 0.25


@dataclass(frozen=True)
class CardTiming:
    path: Path
    start: float
    end: float


def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


def _fit(text: str, path: Path, max_size: int, min_size: int, max_width: int) -> ImageFont.FreeTypeFont:
    """Größte Schrift, bei der der Text in max_width passt."""
    size = max_size
    while size > min_size:
        f = _font(path, size)
        if f.getlength(text) <= max_width:
            return f
        size -= 6
    return _font(path, min_size)


def render_fact_card(value: str, label: str, date_label: str, out: Path, width: int = CARD_WIDTH) -> Path:
    pad = 50
    inner = width - 2 * pad
    f_value = _fit(value.upper(), FONT_BLACK, 200, 90, inner)
    f_label = _fit(label, FONT_XBOLD, 60, 36, inner)
    f_date = _font(FONT_XBOLD, 46)

    # Höhen bestimmen
    vb = f_value.getbbox(value.upper())
    lb = f_label.getbbox(label)
    value_h, label_h = vb[3] - vb[1], lb[3] - lb[1]
    pill_h = 80 if date_label else 0
    height = pad + (pill_h + 36 if date_label else 0) + value_h + 34 + label_h + pad + 12

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, width - 1, height - 1], radius=40, fill=NAVY + (242,))
    d.rounded_rectangle([0, 0, width - 1, 14], radius=7, fill=YELLOW + (255,))  # Akzentleiste

    y = pad + 12
    if date_label:
        text = date_label.upper()
        tw = f_date.getlength(text)
        pw = tw + 64
        x0 = (width - pw) / 2
        d.rounded_rectangle([x0, y, x0 + pw, y + pill_h], radius=pill_h // 2, fill=YELLOW + (255,))
        d.text((width / 2, y + pill_h / 2), text, font=f_date, fill=NAVY, anchor="mm")
        y += pill_h + 36
    d.text((width / 2, y - vb[1]), value.upper(), font=f_value, fill=YELLOW, anchor="ma")
    y += value_h + 34
    d.text((width / 2, y - lb[1]), label, font=f_label, fill=WHITE, anchor="ma")
    img.save(out)
    return out


def _same_info(value: str, date_label: str) -> bool:
    norm = lambda s: " ".join(s.upper().replace(".", " ").split())  # noqa: E731
    v, d = norm(value), norm(date_label)
    return bool(v and d) and (v in d or d in v)


def build_cards(key_facts: list[dict], date_label: str, out_dir: Path, speech_start: float,
                speech_end: float) -> list[CardTiming]:
    """Eine Karte pro Kernzahl, gleichmäßig über die Sprechzeit verteilt (Reihenfolge = Sprechreihenfolge)."""
    facts = [f for f in key_facts if f.get("value")][:3]
    if not facts:
        if not date_label:
            return []
        facts = [{"value": date_label, "label": ""}]
        date_label = ""
    span = max(speech_end - speech_start, 1.0) / len(facts)
    cards = []
    for i, f in enumerate(facts):
        # Datums-Feld weglassen, wenn die Kernzahl das Datum schon zeigt ("BIS ENDE 2027" / "Ende 2027")
        pill = "" if _same_info(f["value"], date_label) else date_label
        path = render_fact_card(f["value"], f.get("label", ""), pill, out_dir / f"card_{i}.png")
        cards.append(CardTiming(path, speech_start + i * span, speech_start + (i + 1) * span))
    return cards
