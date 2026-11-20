"""Themen aus offiziellen Quellen (RSS oder HTML-Linkliste) abrufen und in SQLite ablegen."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from calendar import timegm
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit

import feedparser
import httpx
import trafilatura

from .config import ChannelConfig, SourceConfig
from .db import Database

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; shorts-pipeline/1.0; +RSS-Reader)"
TIMEOUT = httpx.Timeout(20.0)


@dataclass(frozen=True)
class FeedItem:
    url: str
    title: str
    summary: str | None
    published_at: str | None


def normalize_url(url: str) -> str:
    """Entfernt Fragmente und Tracking-Parameter, damit Duplikate erkannt werden."""
    parts = urlsplit(url.strip())
    query = "&".join(
        p for p in parts.query.split("&")
        if p and not p.lower().startswith(("utm_", "nn=", "view="))
    )
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, query, ""))


def _clean(text: str | None) -> str:
    text = re.sub(r"<[^>]+>", " ", unescape(text or ""))
    return re.sub(r"\s+", " ", text).replace("\xa0", " ").strip()


def _http_get(url: str) -> httpx.Response:
    resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, follow_redirects=True)
    resp.raise_for_status()
    return resp


def parse_rss(content: bytes) -> list[FeedItem]:
    feed = feedparser.parse(content)
    items: list[FeedItem] = []
    for e in feed.entries:
        link = e.get("link")
        if not link:
            continue
        published = None
        if e.get("published_parsed"):
            published = datetime.fromtimestamp(timegm(e.published_parsed), tz=timezone.utc).isoformat()
        items.append(FeedItem(normalize_url(link), _clean(e.get("title")), _clean(e.get("summary")) or None,
                              published))
    return items


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href, _clean(" ".join(self._text))))
            self._href = None


def parse_html_links(html: str, base_url: str, pattern: str, max_items: int) -> list[FeedItem]:
    parser = _LinkParser()
    parser.feed(html)
    regex = re.compile(pattern)
    seen: dict[str, FeedItem] = {}
    for href, text in parser.links:
        if not href or not regex.search(href.split("?")[0]):
            continue
        url = normalize_url(urljoin(base_url, href))
        if url in seen and len(seen[url].title) >= len(text):
            continue
        seen[url] = FeedItem(url, text or url.rsplit("/", 1)[-1], None, None)
    return list(seen.values())[:max_items]


def fetch_source(src: SourceConfig) -> list[FeedItem]:
    resp = _http_get(src.url)
    if src.type == "rss":
        return parse_rss(resp.content)[: src.max_items]
    if src.type == "html":
        if not src.link_pattern:
            raise ValueError(f"Quelle {src.name}: link_pattern fehlt")
        return parse_html_links(resp.text, src.url, src.link_pattern, src.max_items)
    raise ValueError(f"Unbekannter Quellentyp {src.type}")


def collect_topics(db: Database, channel: ChannelConfig) -> tuple[int, list[str]]:
    """Ruft alle Quellen ab. Gibt (Anzahl neuer Themen, Fehlerliste) zurück."""
    new, errors = 0, []
    for src in channel.sources:
        try:
            items = fetch_source(src)
        except Exception as exc:  # eine kaputte Quelle darf den Lauf nicht stoppen
            log.warning("Quelle %s fehlgeschlagen: %s", src.name, exc)
            errors.append(f"{src.name}: {exc}")
            continue
        added = sum(
            1 for it in items
            if db.add_topic(channel.key, it.url, src.name, it.title, it.summary, it.published_at)
        )
        log.info("Quelle %s: %d Einträge, %d neu", src.name, len(items), added)
        new += added
    return new, errors


@dataclass(frozen=True)
class Article:
    text: str
    date: str | None  # YYYY-MM-DD laut Seite (article:published_time, JSON-LD, "Stand: …"), sonst None


def fetch_article(url: str, max_chars: int = 20000) -> Article:
    """Lädt Haupttext (ohne Navigation/Footer) und Veröffentlichungsdatum eines Artikels."""
    html = _http_get(url).text
    text = trafilatura.extract(html, url=url, include_comments=False, include_tables=True,
                               favor_precision=True) or ""
    if len(text) > max_chars:
        log.info("Artikeltext %s auf %d Zeichen gekürzt (war %d)", url, max_chars, len(text))
        text = text[:max_chars]
    meta = trafilatura.extract_metadata(html, default_url=url)
    return Article(text, meta.date if meta and meta.date else None)


def fetch_article_text(url: str, max_chars: int = 20000) -> str:
    return fetch_article(url, max_chars).text
