"""SQLite-Persistenz mit expliziter Status-Maschine.

neu → skript → video → wartet_auf_freigabe → freigegeben → hochgeladen
(manueller Modus: freigegeben → paket_gesendet → hochgeladen, bestätigt per Telegram-Button)
Nebenwege: verworfen (Claude unsicher/irrelevant), abgelehnt (von dir), fehler (technisch, retrybar).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)


class Status(StrEnum):
    NEU = "neu"
    SKRIPT = "skript"
    VIDEO = "video"
    WARTET = "wartet_auf_freigabe"
    FREIGEGEBEN = "freigegeben"
    PAKET = "paket_gesendet"
    HOCHGELADEN = "hochgeladen"
    ABGELEHNT = "abgelehnt"
    VERWORFEN = "verworfen"
    FEHLER = "fehler"


ALLOWED: dict[Status, set[Status]] = {
    Status.NEU: {Status.SKRIPT, Status.VERWORFEN, Status.FEHLER},
    Status.SKRIPT: {Status.VIDEO, Status.FEHLER},
    Status.VIDEO: {Status.WARTET, Status.FEHLER},
    Status.WARTET: {Status.FREIGEGEBEN, Status.ABGELEHNT},
    Status.FREIGEGEBEN: {Status.HOCHGELADEN, Status.PAKET, Status.FEHLER},
    Status.PAKET: {Status.HOCHGELADEN},
    Status.FEHLER: {Status.NEU, Status.SKRIPT, Status.VIDEO, Status.FREIGEGEBEN},  # Retry
    Status.HOCHGELADEN: set(),
    Status.ABGELEHNT: {Status.NEU},  # nur per CLI 'regenerate': neues Skript + Video
    Status.VERWORFEN: set(),
}


class TransitionError(ValueError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    channel       TEXT NOT NULL,
    url           TEXT NOT NULL,
    source_name   TEXT NOT NULL,
    title         TEXT NOT NULL,
    summary       TEXT,
    published_at  TEXT,
    source_text   TEXT,
    status        TEXT NOT NULL DEFAULT 'neu',
    prev_status   TEXT,
    script_json   TEXT,
    audio_path    TEXT,
    video_path    TEXT,
    note          TEXT,
    telegram_message_id INTEGER,
    publish_at    TEXT,
    youtube_id    TEXT,
    background_credit TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE(channel, url)
);
CREATE INDEX IF NOT EXISTS idx_topics_status ON topics(channel, status);
CREATE TABLE IF NOT EXISTS quota_usage (
    day      TEXT NOT NULL,      -- Datum in America/Los_Angeles (YouTube-Quota-Reset)
    units    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(day)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Topic:
    id: int
    channel: str
    url: str
    source_name: str
    title: str
    summary: str | None
    published_at: str | None
    source_text: str | None
    status: Status
    prev_status: str | None
    script_json: str | None
    audio_path: str | None
    video_path: str | None
    note: str | None
    telegram_message_id: int | None
    publish_at: str | None
    youtube_id: str | None
    background_credit: str | None
    created_at: str
    updated_at: str

    @property
    def script(self) -> dict[str, Any] | None:
        return json.loads(self.script_json) if self.script_json else None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Topic":
        d = dict(row)
        d["status"] = Status(d["status"])
        return cls(**d)


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Fehlende Spalten bei bestehenden Datenbanken ergänzen (nur additive Änderungen)."""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(topics)")}
        if "background_credit" not in cols:
            self._conn.execute("ALTER TABLE topics ADD COLUMN background_credit TEXT")

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # --- Topics -------------------------------------------------------------
    def add_topic(self, channel: str, url: str, source_name: str, title: str,
                  summary: str | None = None, published_at: str | None = None) -> int | None:
        """Legt ein Thema an. Gibt None zurück, wenn die URL für den Kanal schon bekannt ist."""
        now = _now()
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO topics(channel,url,source_name,title,summary,published_at,"
            "status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (channel, url, source_name, title, summary, published_at, Status.NEU, now, now),
        )
        return cur.lastrowid if cur.rowcount else None

    def get(self, topic_id: int) -> Topic:
        row = self._conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
        if row is None:
            raise KeyError(topic_id)
        return Topic.from_row(row)

    def by_status(self, status: Status, channel: str | None = None, limit: int | None = None) -> list[Topic]:
        sql = "SELECT * FROM topics WHERE status=?"
        args: list[Any] = [status]
        if channel:
            sql += " AND channel=?"
            args.append(channel)
        sql += " ORDER BY COALESCE(published_at, created_at) DESC, id DESC"
        if limit:
            sql += " LIMIT ?"
            args.append(limit)
        return [Topic.from_row(r) for r in self._conn.execute(sql, args)]

    def update_fields(self, topic_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self._conn.execute(f"UPDATE topics SET {cols} WHERE id=?", (*fields.values(), topic_id))

    def transition(self, topic_id: int, new: Status, **fields: Any) -> Topic:
        """Statuswechsel mit Prüfung der erlaubten Übergänge (atomar)."""
        with self.tx():
            topic = self.get(topic_id)
            if new not in ALLOWED[topic.status]:
                raise TransitionError(f"Thema {topic_id}: {topic.status} → {new} nicht erlaubt")
            self.update_fields(topic_id, status=new, prev_status=topic.status, **fields)
        log.info("Thema %s: %s → %s", topic_id, topic.status, new)
        return self.get(topic_id)

    def regenerate(self, topic_id: int) -> Topic:
        """Thema mit frischem Skript/Video neu einreihen; Quelltext bleibt, Artefakte werden geleert."""
        return self.transition(topic_id, Status.NEU, script_json=None, audio_path=None, video_path=None,
                               note=None, telegram_message_id=None, publish_at=None,
                               background_credit=None)

    def taken_publish_slots(self, channel: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT publish_at FROM topics WHERE channel=? AND publish_at IS NOT NULL", (channel,))
        return {r[0] for r in rows}

    def counts(self, channel: str | None = None) -> dict[str, int]:
        sql = "SELECT status, COUNT(*) FROM topics"
        args: tuple[Any, ...] = ()
        if channel:
            sql += " WHERE channel=?"
            args = (channel,)
        sql += " GROUP BY status"
        return {r[0]: r[1] for r in self._conn.execute(sql, args)}

    def purge_channel(self, channel: str) -> int:
        """Löscht alle Themen eines Kanals – nur für die separate Dry-Run-Datenbank gedacht."""
        return self._conn.execute("DELETE FROM topics WHERE channel=?", (channel,)).rowcount

    # --- Quota --------------------------------------------------------------
    def quota_used(self, day: str) -> int:
        row = self._conn.execute("SELECT units FROM quota_usage WHERE day=?", (day,)).fetchone()
        return int(row[0]) if row else 0

    def add_quota(self, day: str, units: int) -> None:
        self._conn.execute(
            "INSERT INTO quota_usage(day,units) VALUES(?,?) "
            "ON CONFLICT(day) DO UPDATE SET units=units+excluded.units", (day, units))
