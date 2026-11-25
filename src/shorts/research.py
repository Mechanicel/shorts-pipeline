"""Ausreißer-Recherche: Shorts kleiner deutscher Kanäle, die weit über dem Niveau ihres Kanals laufen.

Nur offizielle YouTube Data API v3 (API-Key `YT_API_KEY`), kein Download fremder Videos, kein Scraping.
Ablauf: search.list je Suchbegriff → videos.list (Details) → Vorfilter → channels.list (Abos, Uploads-Playlist)
→ Kanal-Median der letzten Uploads (playlistItems.list + videos.list, 7 Tage gecacht) → Score und Filter
→ DB (research_videos + research_snapshots) → Kennzahlen + Claude-Auswertung → Bericht/CSV/Telegram.
"""
from __future__ import annotations

import csv
import json
import logging
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import yaml
from pydantic import BaseModel, Field

from .config import APP_ROOT, Settings, load_channels
from .costs import claude_cost
from .db import Database

log = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/youtube/v3/"
RESEARCH_FILE = APP_ROOT / "config" / "research.yaml"
QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded"}


# ------------------------------------------------------------------ Konfiguration
@dataclass(frozen=True)
class ResearchConfig:
    search_terms: list[str]
    max_search_calls: int = 40
    published_within_days: int = 30
    short_max_seconds: int = 180
    max_subscribers: int = 30000
    min_views: int = 20000
    max_age_days: int = 30
    min_outlier_score: float = 5.0
    channel_median_uploads: int = 20
    min_channel_videos: int = 3
    min_channel_median: float = 300   # winzige Mediane (Clip-Kanäle) erzeugen absurde Scores
    channel_cache_days: int = 7
    top_n: int = 40
    model: str = "claude-sonnet-5-5"
    effort: str = "medium"
    own_channel_ids: list[str] = field(default_factory=list)
    own_channel_titles: list[str] = field(default_factory=list)
    blocked_channels: list[str] = field(default_factory=list)


def load_research_config(path: Path = RESEARCH_FILE) -> ResearchConfig:
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    f, a = raw.get("filters", {}), raw.get("analysis", {})
    try:
        own_titles = [c.name for c in load_channels().values()]
    except Exception:  # pragma: no cover - fehlt nur in Tests ohne channels.yaml
        own_titles = []
    return ResearchConfig(
        search_terms=[str(t) for t in raw.get("search_terms", [])],
        max_search_calls=int(raw.get("max_search_calls", 40)),
        published_within_days=int(raw.get("published_within_days", 30)),
        short_max_seconds=int(raw.get("short_max_seconds", 180)),
        max_subscribers=int(f.get("max_subscribers", 30000)),
        min_views=int(f.get("min_views", 20000)),
        max_age_days=int(f.get("max_age_days", 30)),
        min_outlier_score=float(f.get("min_outlier_score", 5)),
        channel_median_uploads=int(raw.get("channel_median_uploads", 20)),
        min_channel_videos=int(raw.get("min_channel_videos", 3)),
        min_channel_median=float(f.get("min_channel_median", raw.get("min_channel_median", 300))),
        channel_cache_days=int(raw.get("channel_cache_days", 7)),
        top_n=int(a.get("top_n", 40)),
        model=str(a.get("model", "claude-sonnet-5-5")),
        effort=str(a.get("effort", "medium")),
        own_channel_ids=list(raw.get("own_channel_ids") or []),
        own_channel_titles=own_titles,
        blocked_channels=list(raw.get("blocked_channels") or []),
    )


# ------------------------------------------------------------------ YouTube-API (mockbar)
class QuotaExceeded(RuntimeError):
    """Google meldet ein erschöpftes Kontingent – weitere Aufrufe sind sinnlos."""


class YouTubeAPI:
    """Dünner Wrapper um die REST-API. `get` ist austauschbar (Tests). Der Key steht in der URL →
    Fehlermeldungen von httpx nie loggen, nur Status und Grund."""

    def __init__(self, api_key: str, get: Callable[[str, dict], dict] | None = None) -> None:
        self._key = api_key
        self._get = get or self._http_get
        self.search_calls = 0
        self.units = 0  # Einheiten aus dem 10.000er-Topf (videos/channels/playlistItems)

    def _http_get(self, endpoint: str, params: dict) -> dict:
        import httpx

        resp = httpx.get(API_BASE + endpoint, params={**params, "key": self._key}, timeout=30)
        if resp.status_code >= 400:
            reason = ""
            try:
                errors = resp.json().get("error", {}).get("errors", [])
                reason = errors[0].get("reason", "") if errors else ""
            except ValueError:
                pass
            if reason in QUOTA_REASONS:
                raise QuotaExceeded(reason)
            raise RuntimeError(f"YouTube-API {endpoint}: HTTP {resp.status_code} {reason}".strip())
        return resp.json()

    def call(self, endpoint: str, params: dict) -> dict:
        if endpoint == "search":
            self.search_calls += 1
        else:
            self.units += 1
        return self._get(endpoint, params)

    def search(self, term: str, published_after: str) -> list[str]:
        data = self.call("search", {
            "part": "id", "q": term, "type": "video", "regionCode": "DE", "relevanceLanguage": "de",
            "videoDuration": "short", "publishedAfter": published_after, "order": "viewCount", "maxResults": 50})
        return [it["id"]["videoId"] for it in data.get("items", []) if it.get("id", {}).get("videoId")]

    def videos(self, ids: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(ids), 50):
            batch = ids[i:i + 50]
            if batch:
                out += self.call("videos", {"part": "snippet,contentDetails,statistics",
                                            "id": ",".join(batch)}).get("items", [])
        return out

    def channels(self, ids: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(ids), 50):
            batch = ids[i:i + 50]
            if batch:
                out += self.call("channels", {"part": "snippet,statistics,contentDetails",
                                              "id": ",".join(batch)}).get("items", [])
        return out

    def playlist_video_ids(self, playlist_id: str, n: int) -> list[str]:
        data = self.call("playlistItems", {"part": "contentDetails", "playlistId": playlist_id, "maxResults": n})
        return [it["contentDetails"]["videoId"] for it in data.get("items", [])
                if it.get("contentDetails", {}).get("videoId")]


