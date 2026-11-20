"""Hintergrundvideos: eigene Clips → Pixabay-Stockclips → selbst generierte Physik-Animation.

Rechtlich sauber: eigene Aufnahmen (assets/backgrounds/), Pixabay-Inhaltslizenz (heruntergeladen,
mit Quellenangabe in der Beschreibung) oder vollständig selbst erzeugte Animation. Kein fremdes Gameplay.
Stock-Material ist nur Hintergrund; Fakten stehen als eigene Grafik im Vordergrund (graphics.py).
"""
from __future__ import annotations

import colorsys
import hashlib
import json
import logging
import math
import random
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
GENERATED_VARIANTS = 6


# ------------------------------------------------------------------ eigene Clips
def local_clips(assets_dir: Path) -> list[Path]:
    d = assets_dir / "backgrounds"
    return sorted(p for p in d.glob("*") if p.suffix.lower() in VIDEO_EXT | IMAGE_EXT) if d.exists() else []


# ------------------------------------------------------------------ Pixabay
# Regeln (Pixabay-API): Suchanfragen 24 h cachen, Videos herunterladen statt hotlinken,
# max. 100 Requests/Minute, keine Massen-Downloads. Quellenangabe "Video: Pixabay" + pageURL.
PIXABAY_VIDEOS = "https://pixabay.com/api/videos/"
SEARCH_CACHE_SECONDS = 24 * 3600
MIN_REQUEST_INTERVAL = 0.7          # ≈ 85 Requests/Minute im Worst Case, real nur wenige pro Lauf
MAX_NEW_DOWNLOADS_PER_RUN = 3       # Pool wächst langsam, keine Massen-Downloads
POOL_TARGET = 15
_last_request = 0.0


@dataclass(frozen=True)
class BackgroundChoice:
    path: Path | None                # None → Farbverlauf
    credit: str | None = None        # Text für die YouTube-Beschreibung


def _throttle() -> None:
    global _last_request
    wait = _last_request + MIN_REQUEST_INTERVAL - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


def pixabay_search(api_key: str, query: str, cache_dir: Path, now: float | None = None) -> list[dict]:
    """Videosuche mit 24-h-Dateicache je Suchbegriff."""
    now = now or time.time()
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / f"search_{hashlib.sha1(query.lower().encode()).hexdigest()[:16]}.json"
    if cache.exists():
        cached = json.loads(cache.read_text(encoding="utf-8"))
        if now - cached["ts"] < SEARCH_CACHE_SECONDS:
            return cached["hits"]
    _throttle()
    resp = httpx.get(PIXABAY_VIDEOS, timeout=20, params={
        "key": api_key, "q": query[:100], "video_type": "film", "safesearch": "true",
        "order": "popular", "per_page": 20})
    resp.raise_for_status()  # Fehlermeldung enthält die URL samt Key → nie die Exception-Message loggen
    hits = resp.json().get("hits", [])
    cache.write_text(json.dumps({"ts": now, "query": query, "hits": hits}), encoding="utf-8")
    return hits


def choose_pixabay_rendition(hit: dict, min_height: int = 1080) -> dict | None:
    """Beste Variante zum Hochkant-Zuschneiden: genug Höhe für 1920 px, aber nicht unnötig groß."""
    variants = [v for v in hit.get("videos", {}).values()
                if v.get("url") and v.get("height", 0) >= min_height and v.get("width", 0) <= 3840]
    return min(variants, key=lambda v: v["height"]) if variants else None


def credit_for(meta: dict) -> str:
    return f"Video: Pixabay {meta['pageURL']}"


