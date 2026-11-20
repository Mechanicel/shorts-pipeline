"""Wort-Timings (Schätzung oder Whisper) und ASS-Untertitel für das 1080x1920-Video."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

W, H = 1080, 1920
FONT = "Montserrat Black"
FONT_TITLE = "Montserrat ExtraBold"


@dataclass(frozen=True)
class WordTiming:
    word: str
    start: float
    end: float


# --------------------------------------------------------------------------- timings
def split_words(text: str) -> list[str]:
    return re.findall(r"\S+", text)


def _weight(word: str) -> float:
    """Sprechdauer-Gewicht: Silben-Näherung über Zeichenzahl + Pause nach Satzzeichen."""
    letters = len(re.sub(r"\W", "", word)) or 1
    w = 0.6 + letters * 0.18
    if re.search(r"\d", word):  # Zahlen werden lang gesprochen ("zweitausendsechsundzwanzig")
        w += 0.25 * len(re.sub(r"\D", "", word))
    if word.endswith((".", "!", "?", ":")):
        w += 1.4
    elif word.endswith((",", ";", "–")):
        w += 0.6
    return w


def estimate_timings(words: list[str], start: float, end: float) -> list[WordTiming]:
    """Verteilt Wörter proportional zum Gewicht auf [start, end]."""
    if not words:
        return []
    weights = [_weight(w) for w in words]
    total = sum(weights)
    span = max(end - start, 0.1)
    t = start
    out: list[WordTiming] = []
    for word, wt in zip(words, weights):
        dur = span * wt / total
        out.append(WordTiming(word, round(t, 3), round(t + dur, 3)))
        t += dur
    return out


def whisper_timings(audio: Path, words: list[str], api_key: str, language: str,
                    duration: float) -> list[WordTiming]:
    """Echte Wort-Zeitstempel per OpenAI-Transkription; Anzeige bleibt der Skripttext."""
    from openai import OpenAI

    client = OpenAI(api_key=api_key, max_retries=3)
    with audio.open("rb") as f:
        tr = client.audio.transcriptions.create(
            model="whisper-1", file=f, language=language, response_format="verbose_json",
            timestamp_granularities=["word"])
    ww = list(getattr(tr, "words", None) or [])
    if not ww:
        log.warning("Whisper lieferte keine Wort-Zeitstempel – nutze Schätzung")
        return estimate_timings(words, 0.0, duration)
    if len(ww) == len(words):
        return [WordTiming(w, float(x.start), float(x.end)) for w, x in zip(words, ww)]
    # Anzahl weicht ab (z. B. Zahlen als mehrere Wörter): Sprechfenster übernehmen, darin schätzen
    log.info("Whisper: %d vs. %d Wörter – schätze innerhalb des Sprechfensters", len(ww), len(words))
    return estimate_timings(words, float(ww[0].start), float(ww[-1].end))


# --------------------------------------------------------------------------- chunks
def chunk_words(timings: list[WordTiming], max_words: int = 3, max_chars: int = 18) -> list[list[WordTiming]]:
    """Gruppiert Wörter zu kurzen Untertitel-Häppchen, Umbruch an Satzzeichen."""
    chunks: list[list[WordTiming]] = []
    cur: list[WordTiming] = []
    for wt in timings:
        candidate = " ".join(w.word for w in cur + [wt])
        if cur and (len(cur) >= max_words or len(candidate) > max_chars):
            chunks.append(cur)
            cur = []
        cur.append(wt)
        if wt.word.endswith((".", "!", "?", ",", ":", ";")):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


# --------------------------------------------------------------------------- ASS
def ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", "\\N")


def ass_time(t: float) -> str:
    t = max(t, 0.0)
    cs = int(round(t * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


HEADER = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{FONT},104,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,0,0,1,7,4,5,80,80,0,1
Style: Title,{FONT_TITLE},56,&H00FFFFFF,&H00FFFFFF,&H00000000,&H003A1F0B,0,0,0,0,100,100,0,0,3,14,0,8,120,120,300,1
Style: Source,{FONT_TITLE},54,&H00FFFFFF,&H00FFFFFF,&H00000000,&H003A1F0B,0,0,0,0,100,100,0,0,3,16,0,2,90,90,560,1
Style: Outro,{FONT},64,&H003FD2FF,&H003FD2FF,&H00000000,&H96000000,0,0,0,0,100,100,0,0,1,6,3,5,90,90,0,1
Style: Badge,{FONT_TITLE},40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H000000C8,0,0,0,0,100,100,0,0,3,12,0,7,50,50,80,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

TITLE_DELAY = 1.2  # Sekunden, bevor die Titelbox erscheint
HIGHLIGHT = "&H003FD2FF&"  # Kanal-Gelb #FFD23F (ASS: BGR)


def build_ass(*, timings: list[WordTiming], title: str, source_label: str, outro: str,
              total_duration: float, end_card_seconds: float = 3.5, badge: str | None = None) -> str:
    lines: list[str] = [HEADER]

    def ev(start: float, end: float, style: str, text: str, layer: int = 0) -> None:
        lines.append(f"Dialogue: {layer},{ass_time(start)},{ass_time(end)},{style},,0,0,0,,{text}")

    end_card_start = max(total_duration - end_card_seconds, 0.0)

    # Titel: oben, das ganze Video über (mit kurzem Einblenden)
    # Titel klein und erst nach TITLE_DELAY: Frame 0 gehört der Kernzahl
    if total_duration > TITLE_DELAY:
        ev(TITLE_DELAY, total_duration, "Title", r"{\fad(300,0)}" + ass_escape(title.upper()), layer=1)

    # Untertitel: Wortgruppen, aktives Wort gelb hervorgehoben, leichter Pop-Effekt pro Gruppe
    for chunk in chunk_words(timings):
        chunk_end = min(chunk[-1].end, end_card_start) if (outro and end_card_seconds > 0) else chunk[-1].end
        if chunk[0].start >= chunk_end:
            continue
        for i, wt in enumerate(chunk):
            seg_end = min(chunk[i + 1].start if i + 1 < len(chunk) else chunk_end, chunk_end)
            if seg_end <= wt.start:
                continue
            parts = []
            for j, w in enumerate(chunk):
                word = ass_escape(w.word.upper())
                parts.append(f"{{\\c{HIGHLIGHT}}}{word}{{\\c&H00FFFFFF&}}" if j == i else word)
            pop = r"{\fscx92\fscy92\t(0,80,\fscx100\fscy100)}" if i == 0 else ""
            ev(wt.start, seg_end, "Caption", r"{\pos(540,1290)}" + pop + " ".join(parts), layer=2)

    # Endkarte (optional): Quellenhinweis + Outro. Standard: keine – Quelle steht in der Beschreibung,
    # das Video endet direkt nach dem letzten Wort und loopt sauber.
    if end_card_seconds > 0:
        ev(end_card_start, total_duration, "Source", r"{\fad(200,0)}" + ass_escape(f"Quelle: {source_label}"), layer=3)
    if outro and end_card_seconds > 0:
        ev(end_card_start + 0.3, total_duration, "Outro", r"{\pos(540,1180)\fad(200,0)}" + ass_escape(outro), layer=3)

    if badge:
        ev(0, total_duration, "Badge", ass_escape(badge), layer=4)
    return "\n".join(lines) + "\n"
