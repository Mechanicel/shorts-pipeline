"""Video-Rendering mit ffmpeg: 1080x1920, Hintergrund, eingebrannte ASS-Untertitel, Audio."""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

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


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True).stdout
    return float(json.loads(out)["format"]["duration"])


def source_label(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def pick_background(assets_dir: Path, seed: int) -> Path | None:
    bg_dir = assets_dir / "backgrounds"
    files = sorted(p for p in bg_dir.glob("*") if p.suffix.lower() in VIDEO_EXT | IMAGE_EXT) if bg_dir.exists() else []
    return files[seed % len(files)] if files else None


def _background_input(bg: Path | None) -> list[str]:
    if bg is None:  # generierter, ruhiger Farbverlauf
        return ["-f", "lavfi", "-i",
                f"gradients=s={W}x{H}:r={FPS}:c0=0x14325e:c1=0x1f6f8b:c2=0x2b2d6e:n=3:speed=0.004"]
    if bg.suffix.lower() in IMAGE_EXT:
        return ["-loop", "1", "-framerate", str(FPS), "-i", str(bg.resolve())]
    return ["-stream_loop", "-1", "-i", str(bg.resolve())]


def build_ffmpeg_cmd(job: RenderJob, ass_path: Path, out_path: Path, total: float) -> list[str]:
    vf = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={FPS},"
          f"eq=brightness=-0.10:saturation=0.9,"
          f"subtitles=filename={ass_path.name}:fontsdir=/usr/share/fonts[v]")
    af = "[1:a]loudnorm=I=-14:TP=-1.5:LRA=11,apad[a]"
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        *_background_input(job.background),
        "-i", str(job.audio.resolve()),
        "-filter_complex", f"{vf};{af}",
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
    cmd = build_ffmpeg_cmd(job, ass_path, out_path, total)
    log.info("Rendere %s (%.1fs, Hintergrund: %s)", out_path, total, job.background or "Verlauf")
    # cwd = out_dir, damit der subtitles-Filter den Dateinamen ohne Escaping findet
    res = subprocess.run(cmd, cwd=job.out_dir, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg fehlgeschlagen: {res.stderr.strip()[-2000:]}")
    if total > 60:
        log.warning("Video ist %.1fs lang – länger als angepeilt (30–45 s)", total)
    return out_path