def fill_pixabay_pool(api_key: str, queries: list[str], cache_dir: Path,
                      target: int = POOL_TARGET, min_duration: int = 12) -> list[Path]:
    """Hält einen kleinen lokalen Clip-Pool vor; lädt pro Lauf höchstens MAX_NEW_DOWNLOADS_PER_RUN."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    have = sorted(cache_dir.glob("pixabay_*.mp4"))
    downloads = 0
    for query in queries:
        if len(have) >= target or downloads >= MAX_NEW_DOWNLOADS_PER_RUN:
            break
        try:
            hits = pixabay_search(api_key, query, cache_dir)
        except Exception as exc:
            log.warning("Pixabay-Suche '%s' fehlgeschlagen: %s", query, type(exc).__name__)
            continue
        for hit in hits:
            if len(have) >= target or downloads >= MAX_NEW_DOWNLOADS_PER_RUN:
                break
            out = cache_dir / f"pixabay_{hit['id']}.mp4"
            if out.exists() or hit.get("duration", 0) < min_duration:
                continue
            rendition = choose_pixabay_rendition(hit)
            if not rendition:
                continue
            tmp = out.with_suffix(".part")
            _throttle()
            with httpx.stream("GET", rendition["url"], timeout=120, follow_redirects=True) as r:
                r.raise_for_status()
                with tmp.open("wb") as fh:
                    for chunk in r.iter_bytes():
                        fh.write(chunk)
            tmp.rename(out)
            out.with_suffix(".json").write_text(json.dumps({
                "id": hit["id"], "pageURL": hit["pageURL"], "user": hit.get("user"),
                "tags": hit.get("tags"), "query": query}, ensure_ascii=False), encoding="utf-8")
            have.append(out)
            downloads += 1
            log.info("Pixabay-Clip geladen: %s (%s, %ss, %sx%s)", out.name, query, hit.get("duration"),
                     rendition["width"], rendition["height"])
    return sorted(have)


# ------------------------------------------------------------------ generierte Animation
@dataclass
class Ball:
    x: float
    y: float
    vx: float
    vy: float
    r: float
    hue: float
    trail: list[tuple[float, float]]


def _rgb(hue: float, light: float = 0.55) -> tuple[int, int, int]:
    r, g, b = colorsys.hls_to_rgb(hue % 1.0, light, 0.95)
    return int(r * 255), int(g * 255), int(b * 255)


def generate_clip(out: Path, *, seconds: float = 50.0, fps: int = 30, size: tuple[int, int] = (1080, 1920),
                  seed: int = 0) -> Path:
    """Physik-Animation: Bälle springen in einem rotierenden Ring, wachsen, ziehen Leuchtspuren und
    teilen sich. Schleifenfähig genug für Hintergründe, komplett selbst erzeugt."""
    from PIL import Image, ImageDraw

    rng = random.Random(seed)
    w, h = size
    s = w / 1080  # Skalierung für Tests mit kleiner Auflösung
    cx, cy, ring_r = w / 2, h * 0.47, w * 0.44
    base_hue = rng.random()
    bg = tuple(int(c * 0.10) for c in _rgb(base_hue + 0.5, 0.25))
    gravity = 1400 * s
    max_balls, trail_len = 28, 12
    sparks: list[list[float]] = []  # x, y, vx, vy, life, hue

    def new_ball(x: float, y: float) -> Ball:
        ang = rng.uniform(0, 2 * math.pi)
        speed = rng.uniform(500, 800) * s
        return Ball(x, y, math.cos(ang) * speed, math.sin(ang) * speed, rng.uniform(16, 26) * s,
                    base_hue + rng.uniform(-0.15, 0.15), [])

    balls = [new_ball(cx + dx * ring_r * 0.3, cy - ring_r * 0.3) for dx in (-1, 0, 1)]
    tmp = out.with_suffix(".part.mp4")
    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
         "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(tmp)],
        stdin=subprocess.PIPE)
    assert proc.stdin is not None
    substeps, dt = 4, 1 / fps / 4
    try:
        for frame in range(int(seconds * fps)):
            t = frame / fps
            for _ in range(substeps):
                spawned: list[Ball] = []
                for b in balls:
                    b.vy += gravity * dt
                    b.x += b.vx * dt
                    b.y += b.vy * dt
                    dx, dy = b.x - cx, b.y - cy
                    dist = math.hypot(dx, dy) or 1.0
                    if dist > ring_r - b.r:  # Abprall an der Ringwand
                        nx, ny = dx / dist, dy / dist
                        dot = b.vx * nx + b.vy * ny
                        b.vx, b.vy = (b.vx - 2 * dot * nx) * 1.01, (b.vy - 2 * dot * ny) * 1.01
                        b.x, b.y = cx + nx * (ring_r - b.r), cy + ny * (ring_r - b.r)
                        speed = math.hypot(b.vx, b.vy)
                        if speed > 1500 * s:
                            b.vx, b.vy = b.vx / speed * 1500 * s, b.vy / speed * 1500 * s
                        b.r *= 1.10
                        b.hue += 0.05
                        for _ in range(7):  # Funkenregen beim Aufprall
                            a = math.atan2(-ny, -nx) + rng.uniform(-1.1, 1.1)
                            v = rng.uniform(250, 650) * s
                            sparks.append([b.x, b.y, math.cos(a) * v, math.sin(a) * v, 1.0, b.hue])
                        if b.r > ring_r * 0.17:  # zu groß → teilt sich in zwei
                            b.r = rng.uniform(16, 22) * s
                            for _ in range(2):
                                if len(balls) + len(spawned) < max_balls:
                                    spawned.append(new_ball(b.x - nx * 50 * s, b.y - ny * 50 * s))
                balls += spawned
                for sp in sparks:
                    sp[3] += gravity * 0.5 * dt
                    sp[0] += sp[2] * dt
                    sp[1] += sp[3] * dt
                    sp[4] -= dt * 1.6
                sparks[:] = [sp for sp in sparks if sp[4] > 0][-400:]

            img = Image.new("RGB", (w, h), bg)
            d = ImageDraw.Draw(img)
            # rotierender Ring aus Segmenten in Regenbogenfarben
            seg = 72
            for i in range(seg):
                a0 = 360 * i / seg + t * 25
                d.arc([cx - ring_r - 8 * s, cy - ring_r - 8 * s, cx + ring_r + 8 * s, cy + ring_r + 8 * s],
                      a0, a0 + 360 / seg + 0.6, fill=_rgb(base_hue + i / seg, 0.5), width=int(10 * s))
            for x, y, _, _, life, hue in sparks:
                rr = 6 * s * life
                col = tuple(int(c * life + bg_c * (1 - life)) for c, bg_c in zip(_rgb(hue, 0.65), bg))
                d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=col)
            for b in balls:
                b.trail.append((b.x, b.y))
                del b.trail[:-trail_len]
                for k, (tx, ty) in enumerate(b.trail[:-1]):  # Leuchtspur
                    f = (k + 1) / len(b.trail)
                    rr = b.r * (0.35 + 0.55 * f)
                    col = tuple(int(c * f + bg_c * (1 - f)) for c, bg_c in zip(_rgb(b.hue), bg))
                    d.ellipse([tx - rr, ty - rr, tx + rr, ty + rr], fill=col)
                d.ellipse([b.x - b.r, b.y - b.r, b.x + b.r, b.y + b.r], fill=_rgb(b.hue, 0.6),
                          outline=(255, 255, 255), width=max(1, int(3 * s)))
            proc.stdin.write(img.tobytes())
        proc.stdin.close()
        if proc.wait() != 0:
            raise RuntimeError("ffmpeg konnte die Animation nicht kodieren")
    except BaseException:
        proc.kill()
        tmp.unlink(missing_ok=True)
        raise
    tmp.rename(out)
    return out


# ------------------------------------------------------------------ Auswahl
def pick_background(*, assets_dir: Path, cache_dir: Path, seed: int, pixabay_api_key: str | None,
                    queries: list[str]) -> BackgroundChoice:
    """Reihenfolge: eigene Clips → Pixabay → generierte Animation → Farbverlauf."""
    own = local_clips(assets_dir)
    if own:
        return BackgroundChoice(own[seed % len(own)])
    if pixabay_api_key:
        try:
            pool = fill_pixabay_pool(pixabay_api_key, queries, cache_dir / "pixabay")
            if pool:
                clip = pool[seed % len(pool)]
                meta = clip.with_suffix(".json")
                credit = credit_for(json.loads(meta.read_text(encoding="utf-8"))) if meta.exists() \
                    else "Video: Pixabay"
                return BackgroundChoice(clip, credit)
        except Exception as exc:
            log.warning("Pixabay nicht verfügbar (%s) – nutze generierte Animation", type(exc).__name__)
    variant = seed % GENERATED_VARIANTS
    out = cache_dir / "generated" / f"bounce_{variant}.mp4"
    if out.exists():
        return BackgroundChoice(out)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        log.info("Erzeuge Hintergrund-Animation %s (einmalig, wird gecacht) …", out.name)
        return BackgroundChoice(generate_clip(out, seed=variant))
    except Exception:
        log.exception("Animation fehlgeschlagen – nutze Farbverlauf")
        return BackgroundChoice(None)
