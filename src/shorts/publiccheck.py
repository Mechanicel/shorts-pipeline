"""Prüft von außen (ohne Anmeldung), ob hochgeladene Videos für Zuschauer abspielbar sind.

oEmbed 200 + playabilityStatus OK  → öffentlich
LOGIN_REQUIRED "Privates Video"     → privat (oder gesperrt)
ERROR "Video nicht verfügbar"       → gelöscht/entfernt/gesperrt
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from .db import Database, Status

UA = {"User-Agent": "Mozilla/5.0", "Accept-Language": "de-DE"}


@dataclass(frozen=True)
class Visibility:
    video_id: str
    oembed: int
    playability: str
    reason: str

    @property
    def label(self) -> str:
        if self.oembed == 200 and self.playability == "OK":
            return "öffentlich"
        if self.playability == "LOGIN_REQUIRED":
            return "privat"
        if self.playability == "ERROR":
            return "nicht verfügbar"
        return f"unklar ({self.playability or self.oembed})"


def parse_playability(html: str) -> tuple[str, str]:
    m = re.search(r'"playabilityStatus":\{"status":"([A-Z_]+)"(?:,"reason":"([^"]*)")?', html)
    return (m.group(1), m.group(2) or "") if m else ("", "")


def check_video(video_id: str) -> Visibility:
    oe = httpx.get("https://www.youtube.com/oembed", timeout=20, headers=UA,
                   params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"})
    page = httpx.get(f"https://www.youtube.com/watch?v={video_id}", timeout=20, headers=UA,
                     follow_redirects=True)
    status, reason = parse_playability(page.text)
    return Visibility(video_id, oe.status_code, status, reason)


def run_check(db: Database, channel: str, extra_ids: list[str] | None = None) -> int:
    now = datetime.now(timezone.utc)
    rows = [(t.id, t.youtube_id, t.publish_at, (t.script or {}).get("title", t.title))
            for t in db.by_status(Status.HOCHGELADEN, channel) if t.youtube_id]
    rows += [(None, vid, None, "(per --id)") for vid in (extra_ids or [])]
    if not rows:
        print("Keine hochgeladenen Videos mit YouTube-ID.")
        return 0
    problems = 0
    for tid, vid, publish_at, title in rows:
        v = check_video(vid)
        due = publish_at and datetime.fromisoformat(publish_at.replace("Z", "+00:00")) <= now
        flag = ""
        if due and v.label != "öffentlich":
            flag, problems = "  ⚠️ Termin vorbei, aber nicht öffentlich", problems + 1
        elif publish_at and not due and v.label == "öffentlich":
            flag = "  (schon vor dem Termin öffentlich – von Hand veröffentlicht?)"
        when = f"Termin {publish_at}" if publish_at else "kein Termin"
        print(f"#{tid or '-':<4} {vid}  {v.label:<16} {when}  {title[:50]}{flag}")
        if v.reason and v.label != "öffentlich":
            print(f"       YouTube: {v.reason}")
    return 1 if problems else 0
