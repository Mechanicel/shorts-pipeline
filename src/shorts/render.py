"""Video-Rendering mit ffmpeg: 1080x1920, Hintergrund, eingebrannte ASS-Untertitel, Audio."""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .graphics import CARD_Y, FADE, CardTiming, build_cards
from .subtitles import H, W, WordTiming, build_ass

log = logging.getLogger(__name__)

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
TAIL_SECONDS = 2.8  # Endkarte nach der Sprachspur (Quelle + Outro), Untertitel enden vorher
FPS = 30


@dataclass(frozen=True)
class RenderJob:
    audio: Path
    timings: list[WordTiming]
    title: str
    source_url: str
    outro: str
    out_dir: Path
    background: Path | None = None
    badge: str | None = None
    key_facts: list[dict] = field(default_factory=list)
    date_label: str = ""


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True).stdout
    return float(json.loads(out)["format"]["duration"])


def source_label(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _background_input(bg: Path | None) -> list[str]:
    if bg is None:  # generierter, ruhiger Farbverlauf
        return ["-f", "lavfi", "-i",
                f"gradients=s={W}x{H}:r={FPS}:c0=0x14325e:c1=0x1f6f8b:c2=0x2b2d6e:n=3:speed=0.004"]
    if bg.suffix.lower() in IMAGE_EXT:
        return ["-loop", "1", "-framerate", str(FPS), "-i", str(bg.resolve())]
    return ["-stream_loop", "-1", "-i", str(bg.resolve())]


def build_filter(cards: list[CardTiming], ass_name: str) -> str:
    """Hintergrund (auf 9:16 zugeschnitten, abgedunkelt, weich) → Faktenkarten → Untertitel."""
    parts = [f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={FPS},"
             f"eq=brightness=-0.16:saturation=0.85,gblur=sigma=3[b0]"]
    for i, c in enumerate(cards):
        idx = 2 + i  # 0 = Hintergrund, 1 = Audio
        parts.append(f"[{idx}:v]format=rgba,fade=t=in:st={c.start:.2f}:d={FADE}:alpha=1,"
                     f"fade=t=out:st={max(c.end - FADE, c.start):.2f}:d={FADE}:alpha=1[c{i}]")
        parts.append(f"[b{i}][c{i}]overlay=x=(W-w)/2:y={CARD_Y}:shortest=0[b{i + 1}]")
    parts.append(f"[b{len(cards)}]subtitles=filename={ass_name}:fontsdir=/usr/share/fonts[v]")
    parts.append("[1:a]loudnorm=I=-14:TP=-1.5:LRA=11,apad[a]")
    return ";".join(parts)


def build_ffmpeg_cmd(job: RenderJob, ass_path: Path, out_path: Path, total: float,
                     cards: list[CardTiming] | None = None) -> list[str]:
    cards = cards or []
    card_inputs = [a for c in cards for a in ("-loop", "1", "-framerate", str(FPS), "-i", str(c.path.resolve()))]
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        *_background_input(job.background),
        "-i", str(job.audio.resolve()),
        *card_inputs,
        "-filter_complex", build_filter(cards, ass_path.name),
        "-map", "[v]", "-map", "[a]",
        "-t", f"{total:.2f}",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-profile:v", "high", "-r", str(FPS),
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(out_path),
    ]


def render(job: RenderJob) -> Path:
    job.out_dir.mkdir(parents=True, exist_ok=True)
    speech = probe_duration(job.audio)
    total = round(speech + TAIL_SECONDS, 2)
    ass = build_ass(timings=job.timings, title=job.title, source_label=source_label(job.source_url),
                    outro=job.outro, total_duration=total, badge=job.badge,
                    end_card_seconds=TAIL_SECONDS)
    ass_path = job.out_dir / "subs.ass"
    ass_path.write_text(ass, encoding="utf-8")
    out_path = (job.out_dir / "short.mp4").resolve()
    speech_start = job.timings[0].start if job.timings else 0.0
    speech_end = job.timings[-1].end if job.timings else speech
    cards = build_cards(job.key_facts, job.date_label, job.out_dir, speech_start + 0.3, speech_end)
    cmd = build_ffmpeg_cmd(job, ass_path, out_path, total, cards)
    log.info("Rendere %s (%.1fs, Hintergrund: %s)", out_path, total, job.background or "Verlauf")
    # cwd = out_dir, damit der subtitles-Filter den Dateinamen ohne Escaping findet
    res = subprocess.run(cmd, cwd=job.out_dir, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg fehlgeschlagen: {res.stderr.strip()[-2000:]}")
    if total > 60:
        log.warning("Video ist %.1fs lang – länger als angepeilt (30–45 s)", total)
    return out_path
