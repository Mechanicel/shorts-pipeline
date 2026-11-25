"""YouTube Analytics API (Scope yt-analytics.readonly): täglich Kennzahlen pro hochgeladenem Video in die DB.

Geprüft am 25.11.2026: Die API liefert views, engagedViews, averageViewDuration, averageViewPercentage, likes,
comments, shares – aber NICHT den Shorts-Wert „Angesehen vs. weggewischt“ (nur in YouTube Studio). Den trägt der
Betreiber im Webinterface von Hand ein (Spalte topics.yt_viewed_rate).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from .config import ChannelConfig
from .db import Database, Status

log = logging.getLogger(__name__)

ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
METRICS = "views,engagedViews,averageViewDuration,averageViewPercentage,likes,comments,shares"
FIELDS = ["views", "engaged_views", "avg_view_duration", "avg_view_pct", "likes", "comments", "shares"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS yt_stats (
    topic_id INTEGER NOT NULL, day TEXT NOT NULL, views INTEGER, engaged_views INTEGER, avg_view_duration REAL,
    avg_view_pct REAL, likes INTEGER, comments INTEGER, shares INTEGER, fetched_at TEXT NOT NULL,
    PRIMARY KEY (topic_id, day)
);
"""


class MissingScope(RuntimeError):
    pass


def analytics_client(channel: ChannelConfig):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    path = channel.youtube_token
    if not path.exists():
        raise RuntimeError(f"Token-Datei fehlt: {path.name}")
    creds = Credentials.from_authorized_user_file(str(path))
    if ANALYTICS_SCOPE not in (creds.scopes or []):
        raise MissingScope("Token ohne Analytics-Recht – auth_youtube.py auf dem Laptop neu ausführen "
                           "und token_de.json nach credentials/ kopieren.")
    if not creds.valid:
        creds.refresh(Request())
        path.write_text(creds.to_json(), encoding="utf-8")
        path.chmod(0o600)
    return build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)


def ensure_schema(db: Database) -> None:
    db._conn.executescript(SCHEMA)


def _published(topic) -> datetime | None:
    iso = topic.yt_published_at or topic.publish_at
    return datetime.fromisoformat(iso.replace("Z", "+00:00")) if iso else None


def fetch_stats(db: Database, channel: str, query: Callable[..., dict], today: date | None = None) -> dict:
    """query(**params) → API-Antwort (googleapiclient: reports().query(**params).execute()). Liefert Zähler."""
    ensure_schema(db)
    today = today or date.today()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    topics = [t for t in db.by_status(Status.HOCHGELADEN, channel)
              if t.youtube_id and t.yt_visibility != "gelöscht" and (p := _published(t)) and p.date() <= today]
    if not topics:
        return {"videos": 0, "rows": 0, "h72": 0}
    by_vid = {t.youtube_id: t for t in topics}
    start = min(_published(t).date() for t in topics).isoformat()
    rows = 0
    ids = list(by_vid)
    for i in range(0, len(ids), 200):  # Gesamtwerte seit Veröffentlichung, eine Zeile pro Video
        resp = query(ids="channel==MINE", startDate=start, endDate=today.isoformat(), metrics=METRICS,
                     dimensions="video", filters="video==" + ",".join(ids[i:i + 200]), maxResults=200)
        for row in resp.get("rows", []):
            vid, values = row[0], row[1:]
            t = by_vid.get(vid)
            if not t:
                continue
            db._conn.execute(
                f"INSERT OR REPLACE INTO yt_stats(topic_id, day, {', '.join(FIELDS)}, fetched_at) "
                f"VALUES (?,?,{','.join('?' * len(FIELDS))},?)", (t.id, today.isoformat(), *values, now_iso))
            rows += 1
    h72 = 0
    for t in topics:  # Aufrufe der ersten 72 h (Tag 0–2), sobald alle drei Tage in der API vorliegen
        if t.yt_views_72h:
            continue
        p = _published(t).date()
        if today < p + timedelta(days=4):  # die API hängt 1–2 Tage hinterher
            continue
        resp = query(ids="channel==MINE", startDate=p.isoformat(), endDate=(p + timedelta(days=2)).isoformat(),
                     metrics="views", dimensions="day", filters=f"video=={t.youtube_id}")
        days = resp.get("rows", [])
        if len(days) >= 3:
            db.update_fields(t.id, yt_views_72h=str(sum(int(r[1]) for r in days)))
            h72 += 1
    return {"videos": len(topics), "rows": rows, "h72": h72}


def latest_stats(db: Database) -> dict[int, dict]:
    ensure_schema(db)
    rows = db._conn.execute(
        "SELECT s.* FROM yt_stats s JOIN (SELECT topic_id, MAX(day) d FROM yt_stats GROUP BY topic_id) m "
        "ON s.topic_id=m.topic_id AND s.day=m.d")
    return {r["topic_id"]: dict(r) for r in rows}


def style_of(topic) -> str:
    return topic.style or (topic.script or {}).get("style") or "v1"


def style_comparison(db: Database, channel: str, duration_of: Callable[[str], float] | None = None) -> list[dict]:
    """Je Stil: Anzahl Videos, Ø Aufrufe nach 72 h (nur Videos mit Wert), Ø Länge. Länge wird einmalig ermittelt."""
    ensure_schema(db)
    groups: dict[str, list] = {}
    for t in db.by_status(Status.HOCHGELADEN, channel):
        if t.yt_visibility == "gelöscht":
            continue
        groups.setdefault(style_of(t), []).append(t)
    out = []
    for style, topics in sorted(groups.items()):
        views = [int(t.yt_views_72h) for t in topics if t.yt_views_72h]
        lengths = []
        for t in topics:
            secs = t.video_seconds
            if not secs and duration_of and t.video_path:
                try:
                    secs = f"{duration_of(t.video_path):.1f}"
                    db.update_fields(t.id, video_seconds=secs)
                except Exception:
                    secs = None
            if secs:
                lengths.append(float(secs))
        out.append({"style": style, "videos": len(topics), "with_72h": len(views),
                    "avg_72h": sum(views) / len(views) if views else None,
                    "avg_len": sum(lengths) / len(lengths) if lengths else None})
    return out