# ------------------------------------------------------------------ Kennzahlen (reine Funktionen)
_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def parse_duration(iso: str) -> int | None:
    """ISO-8601-Dauer (PT1M5S) → Sekunden; None bei unbekanntem Format (z. B. Livestream 'P0D' → 0)."""
    m = _DURATION.match(iso or "")
    if not m:
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def is_short(seconds: int | None, max_seconds: int = 180) -> bool:
    return seconds is not None and 0 < seconds <= max_seconds


def channel_median(view_counts: list[int]) -> float | None:
    return float(statistics.median(view_counts)) if view_counts else None


def outlier_score(views: int, median: float | None) -> float | None:
    if not median or median <= 0:
        return None
    return views / median


def age_days(published_at: str, now: datetime) -> float:
    published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    return max((now - published).total_seconds() / 86400, 1 / 24)  # mind. 1 Stunde, sonst explodiert /Tag


def is_blocked_channel(title: str, channel_id: str, cfg: ResearchConfig) -> str | None:
    """Grund, wenn der Kanal ausgeschlossen ist (eigener Kanal oder große Medienmarke)."""
    if channel_id in cfg.own_channel_ids:
        return "eigener Kanal"
    if any(title.strip().lower() == own.strip().lower() for own in cfg.own_channel_titles):
        return "eigener Kanal"
    for brand in cfg.blocked_channels:
        if re.search(rf"(?<!\w){re.escape(brand)}(?!\w)", title, re.IGNORECASE):
            return f"Medienmarke ({brand})"
    return None


def _int(stats: dict, key: str) -> int | None:
    value = stats.get(key)
    return int(value) if value not in (None, "") else None


@dataclass
class Candidate:
    video_id: str
    channel_id: str
    channel_title: str
    title: str
    description: str
    tags: list[str]
    duration_s: int
    published_at: str
    views: int
    likes: int | None
    comments: int | None
    search_term: str
    language: str = ""  # defaultAudioLanguage bzw. defaultLanguage, falls der Kanal sie setzt
    subs: int | None = None
    subs_hidden: bool = False
    channel_median: float | None = None
    channel_videos: int = 0
    score: float | None = None
    views_per_day: float = 0.0
    views_per_sub: float | None = None


def candidate_from_video(item: dict, term: str) -> Candidate | None:
    sn, cd, st = item.get("snippet", {}), item.get("contentDetails", {}), item.get("statistics", {})
    views = _int(st, "viewCount")
    duration = parse_duration(cd.get("duration", ""))
    if views is None or duration is None or not sn.get("publishedAt"):
        return None
    return Candidate(
        video_id=item["id"], channel_id=sn.get("channelId", ""), channel_title=sn.get("channelTitle", ""),
        title=sn.get("title", ""), description=sn.get("description", ""), tags=list(sn.get("tags", []) or []),
        duration_s=duration, published_at=sn["publishedAt"], views=views,
        likes=_int(st, "likeCount"), comments=_int(st, "commentCount"), search_term=term,
        language=(sn.get("defaultAudioLanguage") or sn.get("defaultLanguage") or "").lower())


_LATIN = re.compile(r"[A-Za-zÄÖÜäöüß]")
_LETTER = re.compile(r"[^\W\d_]")


def is_german(c: Candidate) -> bool:
    """relevanceLanguage ist für YouTube nur ein Hinweis → selbst prüfen: gesetzte Sprache oder Schrift im Titel."""
    if c.language:
        return c.language.startswith("de")
    letters = _LETTER.findall(c.title)
    return not letters or len(_LATIN.findall(c.title)) / len(letters) >= 0.7


def prefilter_reason(c: Candidate, cfg: ResearchConfig, now: datetime) -> str | None:
    """Filter, die ohne Kanaldaten gehen (sparen channels/playlistItems-Aufrufe)."""
    if not is_short(c.duration_s, cfg.short_max_seconds):
        return "kein Short"
    if c.views < cfg.min_views:
        return "zu wenig Aufrufe"
    if age_days(c.published_at, now) > cfg.max_age_days:
        return "zu alt"
    if not is_german(c):
        return "andere Sprache"
    return is_blocked_channel(c.channel_title, c.channel_id, cfg)


