"""Prüft von außen (ohne Anmeldung), ob hochgeladene Videos für Zuschauer abspielbar sind,
und schreibt das Ergebnis in die Datenbank (yt_visibility, yt_published_at, yt_checked_at).

oEmbed 200 + playabilityStatus OK  → öffentlich
LOGIN_REQUIRED "Privates Video"     → privat (geplant oder gesperrt)
ERROR "Video nicht verfügbar"       → gelöscht (oder von YouTube entfernt)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from .db import Database, Status
from .result import OK, WARN, plural, report

UA = {"User-Agent": "Mozilla/5.0", "Accept-Language": "de-DE"}
PUBLIC, PRIVATE, DELETED = "öffentlich", "privat", "gelöscht"


@dataclass(frozen=True)
class Visibility:
    video_id: str
    oembed: int
    playability: str
    reason: str
    publish_date: str | None = None

    @property
    def label(self) -> str:
        if self.oembed == 200 and self.playability == "OK":
            return PUBLIC
        if self.playability == "LOGIN_REQUIRED":
            return PRIVATE
        if self.playability == "ERROR":
            return DELETED
        return f"unklar ({self.playability or self.oembed})"


def parse_playability(html: str) -> tuple[str, str]:
    m = re.search(r'"playabilityStatus":\{"status":"([A-Z_]+)"(?:,"reason":"([^"]*)")?', html)
    return (m.group(1), m.group(2) or "") if m else ("", "")


def parse_publish_date(html: str) -> str | None:
    """'publishDate' der Watch-Seite → UTC-ISO (nur bei öffentlichen Videos vorhanden)."""
    m = re.search(r'"publishDate":"([^"]+)"', html)
    if not m:
        return None
    try:
        return datetime.fromisoformat(m.group(1)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def check_video(video_id: str) -> Visibility:
    oe = httpx.get("https://www.youtube.com/oembed", timeout=20, headers=UA,
                   params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"})
    page = httpx.get(f"https://www.youtube.com/watch?v={video_id}", timeout=20, headers=UA,
                     follow_redirects=True)
    status, reason = parse_playability(page.text)
    return Visibility(video_id, oe.status_code, status, reason, parse_publish_date(page.text))


def run_check(db: Database, channel: str, extra_ids: list[str] | None = None, checker=check_video) -> int:
    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    topics = [t for t in db.by_status(Status.HOCHGELADEN, channel) if t.youtube_id]
    rows = [(t.id, t.youtube_id, t.publish_at, (t.script or {}).get("title", t.title)) for t in topics]
    rows += [(None, vid, None, "(per --id)") for vid in (extra_ids or [])]
    if not rows:
        report(WARN, "Nichts zu prüfen – keine hochgeladenen Videos mit YouTube-ID.")
        return 0
    findings, counts, known_deleted = [], {}, 0
    previous = {t.id: t for t in topics}
    for tid, vid, publish_at, title in rows:
        v = checker(vid)
        counts[v.label] = counts.get(v.label, 0) + 1
        before = previous.get(tid)
        was_deleted = bool(before and (before.yt_deleted_at or before.yt_visibility == DELETED))
        if tid is not None:
            fields = {"yt_visibility": v.label, "yt_checked_at": now_iso}
            if v.publish_date:
                fields["yt_published_at"] = v.publish_date
            if v.label == DELETED and not (before and before.yt_deleted_at):
                fields["yt_deleted_at"] = now_iso
            db.update_fields(tid, **fields)
            db.log_visibility(tid, v.label, now_iso)
        due = publish_at and datetime.fromisoformat(publish_at.replace("Z", "+00:00")) <= now
        flag = ""
        # Warnung nur bei neuen Befunden – eine schon einmal erkannte Löschung meldet sich nicht jeden Tag neu
        if v.label == DELETED and was_deleted:
            flag = "  (gelöscht, bereits bekannt)"
            known_deleted += 1
        elif v.label == DELETED:
            flag = "  ⚠️ neu gelöscht"
            findings.append(f"#{tid or vid} neu gelöscht")
        elif due and v.label != PUBLIC:
            flag = "  ⚠️ Termin vorbei, aber nicht öffentlich"
            findings.append(f"#{tid or vid} nicht öffentlich trotz Termin")
        elif v.label == PRIVATE and not publish_at and tid is not None:
            flag = "  ⚠️ unerwartet privat (kein Termin)"
            findings.append(f"#{tid} unerwartet privat")
        elif v.label == PUBLIC and publish_at and not due:
            flag = "  (schon vor dem Termin öffentlich – von Hand veröffentlicht)"
        when = f"Termin {publish_at}" if publish_at else "ohne Termin"
        print(f"#{tid or '-':<4} {vid}  {v.label:<10} {when}  {title}{flag}")
        if v.reason and v.label != PUBLIC:
            print(f"       YouTube: {v.reason}")
    overview = ", ".join(f"{n} {label}" for label, n in counts.items())
    if known_deleted:
        overview += f"; davon {known_deleted} bekannt gelöscht"
    if findings:
        report(WARN, f"{plural(len(rows), 'Video', 'Videos')} geprüft ({overview}) – " + "; ".join(findings))
    else:
        report(OK, f"{plural(len(rows), 'Video', 'Videos')} geprüft ({overview}) – alles wie erwartet.")
    return 0  # Befunde sind kein technischer Fehler
