"""Aufbereitung der Daten für die einzelnen Seiten (Heute, Freigabe, Videos, Statistik, Jobs).

Bewusst ohne FastAPI-Abhängigkeiten, damit die Logik (Reiter, Sortierung, Klartext-Fehler, Diagramm) testbar ist.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import escape

from ..db import Database, Status, Topic
from ..upload import BERLIN, WOCHENTAGE, upcoming_free_slots

STYLE_SHORT = {"v1": "v1", "v2-hook-frame0": "v2", "v3-merkliste": "v3"}
STYLE_LONG = {"v1": "v1 · Start", "v2-hook-frame0": "v2 · Hook-Tafel", "v3-merkliste": "v3 · Merkliste"}
STYLE_COLORS = {"v1": "#94a3b8", "v2": "#0b1f3a", "v3": "#d4a017"}


# ------------------------------------------------------------------ Datum
def parse_iso(iso: str | None) -> datetime | None:
    if not iso:
        return None
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt_dt(iso: str | None, with_time: bool = True) -> str:
    """Ein Format überall: 'Mo 23.11., 17:00'."""
    dt = parse_iso(iso)
    if not dt:
        return "–"
    local = dt.astimezone(BERLIN)
    day = f"{WOCHENTAGE[local.weekday()]} {local:%d.%m.}"
    return f"{day}, {local:%H:%M}" if with_time else day


def fmt_rel(iso: str | None, now: datetime | None = None) -> str:
    """Relative Angabe als Zusatz: 'heute', 'morgen', 'in 2 Tagen', 'vor 3 Tagen'."""
    dt = parse_iso(iso)
    if not dt:
        return ""
    now = now or datetime.now(timezone.utc)
    days = (dt.astimezone(BERLIN).date() - now.astimezone(BERLIN).date()).days
    if days == 0:
        return "heute"
    if days == 1:
        return "morgen"
    if days == -1:
        return "gestern"
    return f"in {days} Tagen" if days > 0 else f"vor {-days} Tagen"


def style_key(topic: Topic) -> str:
    return topic.style or (topic.script or {}).get("style") or "v1"


# ------------------------------------------------------------------ Status (eine Quelle für alle Badges)
def video_state(topic: Topic, now: datetime) -> str:
    """Einheitlicher Anzeige-Status eines Themas/Videos."""
    if topic.yt_visibility == "gelöscht":
        return "gelöscht"
    if topic.status == Status.HOCHGELADEN:
        when = parse_iso(topic.yt_published_at or topic.publish_at)
        if topic.yt_visibility == "öffentlich" or (when and when <= now and topic.yt_visibility != "privat"):
            return "öffentlich"
        return "geplant"
    return {Status.NEU: "neu", Status.SKRIPT: "in Arbeit", Status.VIDEO: "wartet", Status.WARTET: "wartet",
            Status.FREIGEGEBEN: "freigegeben", Status.PAKET: "Paket gesendet", Status.ABGELEHNT: "abgelehnt",
            Status.VERWORFEN: "verworfen", Status.FEHLER: "Fehler"}.get(topic.status, str(topic.status))


# ------------------------------------------------------------------ Fehler in Klartext
_ERRORS = [
    (r"Erster Satz spricht nicht mit 'du' an", "Der erste Satz spricht den Zuschauer nicht direkt an."),
    (r"Erster Satz hat (\d+) Wörter", r"Der erste Satz ist zu lang (\1 Wörter, erlaubt sind 8)."),
    (r"Erster Satz beginnt mit einer Datums", "Der erste Satz beginnt mit einem Datum statt mit dem Nutzen."),
    (r"Zweiter Satz beginnt mit Behörde", "Der zweite Satz beginnt mit der Behörde statt mit der Änderung."),
    (r"Sprechtext hat (\d+) Wörter, erlaubt sind? (\d+)–(\d+)", r"Der Sprechtext hat \1 Wörter, erlaubt sind \2–\3."),
    (r"Tafel um (\d+) px zu hoch", r"Die Merkliste passt nicht auf den Bildschirm (\1 px zu hoch)."),
    (r"Videodatei fehlt", "Die Videodatei fehlt auf dem Server."),
    (r"Sprechtext zu lang für v3", "Der Sprechtext ist für eine Merkliste zu lang."),
    (r"quotaExceeded|uploadLimitExceeded", "YouTube hat das Tageskontingent erreicht."),
    (r"HTTPStatusError.*404", "Die Quelle ist nicht mehr erreichbar (404)."),
    (r"(Timeout|TimeoutError|ReadTimeout|APITimeoutError)", "Zeitüberschreitung bei einem externen Dienst."),
    (r"(ConnectError|APIConnectionError)", "Keine Verbindung zu einem externen Dienst."),
    (r"refusal|abgelehnt \(refusal\)", "Claude hat die Anfrage abgelehnt."),
]
_STAGE = re.compile(r"^#\d+\s+(Skript|Render|Upload)\b", re.IGNORECASE)


@dataclass
class PlainError:
    text: str
    detail: str


def humanize_error(note: str | None) -> PlainError:
    """'#6 Skript: RuntimeError: Skript ungültig: Erster Satz …' → 'Skript abgelehnt: Der erste Satz …'."""
    raw = (note or "").strip()
    stage = (m.group(1).capitalize() if (m := _STAGE.match(raw)) else
             "Upload" if raw.startswith("Upload") else "")
    prefix = {"Skript": "Skript abgelehnt", "Render": "Video konnte nicht erzeugt werden",
              "Upload": "Upload fehlgeschlagen"}.get(stage, "Fehler")
    for pattern, text in _ERRORS:
        if m := re.search(pattern, raw):
            return PlainError(f"{prefix}: {m.expand(text)}", raw)
    short = re.sub(r"^#\d+\s+\w+:\s*", "", raw)
    short = re.sub(r"^[A-Za-z]+(Error|Exception):\s*", "", short)
    return PlainError(f"{prefix}: {short[:160]}" if short else prefix, raw)


# ------------------------------------------------------------------ Heute
@dataclass
class TimelineRow:
    slot: str
    when: str
    rel: str
    topic: Topic | None
    style: str = ""
    state: str = "frei"
    is_next: bool = False


def timeline(db: Database, channel: str, slots: list[str], now: datetime, n: int = 7) -> list[TimelineRow]:
    """Die nächsten n Termine aus dem Wochenplan – belegt (mit Video) oder frei."""
    upcoming = upcoming_free_slots(slots, set(), now, n) if slots else []
    by_slot = {}
    for t in db.by_status(Status.HOCHGELADEN, channel):
        if t.publish_at and t.yt_visibility != "gelöscht":
            by_slot[t.publish_at] = t
    rows = []
    for slot in upcoming:
        t = by_slot.get(slot)
        rows.append(TimelineRow(slot, fmt_dt(slot), fmt_rel(slot, now), t,
                                STYLE_SHORT.get(style_key(t), "") if t else "",
                                video_state(t, now) if t else "frei"))
    if rows:
        rows[0].is_next = True
    return rows


@dataclass
class Attention:
    kind: str                 # fehler | frist | vorrat | sperre
    text: str
    detail: str = ""
    topic_id: int | None = None
    title: str = ""


def needs_attention(failed: list[Topic], deadline_warnings: list[tuple[Topic, str]], stock_text: str | None,
                    block_text: str | None) -> list[Attention]:
    out = []
    for t in failed:
        e = humanize_error(t.note)
        out.append(Attention("fehler", e.text, e.detail, t.id, (t.script or {}).get("title", t.title)))
    for t, warning in deadline_warnings:
        out.append(Attention("frist", warning, topic_id=t.id, title=(t.script or {}).get("title", t.title)))
    if stock_text:
        out.append(Attention("vorrat", stock_text))
    if block_text:
        out.append(Attention("sperre", block_text))
    return out


def views_last_days(db: Database, days: int = 7, today: datetime | None = None, channel: str | None = None) -> int | None:
    """Aufrufe der letzten N Tage aus den täglichen Gesamtständen (yt_stats): aktueller Stand minus Stand vor N
    Tagen je Video. None, wenn noch keine Analytics-Daten vorliegen."""
    from ..analytics import ensure_schema
    ensure_schema(db)
    today = today or datetime.now(timezone.utc)
    cutoff = (today - timedelta(days=days)).date().isoformat()
    where, params = (" WHERE topic_id IN (SELECT id FROM topics WHERE channel=?)", (channel,)) if channel else ("", ())
    rows = db._conn.execute(f"SELECT topic_id, day, views FROM yt_stats{where} ORDER BY topic_id, day",
                            params).fetchall()
    if not rows:
        return None
    latest: dict[int, int] = {}
    before: dict[int, int] = {}
    for tid, day, views in rows:
        latest[tid] = views or 0
        if day <= cutoff:
            before[tid] = views or 0
    return sum(v - before.get(tid, 0) for tid, v in latest.items())


# ------------------------------------------------------------------ Videos
TABS = {"geplant": "Geplant", "oeffentlich": "Öffentlich", "abgelehnt": "Abgelehnt/Verworfen",
        "geloescht": "Gelöscht"}


@dataclass
class VideoRow:
    topic: Topic
    date_label: str
    date_sort: str
    state: str
    style: str
    views: int | None = None
    avg_pct: float | None = None
    viewed_rate: str | None = None
    extra: dict = field(default_factory=dict)


def video_rows(db: Database, channel: str, tab: str, now: datetime, stats: dict[int, dict]) -> list[VideoRow]:
    """Ein Reiter der Videos-Seite. Geplant aufsteigend nach Termin, alle anderen absteigend."""
    rows: list[VideoRow] = []
    if tab in ("geplant", "oeffentlich", "geloescht"):
        topics = db.by_status(Status.HOCHGELADEN, channel) + db.by_status(Status.PAKET, channel)
    else:
        topics = db.by_status(Status.ABGELEHNT, channel) + [
            t for t in db.by_status(Status.VERWORFEN, channel) if t.script_json]
    for t in topics:
        state = video_state(t, now)
        if tab == "geplant" and state not in ("geplant", "Paket gesendet"):
            continue
        if tab == "oeffentlich" and state != "öffentlich":
            continue
        if tab == "geloescht" and state != "gelöscht":
            continue
        if state == "gelöscht":  # nie „geplant …“ bei gelöschten Videos
            label = f"gelöscht (erkannt {fmt_dt(t.yt_deleted_at, False)})" if t.yt_deleted_at else "gelöscht"
            sort = t.yt_deleted_at or t.updated_at
        elif state == "öffentlich":
            sort = t.yt_published_at or t.publish_at or t.updated_at
            label = fmt_dt(sort)
        elif tab in ("geplant",):
            sort = t.publish_at or t.updated_at
            label = fmt_dt(sort)
        else:
            sort = t.updated_at
            label = fmt_dt(sort)
        st = stats.get(t.id, {})
        rows.append(VideoRow(t, label, sort or "", state, STYLE_SHORT.get(style_key(t), "v1"),
                             st.get("views"), st.get("avg_view_pct"), t.yt_viewed_rate))
    rows.sort(key=lambda r: r.date_sort, reverse=(tab != "geplant"))
    return rows


def tab_counts(db: Database, channel: str, now: datetime) -> dict[str, int]:
    return {tab: len(video_rows(db, channel, tab, now, {})) for tab in TABS}


# ------------------------------------------------------------------ Statistik
def parse_rate(value: str) -> float | None:
    """'47,5 %' → 47.5; leer → None; ungültig → ValueError."""
    v = (value or "").strip().replace(",", ".").rstrip("%").strip()
    if not v:
        return None
    f = float(v)
    if not 0 <= f <= 100:
        raise ValueError(value)
    return f


def scatter_svg(points: list[tuple[datetime, float, str, str]], width: int = 880, height: int = 280) -> str:
    """Punkte (Datum, Prozent, Stil, Titel) → SVG. Serverseitig, ohne Bibliothek."""
    pad_l, pad_r, pad_t, pad_b = 44, 16, 16, 32
    w, h = width - pad_l - pad_r, height - pad_t - pad_b
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Angesehen vs. weggewischt pro Video" '
             f'class="chart" preserveAspectRatio="xMidYMid meet">']
    for pct in (0, 25, 50, 75, 100):  # leises Raster mit Beschriftung
        y = pad_t + h - h * pct / 100
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{pad_l - 8}" y="{y + 4:.1f}" class="axis" text-anchor="end">{pct} %</text>')
    if not points:
        parts.append(f'<text x="{width / 2}" y="{height / 2}" class="axis" text-anchor="middle">'
                     'Noch keine Werte eingetragen</text></svg>')
        return "".join(parts)
    xs = [p[0] for p in points]
    x0, x1 = min(xs), max(xs)
    span = max((x1 - x0).total_seconds(), 86400)
    days = sorted({p[0].astimezone(BERLIN).date() for p in points})
    for d in days[:: max(1, len(days) // 6)]:
        x = pad_l + w * ((datetime(d.year, d.month, d.day, 12, tzinfo=BERLIN) - x0).total_seconds() / span)
        x = min(max(x, pad_l), pad_l + w)
        parts.append(f'<text x="{x:.1f}" y="{height - 10}" class="axis" text-anchor="middle">{d:%d.%m.}</text>')
    for when, pct, style, title in points:
        x = pad_l + w * ((when - x0).total_seconds() / span) if span else pad_l + w / 2
        y = pad_t + h - h * pct / 100
        color = STYLE_COLORS.get(style, "#94a3b8")
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6" fill="{color}" stroke="#fff" stroke-width="2">'
                     f'<title>{escape(title)} · {pct:.0f} % · {style}</title></circle>')
    parts.append("</svg>")
    return "".join(parts)