def final_filter_reason(c: Candidate, cfg: ResearchConfig) -> str | None:
    if not c.subs_hidden and c.subs is not None and c.subs > cfg.max_subscribers:
        return "Kanal zu groß"
    if c.channel_videos < cfg.min_channel_videos or c.score is None:
        return "kein belastbarer Kanal-Median"
    if (c.channel_median or 0) < cfg.min_channel_median:
        return "Kanal-Median zu klein"
    if c.score < cfg.min_outlier_score:
        return "kein Ausreißer"
    return None


def compute_metrics(c: Candidate, now: datetime) -> None:
    c.score = outlier_score(c.views, c.channel_median)
    c.views_per_day = c.views / age_days(c.published_at, now)
    c.views_per_sub = (c.views / c.subs) if c.subs else None


# ------------------------------------------------------------------ Datenbank
SCHEMA = """
CREATE TABLE IF NOT EXISTS research_channels (
    channel_id TEXT PRIMARY KEY, title TEXT, subs INTEGER, subs_hidden INTEGER, uploads_playlist TEXT,
    median_views REAL, n_videos INTEGER, fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_videos (
    video_id TEXT PRIMARY KEY, channel_id TEXT, channel_title TEXT, title TEXT, description TEXT, tags TEXT,
    duration_s INTEGER, published_at TEXT, views INTEGER, likes INTEGER, comments INTEGER, subs INTEGER,
    subs_hidden INTEGER, channel_median REAL, score REAL, views_per_day REAL, views_per_sub REAL,
    search_term TEXT, first_seen TEXT NOT NULL, fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, video_id TEXT NOT NULL, fetched_at TEXT NOT NULL, views INTEGER,
    likes INTEGER, comments INTEGER, subs INTEGER, channel_median REAL, score REAL, search_term TEXT
);
CREATE INDEX IF NOT EXISTS idx_research_snap ON research_snapshots(video_id, fetched_at);
CREATE TABLE IF NOT EXISTS research_reviews (
    video_id TEXT PRIMARY KEY, face TEXT, voice TEXT, first_frame TEXT, hook_type TEXT, music TEXT,
    notes TEXT, reviewed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT, terms INTEGER,
    search_calls INTEGER, units INTEGER, candidates INTEGER, outliers INTEGER, quota_stop TEXT, report_path TEXT
);
"""

REVIEW_FIELDS = {  # Feld → erlaubte Werte (Formular im Webinterface)
    "face": ["ja", "nein"],
    "voice": ["echt", "KI", "keine"],
    "first_frame": ["Person", "Text-Tafel", "Stock", "Grafik", "Screenshot", "Dokument"],
    "hook_type": ["Zahl", "Frage", "Warnung", "Behauptung", "Geschichte"],
    "music": ["ja", "nein"],
}
MIN_REVIEWS_FOR_ANALYSIS = 15


class ResearchStore:
    def __init__(self, db: Database) -> None:
        self.conn = db._conn
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(research_videos)")}
        for col in ("relevant", "relevant_reason"):  # Themen-Vorauswahl (Haiku), nachträglich ergänzt
            if col not in cols:
                self.conn.execute(f"ALTER TABLE research_videos ADD COLUMN {col} TEXT")

    def now(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Kanal-Cache
    def cached_channel(self, channel_id: str, max_age_days: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM research_channels WHERE channel_id=?", (channel_id,)).fetchone()
        if not row:
            return None
        fetched = datetime.fromisoformat(row["fetched_at"])
        return dict(row) if datetime.now(timezone.utc) - fetched < timedelta(days=max_age_days) else None

    def save_channel(self, channel_id: str, title: str, subs: int | None, hidden: bool, playlist: str,
                     median: float | None, n_videos: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO research_channels VALUES (?,?,?,?,?,?,?,?)",
            (channel_id, title, subs, int(hidden), playlist, median, n_videos, self.now()))

    # Videos + Snapshots
    def save_video(self, c: Candidate) -> None:
        now = self.now()
        self.conn.execute(
            """INSERT INTO research_videos(video_id, channel_id, channel_title, title, description, tags, duration_s,
                 published_at, views, likes, comments, subs, subs_hidden, channel_median, score, views_per_day,
                 views_per_sub, search_term, first_seen, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(video_id) DO UPDATE SET title=excluded.title, description=excluded.description,
                 tags=excluded.tags, views=excluded.views, likes=excluded.likes, comments=excluded.comments,
                 subs=excluded.subs, subs_hidden=excluded.subs_hidden, channel_median=excluded.channel_median,
                 score=excluded.score, views_per_day=excluded.views_per_day, views_per_sub=excluded.views_per_sub,
                 fetched_at=excluded.fetched_at""",
            (c.video_id, c.channel_id, c.channel_title, c.title, c.description, json.dumps(c.tags, ensure_ascii=False),
             c.duration_s, c.published_at, c.views, c.likes, c.comments, c.subs, int(c.subs_hidden),
             c.channel_median, c.score, c.views_per_day, c.views_per_sub, c.search_term, now, now))
        self.snapshot(c, now)

    def snapshot(self, c: Candidate, now: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO research_snapshots(video_id,fetched_at,views,likes,comments,subs,channel_median,score,"
            "search_term) VALUES (?,?,?,?,?,?,?,?,?)",
            (c.video_id, now or self.now(), c.views, c.likes, c.comments, c.subs, c.channel_median, c.score,
             c.search_term))

    def known_video_ids(self, since_days: int) -> list[str]:
        since = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
        return [r[0] for r in self.conn.execute(
            "SELECT video_id FROM research_videos WHERE published_at >= ?", (since,))]

    def outliers(self, order: str = "score", limit: int = 500) -> list[dict]:
        cols = {"score", "views", "views_per_day", "subs", "duration_s", "published_at", "channel_title"}
        order = order if order in cols else "score"
        direction = "ASC" if order in ("channel_title", "duration_s") else "DESC"
        rows = self.conn.execute(
            f"""SELECT v.*, r.face, r.voice, r.first_frame, r.hook_type, r.music, r.notes, r.reviewed_at,
                   (SELECT MIN(views) FROM research_snapshots s WHERE s.video_id=v.video_id) AS first_views,
                   (SELECT COUNT(*) FROM research_snapshots s WHERE s.video_id=v.video_id) AS n_snapshots
                FROM research_videos v LEFT JOIN research_reviews r USING(video_id)
                ORDER BY {order} {direction} LIMIT ?""", (limit,))
        return [dict(r) for r in rows]

    def unclassified(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT video_id, title, description, tags, channel_title FROM research_videos WHERE relevant IS NULL")]

    def set_relevance(self, video_id: str, relevant: bool, reason: str) -> None:
        self.conn.execute("UPDATE research_videos SET relevant=?, relevant_reason=? WHERE video_id=?",
                          ("ja" if relevant else "nein", reason[:200], video_id))

    def drop_small_median(self, min_median: float) -> int:
        """Bestehende Ausreißer, die nach dem Mindest-Median nicht mehr zählen, entfernen (Snapshots bleiben)."""
        return self.conn.execute("DELETE FROM research_videos WHERE COALESCE(channel_median, 0) < ?",
                                 (min_median,)).rowcount

    def save_review(self, video_id: str, values: dict[str, str]) -> None:
        clean = {k: (values.get(k) if values.get(k) in allowed else None) for k, allowed in REVIEW_FIELDS.items()}
        notes = (values.get("notes") or "").strip()[:1000]
        self.conn.execute("INSERT OR REPLACE INTO research_reviews VALUES (?,?,?,?,?,?,?,?)",
                          (video_id, clean["face"], clean["voice"], clean["first_frame"], clean["hook_type"],
                           clean["music"], notes, self.now()))

    def reviewed(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT v.video_id, v.title, v.channel_title, v.duration_s, v.views, v.score, r.* "
            f"FROM research_reviews r JOIN research_videos v USING(video_id) WHERE {self.REVIEWABLE} "
            "ORDER BY v.score DESC")
        return [dict(r) for r in rows]

    # Eine Regel für Kopfzeile UND Auswertung: nur Sichtungen zu Videos, die noch im Bestand und nicht
    # als Politik/Satire aussortiert sind, zählen als „auswertbar“.
    REVIEWABLE = "COALESCE(v.relevant, '') != 'nein'"

    def review_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM research_reviews r JOIN research_videos v USING(video_id) "
                                 f"WHERE {self.REVIEWABLE}").fetchone()[0]

    def get_outlier(self, video_id: str) -> dict | None:
        rows = [r for r in self.outliers(limit=2000) if r["video_id"] == video_id]
        return rows[0] if rows else None

    def start_run(self, terms: int) -> int:
        return self.conn.execute("INSERT INTO research_runs(started_at, terms) VALUES (?,?)",
                                 (self.now(), terms)).lastrowid

    def finish_run(self, run_id: int, **fields) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE research_runs SET finished_at=?, {sets} WHERE id=?",
                          (self.now(), *fields.values(), run_id))

    def last_run(self) -> dict | None:
        row = self.conn.execute("SELECT * FROM research_runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None


# ------------------------------------------------------------------ Recherche-Lauf
@dataclass
class ResearchResult:
    outliers: list[Candidate]
    search_calls: int
    units: int
    terms_searched: int
    candidates: int  # nach Vorfilter (Short, Aufrufe, Alter, Marke)
    quota_stop: str | None = None
    skipped: dict[str, int] = field(default_factory=dict)
    note: str = ""  # z. B. „Neuauswertung ohne neue Suche“


def _channel_stats(api: YouTubeAPI, store: ResearchStore, channel_ids: list[str], cfg: ResearchConfig
                   ) -> dict[str, dict]:
    """Abos + Median je Kanal; aus dem Cache, wenn jünger als channel_cache_days."""
    result: dict[str, dict] = {}
    missing = []
    for cid in channel_ids:
        cached = store.cached_channel(cid, cfg.channel_cache_days)
        if cached:
            result[cid] = cached
        else:
            missing.append(cid)
    for ch in api.channels(missing):
        st, cd = ch.get("statistics", {}), ch.get("contentDetails", {})
        hidden = bool(st.get("hiddenSubscriberCount"))
        subs = None if hidden else _int(st, "subscriberCount")
        playlist = cd.get("relatedPlaylists", {}).get("uploads", "")
        info = {"channel_id": ch["id"], "title": ch.get("snippet", {}).get("title", ""), "subs": subs,
                "subs_hidden": int(hidden), "uploads_playlist": playlist, "median_views": None, "n_videos": 0}
        if not hidden and subs is not None and subs > cfg.max_subscribers:
            pass  # zu groß – Median sparen wir uns
        elif playlist:
            ids = api.playlist_video_ids(playlist, cfg.channel_median_uploads)
            views = [v for v in (_int(it.get("statistics", {}), "viewCount") for it in api.videos(ids))
                     if v is not None]
            info["median_views"], info["n_videos"] = channel_median(views), len(views)
        store.save_channel(ch["id"], info["title"], subs, hidden, playlist, info["median_views"], info["n_videos"])
        result[ch["id"]] = info
    return result


def run_research(api: YouTubeAPI, store: ResearchStore, cfg: ResearchConfig, terms: list[str] | None = None,
                 now: datetime | None = None) -> ResearchResult:
    now = now or datetime.now(timezone.utc)
    terms = (terms or cfg.search_terms)[: cfg.max_search_calls]
    published_after = (now - timedelta(days=cfg.published_within_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    term_of: dict[str, str] = {}
    skipped: dict[str, int] = {}
    quota_stop = None
    searched = 0
    outliers: list[Candidate] = []
    candidates: list[Candidate] = []
    try:
        for term in terms:
            for vid in api.search(term, published_after):
                term_of.setdefault(vid, term)
            searched += 1
        # bekannte Ausreißer der letzten 30 Tage mit abrufen → neuer Snapshot fürs Wachstum
        for vid in store.known_video_ids(cfg.max_age_days + 7):
            term_of.setdefault(vid, "(Nachverfolgung)")
        for item in api.videos(list(term_of)):
            c = candidate_from_video(item, term_of.get(item.get("id", ""), ""))
            if c is None:
                skipped["unvollständig"] = skipped.get("unvollständig", 0) + 1
                continue
            if reason := prefilter_reason(c, cfg, now):
                skipped[reason.split(" (")[0]] = skipped.get(reason.split(" (")[0], 0) + 1
                continue
            candidates.append(c)
        stats = _channel_stats(api, store, sorted({c.channel_id for c in candidates}), cfg)
    except QuotaExceeded as exc:
        quota_stop = str(exc)
        log.warning("YouTube meldet %s – Recherche bricht ab und wertet das bisher Gesammelte aus.", exc)
        stats = {cid: store.cached_channel(cid, cfg.channel_cache_days) or {} for cid in
                 {c.channel_id for c in candidates}}
    for c in candidates:
        info = stats.get(c.channel_id) or {}
        c.subs, c.subs_hidden = info.get("subs"), bool(info.get("subs_hidden"))
        c.channel_median, c.channel_videos = info.get("median_views"), int(info.get("n_videos") or 0)
        compute_metrics(c, now)
        if reason := final_filter_reason(c, cfg):
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        store.save_video(c)
        outliers.append(c)
    outliers.sort(key=lambda c: c.score or 0, reverse=True)
    return ResearchResult(outliers, api.search_calls, api.units, searched, len(candidates), quota_stop, skipped)


# ------------------------------------------------------------------ Muster ohne KI (exakt gezählt)
TITLE_PATTERNS: dict[str, re.Pattern] = {
    "Zahl im Titel": re.compile(r"\d"),
    "du-Ansprache": re.compile(r"\b(du|dein\w*|dir|dich)\b", re.IGNORECASE),
    "Frage": re.compile(r"\?"),
    "Warnung (Achtung/Vorsicht/Warnung)": re.compile(r"\b(achtung|vorsicht|warnung|aufgepasst|wichtig)\b",
                                                     re.IGNORECASE),
    "Datum/Zeitpunkt": re.compile(r"\b(ab|bis|seit)\s+(jetzt|sofort|\d|januar|februar|märz|april|mai|juni|juli|"
                                  r"august|september|oktober|november|dezember)|\b20\d\d\b", re.IGNORECASE),
    "Betrag (€/Euro)": re.compile(r"€|\beuro\b", re.IGNORECASE),
    "GROSSBUCHSTABEN-Wort": re.compile(r"\b[A-ZÄÖÜ]{4,}\b"),
    "Emoji": re.compile("[\U0001F300-\U0001FAFF☀-➿]"),
}
LENGTH_BUCKETS = [(0, 15, "≤ 15 s"), (16, 30, "16–30 s"), (31, 45, "31–45 s"), (46, 60, "46–60 s"),
                  (61, 90, "61–90 s"), (91, 180, "91–180 s")]


def pattern_stats(videos: list[dict]) -> dict:
    n = len(videos) or 1
    titles = [v["title"] for v in videos]
    patterns = {name: sum(1 for t in titles if rx.search(t)) for name, rx in TITLE_PATTERNS.items()}
    lengths = {label: sum(1 for v in videos if lo <= v["duration_s"] <= hi) for lo, hi, label in LENGTH_BUCKETS}
    by_channel: dict[str, int] = {}
    for v in videos:
        by_channel[v["channel_title"]] = by_channel.get(v["channel_title"], 0) + 1
    durations = sorted(v["duration_s"] for v in videos)
    return {
        "n": len(videos),
        "title_patterns": {k: {"count": c, "share": round(100 * c / n)} for k, c in patterns.items()},
        "lengths": lengths,
        "median_length": statistics.median(durations) if durations else None,
        "recurring_channels": sorted(((k, c) for k, c in by_channel.items() if c >= 2), key=lambda x: -x[1]),
    }


# ------------------------------------------------------------------ Themen-Vorauswahl (Haiku)
class TopicVerdict(BaseModel):
    video_id: str
    relevant: bool = Field(description="true = sachliche Info zu Geld, Recht, Sozialleistungen, Steuern, Rente, "
                                       "Verbraucherthemen, die Zuschauern konkret etwas sagt.")
    reason: str = Field(description="Begründung auf Deutsch, max. 10 Wörter.")


class TopicBatch(BaseModel):
    items: list[TopicVerdict]


TOPIC_SYSTEM = """You sort YouTube Shorts for a German channel that explains concrete changes for ordinary people (money, law, \
benefits, taxes, pension, health insurance, consumer rights) in a factual way.
relevant=true: the Short informs about such a topic in a factual or practical way (rules, amounts, deadlines, \
tips, comparisons with numbers), even if the title is catchy.
relevant=false: political opinion clips or speeches by politicians, cabaret/satire/comedy, memes, rage bait \
without concrete information, unrelated entertainment, advertising for a single product or service.
Judge only from title, description and tags. When unsure, choose relevant=true.
Return exactly one entry per video_id. Reason in German, max. 10 words."""


class TopicClassifier:
    """Ein günstiger Haiku-Aufruf je 60 Videos; Ergebnis wird in research_videos gespeichert."""

    def __init__(self, settings: Settings) -> None:
        import anthropic

        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY fehlt in .env")
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=3, timeout=240)  # hängende Verbindung nach 4 min abbrechen + neu versuchen
        self._model = settings.anthropic_triage_model
        self.spent: list[float | None] = []

    def classify(self, videos: list[dict]) -> dict[str, tuple[bool, str]]:
        out: dict[str, tuple[bool, str]] = {}
        for i in range(0, len(videos), 60):
            batch = videos[i:i + 60]
            listing = "\n\n".join(
                f"[{v['video_id']}] {v['title']}\nKanal: {v['channel_title']}\n"
                f"Tags: {', '.join(json.loads(v.get('tags') or '[]')[:10])}\n{(v.get('description') or '')[:300]}"
                for v in batch)
            response = self._client.messages.parse(
                model=self._model, max_tokens=8000, system=TOPIC_SYSTEM,
                messages=[{"role": "user", "content": listing}], output_format=TopicBatch,
                output_config={"effort": "low"}, thinking={"type": "disabled"})
            self.spent.append(claude_cost(response.model, response.usage.input_tokens, response.usage.output_tokens))
            if response.stop_reason != "end_turn" or response.parsed_output is None:
                raise RuntimeError(f"Themen-Vorauswahl ohne gültige Antwort (stop_reason={response.stop_reason})")
            got = {it.video_id: (it.relevant, it.reason) for it in response.parsed_output.items}
            for v in batch:  # fehlende IDs: im Zweifel behalten
                out[v["video_id"]] = got.get(v["video_id"], (True, "keine Einschätzung – behalten"))
        return out


def classify_outliers(store: "ResearchStore", classifier) -> tuple[int, int]:
    """Noch nicht eingeordnete Ausreißer einordnen. Liefert (behalten, aussortiert)."""
    todo = store.unclassified()
    if not todo:
        return 0, 0
    verdicts = classifier.classify(todo)
    kept = dropped = 0
    for v in todo:  # fehlende Einschätzung: im Zweifel behalten
        ok, reason = verdicts.get(v["video_id"], (True, "keine Einschätzung – behalten"))
        store.set_relevance(v["video_id"], ok, reason)
        kept, dropped = (kept + 1, dropped) if ok else (kept, dropped + 1)
    return kept, dropped


# ------------------------------------------------------------------ Claude-Auswertung
class Cluster(BaseModel):
    topic: str = Field(description="Thema in wenigen Wörtern, Deutsch.")
    video_ids: list[str] = Field(description="IDs der Videos aus den Daten, die dazugehören.")
    observation: str = Field(description="Was die Daten zu diesem Thema zeigen, 1–2 Sätze.")


class Idea(BaseModel):
    title: str = Field(description="Arbeitstitel für unseren Short, Deutsch.")
    pattern: str = Field(description="Auf welches beobachtete Muster/Cluster sich die Idee stützt.")
    source_hint: str = Field(description="Welche unserer offiziellen Quellen das belegen könnte (Name aus der Liste).")
    caution: str = Field(description="Was vor der Umsetzung zu prüfen ist (z. B. Stand, Rechtsstatus); leer wenn nichts.")


class OutlierAnalysis(BaseModel):
    clusters: list[Cluster]
    title_findings: str = Field(description="Deutung der gelieferten Titelmuster-Anteile, 2–4 Sätze.")
    length_findings: str = Field(description="Deutung der Längenverteilung, 1–3 Sätze.")
    channel_findings: str = Field(description="Deutung der wiederkehrenden Kanäle, 1–3 Sätze.")
    ideas: list[Idea] = Field(description="Genau 10 Themenideen für unseren Kanal.")
    caveats: str = Field(description="Grenzen der Daten (Stichprobe, Suchbegriffe, API-Sicht), 1–3 Sätze.")


ANALYSIS_SYSTEM = """\
You analyse YouTube Shorts from small German channels about money, law and everyday life that performed far \
above their channel's usual level ("outliers"). Our own channel "{channel}" makes faceless Shorts with an AI \
voice and large text about concrete changes for ordinary people in Germany, always backed by official sources.

Rules:
- Conclude only from the data you are given. Do not use outside knowledge about these channels or videos.
- Mark every pattern that rests on fewer than 5 videos with "(< 5 Videos)" in the text.
- The title-pattern shares, length distribution and recurring channels were counted exactly in code; interpret \
them, do not recount.
- Topic ideas must fit the observed patterns and be checkable from one of OUR official sources: {sources}. \
Never claim a fact is true; describe what the video would cover.
- Write all text in German."""

REVIEW_SYSTEM = """\
You compare manually recorded first-3-second features of outlier Shorts from small German channels with our own \
channel's format. Conclude only from the given data; mark patterns resting on fewer than 5 videos with \
"(< 5 Videos)". Write in German, in Markdown, max. 350 words: 1) what the outliers have in common (with counts), \
2) how our format differs, 3) three concrete, testable changes for our next videos."""


class ResearchAnalyst:
    def __init__(self, settings: Settings, cfg: ResearchConfig) -> None:
        import anthropic

        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY fehlt in .env")
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=3, timeout=240)  # hängende Verbindung nach 4 min abbrechen + neu versuchen
        self._cfg = cfg
        self.spent: list[float | None] = []

    def analyse(self, videos: list[dict], stats: dict, channel_name: str, sources: list[str]) -> OutlierAnalysis:
        payload = [{k: v[k] for k in ("video_id", "title", "channel_title", "duration_s", "views", "subs",
                                      "score", "views_per_day", "search_term")}
                   | {"description": (v.get("description") or "")[:300], "tags": json.loads(v.get("tags") or "[]")[:12]}
                   for v in videos]
        response = self._client.messages.parse(
            model=self._cfg.model,
            max_tokens=16000,
            system=ANALYSIS_SYSTEM.format(channel=channel_name, sources=", ".join(sources)),
            messages=[{"role": "user", "content": "COUNTED STATISTICS:\n" + json.dumps(stats, ensure_ascii=False)
                       + "\n\nVIDEOS (sorted by outlier score):\n" + json.dumps(payload, ensure_ascii=False)}],
            output_format=OutlierAnalysis,
            output_config={"effort": self._cfg.effort},
        )
        self.spent.append(claude_cost(response.model, response.usage.input_tokens, response.usage.output_tokens))
        log.info("Auswertung: model=%s stop=%s in=%s out=%s", response.model, response.stop_reason,
                 response.usage.input_tokens, response.usage.output_tokens)
        if response.stop_reason != "end_turn" or response.parsed_output is None:
            raise RuntimeError(f"Auswertung ohne gültige Antwort (stop_reason={response.stop_reason})")
        return response.parsed_output

    def compare_reviews(self, reviews: list[dict], our_format: str) -> str:
        rows = [{k: r.get(k) for k in ("title", "channel_title", "duration_s", "views", "score", "face", "voice",
                                       "first_frame", "hook_type", "music", "notes")} for r in reviews]
        response = self._client.messages.create(
            model=self._cfg.model, max_tokens=8000, system=REVIEW_SYSTEM,
            messages=[{"role": "user", "content": "OUR FORMAT:\n" + our_format + "\n\nREVIEWED OUTLIERS:\n"
                       + json.dumps(rows, ensure_ascii=False)}],
            output_config={"effort": self._cfg.effort},
        )
        self.spent.append(claude_cost(response.model, response.usage.input_tokens, response.usage.output_tokens))
        if response.stop_reason != "end_turn":
            raise RuntimeError(f"Muster-Auswertung abgebrochen (stop_reason={response.stop_reason})")
        return "".join(b.text for b in response.content if b.type == "text").strip()


# ------------------------------------------------------------------ Ausgabe
def short_url(video_id: str) -> str:
    return f"https://www.youtube.com/shorts/{video_id}"


def _fmt_int(n: int | float | None) -> str:
    return "–" if n is None else f"{int(round(n)):,}".replace(",", ".")


def write_csv(videos: list[dict], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["video_id", "title", "channel_title", "channel_id", "duration_s", "published_at", "views", "likes",
            "comments", "subs", "subs_hidden", "channel_median", "score", "views_per_day", "views_per_sub",
            "search_term"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols + ["url"])
        for v in videos:
            w.writerow([v.get(c) for c in cols] + [short_url(v["video_id"])])
    return path


def render_report(result: ResearchResult, videos: list[dict], stats: dict, analysis: OutlierAnalysis | None,
                  cost_usd: float, eur_per_usd: float, today: str, excluded: list[dict] | None = None,
                  min_median: float = 300) -> str:
    excluded = excluded or []
    counts = (f"Gespeicherte Ausreißer: {len(videos) + len(excluded)}" if result.note else
              f"Suchbegriffe: {result.terms_searched} · search-Aufrufe: {result.search_calls} · weitere Einheiten: "
              f"{result.units} · Kandidaten nach Vorfilter: {result.candidates} · **Ausreißer: {len(result.outliers)}**"
              + (f" · ⚠️ Abbruch: {result.quota_stop}" if result.quota_stop else ""))
    lines = [f"# Ausreißer-Recherche {today}", ""] + ([f"_{result.note}_", ""] if result.note else []) + [
             counts, "",
             "Filter: Short ≤ 180 s, ≥ 20.000 Aufrufe, ≤ 30 Tage alt, Kanal ≤ 30.000 Abos (versteckt = markiert), "
             f"Score = Aufrufe / Median der letzten 20 Uploads ≥ 5, Kanal-Median ≥ {min_median:.0f}. "
             "Nur offizielle YouTube Data API.", ""]
    if result.skipped:
        lines += ["Aussortiert: " + ", ".join(f"{k} {v}" for k, v in sorted(result.skipped.items(),
                                                                          key=lambda x: -x[1])), ""]
    if excluded:
        lines += [f"Themen-Vorauswahl (Claude Haiku): **{len(excluded)} Ausreißer als Politik, Satire oder Unterhaltung "
                  f"aussortiert**, ausgewertet werden {len(videos)}.", "", "<details><summary>Aussortierte Titel</summary>",
                  ""] + [f"- {v['title'].replace('|', '/')} – _{v.get('relevant_reason') or ''}_" for v in excluded[:30]] \
            + ["", "</details>", ""]
    lines += ["## Top 20", "", "| # | Titel | Kanal | Abos | Aufrufe | Median Kanal | Score | Aufrufe/Tag | Länge |",
              "|---|---|---|---|---|---|---|---|---|"]
    for i, v in enumerate(videos[:20], 1):
        title = v["title"].replace("|", "/")
        subs = "versteckt" if v.get("subs_hidden") else _fmt_int(v.get("subs"))
        lines.append(f"| {i} | [{title}]({short_url(v['video_id'])}) | {v['channel_title'].replace('|', '/')} | "
                     f"{subs} | {_fmt_int(v['views'])} | {_fmt_int(v['channel_median'])} | {v['score']:.1f} | "
                     f"{_fmt_int(v['views_per_day'])} | {v['duration_s']} s |")
    n = stats["n"]
    small = " (< 5 Videos)" if n < 5 else ""
    lines += ["", f"## Muster (gezählt über die Top {n}{small})", "", "| Titelmuster | Anzahl | Anteil |",
              "|---|---|---|"]
    for name, s in stats["title_patterns"].items():
        lines.append(f"| {name} | {s['count']} | {s['share']} %{' (< 5 Videos)' if s['count'] < 5 else ''} |")
    lines += ["", "| Länge | Anzahl |", "|---|---|"] + [f"| {k} | {c} |" for k, c in stats["lengths"].items()]
    lines += ["", f"Median-Länge: {stats['median_length']} s" if stats["median_length"] is not None else "", ""]
    rec = stats["recurring_channels"]
    lines += ["Kanäle mit mehreren Ausreißern: " + (", ".join(f"{k} ({c})" for k, c in rec) if rec else "keine"), ""]
    if analysis:
        lines += ["## Auswertung (Claude)", "", "### Themen-Cluster", ""]
        for cl in analysis.clusters:
            mark = " (< 5 Videos)" if len(cl.video_ids) < 5 and "(< 5" not in cl.observation else ""
            lines.append(f"- **{cl.topic}** ({len(cl.video_ids)} Videos{mark}): {cl.observation}")
        lines += ["", "### Titel", "", analysis.title_findings, "", "### Länge", "", analysis.length_findings, "",
                  "### Kanäle", "", analysis.channel_findings, "", "### 10 Themenideen für unseren Kanal", ""]
        for i, idea in enumerate(analysis.ideas, 1):
            caution = f" ⚠️ {idea.caution}" if idea.caution else ""
            lines.append(f"{i}. **{idea.title}** – Muster: {idea.pattern}. Quelle: {idea.source_hint}.{caution}")
        lines += ["", "### Grenzen", "", analysis.caveats, ""]
    else:
        lines += ["_Keine Claude-Auswertung (zu wenige Ausreißer oder kein API-Key)._", ""]
    lines += [f"Kosten der Auswertung: {cost_usd * eur_per_usd:.3f} €".replace(".", ","), ""]
    return "\n".join(lines)


def telegram_summary(result: ResearchResult, videos: list[dict], report_path: Path | None, excluded: int = 0) -> str:
    head = (f"🔎 Recherche: {len(videos)} passende Ausreißer"
            + (f", {excluded} Politik/Satire aussortiert" if excluded else "")
            + (f" · {result.note}" if result.note else
               f" (aus {result.candidates} Kandidaten, {result.search_calls} search-Aufrufe, {result.units} weitere Einheiten)"))
    lines = [head]
    if result.quota_stop:
        lines.append(f"⚠️ Abgebrochen: YouTube meldet {result.quota_stop}")
    for v in videos[:5]:
        lines.append(f"• {v['score']:.0f}× · {_fmt_int(v['views'])} Aufrufe · {v['title'][:60]}\n  {short_url(v['video_id'])}")
    if report_path:
        lines.append(f"Bericht: {report_path.relative_to(APP_ROOT) if report_path.is_relative_to(APP_ROOT) else report_path}")
    return "\n".join(lines)


OUR_FORMAT = """\
- kein Gesicht, KI-Stimme (OpenAI, Stimme nova, Tempo 1,25), keine Musik
- Sekunde 1: seit v2 große Text-Tafel mit dem Hook-Satz (Gelb auf Navy); v1: Faktenkarte mit Kernzahl
- Hintergrund: abgedunkeltes Stock-Video (Feuerwerk, Zeitraffer, Partikel)
- Hook: max. 8 Wörter, du-Ansprache, Nutzen/Verlust oder Neugier-Lücke, kein Datum am Anfang
- Länge 24–30 s, Faktenkarten mit Zahlen, Wort-Untertitel, keine Endkarte"""
