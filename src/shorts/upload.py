"""YouTube-Upload freigegebener Videos: privat + publishAt auf die Wochen-Slots verteilt (oder --sofort öffentlich).

Quota: videos.insert kostet ca. 1.600 Einheiten, Tageskontingent 10.000; Reset um Mitternacht
Pacific Time. Verbrauch wird in SQLite mitgezählt.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import ChannelConfig, Settings
from .db import Database, Status, Topic
from .notify import notify
from .result import ERROR, OK, WARN, plural, report

log = logging.getLogger(__name__)

BERLIN = ZoneInfo("Europe/Berlin")
PACIFIC = ZoneInfo("America/Los_Angeles")
UPLOAD_COST = 1600
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
MIN_LEAD = timedelta(hours=2)  # publishAt muss in der Zukunft liegen; etwas Puffer
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


# ------------------------------------------------------------------ Slots
def parse_slot(slot: str) -> tuple[int, int, int]:
    """'Mon 17:00' → (weekday, hour, minute)."""
    day, hm = slot.split()
    h, m = hm.split(":")
    return WEEKDAYS[day.lower()[:3]], int(h), int(m)


def to_utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def next_free_slot(slots: list[str], taken: set[str], now: datetime, horizon_days: int = 28) -> str:
    """Nächster freier Slot (UTC-ISO), mindestens MIN_LEAD in der Zukunft."""
    if not slots:
        raise ValueError("Keine publish_slots konfiguriert")
    parsed = [parse_slot(s) for s in slots]
    local_now = now.astimezone(BERLIN)
    for offset in range(horizon_days + 1):
        day = (local_now + timedelta(days=offset)).date()
        for wd, h, m in sorted(p for p in parsed if p[0] == day.weekday()):
            candidate = datetime(day.year, day.month, day.day, h, m, tzinfo=BERLIN)
            iso = to_utc_iso(candidate)
            if candidate >= now + MIN_LEAD and iso not in taken:
                return iso
    raise RuntimeError(f"Kein freier Slot in den nächsten {horizon_days} Tagen")


def upcoming_free_slots(slots: list[str], taken: set[str], now: datetime, n: int = 5) -> list[str]:
    """Die nächsten n freien Termine (UTC-ISO) – für Dropdowns und Vorschau."""
    out, blocked = [], set(taken)
    while len(out) < n:
        try:
            slot = next_free_slot(slots, blocked, now)
        except (RuntimeError, ValueError):
            break
        out.append(slot)
        blocked.add(slot)
    return out


def quota_day(now: datetime) -> str:
    return now.astimezone(PACIFIC).date().isoformat()


# ------------------------------------------------------------------ Metadaten
def build_metadata(topic: Topic, channel: ChannelConfig, publish_at: str | None) -> dict:
    """publish_at=None → sofort öffentlich; sonst privat mit geplanter Veröffentlichung."""
    s = topic.script or {}
    hashtags = list(dict.fromkeys(s.get("hashtags", []) + channel.default_hashtags))  # ohne Duplikate
    title = s.get("title", topic.title).replace("<", "").replace(">", "")[:100]
    credit = f"\n{topic.background_credit}" if topic.background_credit else ""
    description = (f"{s.get('description', '')}\n\nQuelle: {topic.url}{credit}\n\n"
                   f"Alle Angaben ohne Gewähr, Stand bei Veröffentlichung.\n\n{' '.join(hashtags)}")
    return {
        "snippet": {
            "title": title,
            "description": description.replace("<", "").replace(">", "")[:4900],
            "tags": [h.lstrip("#") for h in hashtags][:15],
            "categoryId": channel.youtube_category_id,
            "defaultLanguage": channel.language,
            "defaultAudioLanguage": channel.language,
        },
        "status": {
            "privacyStatus": "private" if publish_at else "public",
            **({"publishAt": publish_at} if publish_at else {}),
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": channel.contains_synthetic_media,
        },
    }


STUDIO_URL = "https://studio.youtube.com/video/{id}/edit"
WOCHENTAGE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def format_upload_line(topic: Topic, title: str, video_id: str, slot: str | None) -> str:
    """Telegram-Zeile nach dem Upload: Termin, an dem YouTube das Video automatisch veröffentlicht
    (oder 'sofort öffentlich'), plus Studio-Link als Rückfallebene (ohne API-Audit nicht garantiert)."""
    if slot is None:
        return (f"#{topic.id} {title}\n🌍 sofort öffentlich hochgeladen\n"
                f"https://youtu.be/{video_id}\nStudio: {STUDIO_URL.format(id=video_id)}")
    local = datetime.fromisoformat(slot.replace("Z", "+00:00")).astimezone(BERLIN)
    return (f"#{topic.id} {title}\n"
            f"📅 geplant: {WOCHENTAGE[local.weekday()]} {local:%d.%m. %H:%M} (geht automatisch live)\n"
            f"Studio: {STUDIO_URL.format(id=video_id)}")


# ------------------------------------------------------------------ API
def youtube_client(channel: ChannelConfig):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    path = channel.youtube_token
    if not path.exists():
        raise RuntimeError(f"Token-Datei fehlt: {path.name} (auth_youtube.py ausführen, siehe README)")
    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if not creds.valid:
        if not creds.refresh_token:
            raise RuntimeError("Token ohne refresh_token – auth_youtube.py erneut ausführen")
        creds.refresh(Request())
        path.write_text(creds.to_json(), encoding="utf-8")
        path.chmod(0o600)
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def upload_video(youtube, path: str, body: dict) -> str:
    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(path, mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk(num_retries=3)
        if status:
            log.info("Upload %d%%", int(status.progress() * 100))
    return response["id"]


def _is_quota_error(exc: Exception) -> bool:
    text = str(exc)
    return "quotaExceeded" in text or "uploadLimitExceeded" in text or "dailyLimitExceeded" in text


def parse_publish_at(text: str, now: datetime) -> str:
    """'2026-11-20 20:30' (Berliner Zeit) → UTC-ISO; muss mind. 15 Minuten in der Zukunft liegen."""
    local = datetime.strptime(text.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=BERLIN)
    if local < now + timedelta(minutes=15):
        raise ValueError(f"--publish-at {text} liegt nicht mindestens 15 Minuten in der Zukunft")
    return to_utc_iso(local)


def quota_reset(now: datetime) -> datetime:
    """Nächster Kontingent-Reset (Mitternacht Pacific Time) in Berliner Zeit."""
    pt = now.astimezone(PACIFIC)
    nxt = datetime(pt.year, pt.month, pt.day, tzinfo=PACIFIC) + timedelta(days=1)
    return nxt.astimezone(BERLIN)


def quota_status(settings: Settings, db: Database, now: datetime) -> dict:
    """Verbrauch heute, freie Einheiten, mögliche Uploads und Reset-Zeitpunkt (für CLI und Web)."""
    used = db.quota_used(quota_day(now))
    free = max(settings.youtube_daily_quota - used, 0)
    by_quota = free // UPLOAD_COST
    by_daily_limit = max(settings.youtube_max_uploads_per_day - used // UPLOAD_COST, 0)
    reset = quota_reset(now)
    reset_txt = f"{WOCHENTAGE[reset.weekday()]} {reset:%d.%m. %H:%M}"
    if by_daily_limit < by_quota:
        reason = f"Tageslimit ({settings.youtube_max_uploads_per_day} Uploads), wieder ab {reset_txt}"
    else:
        reason = f"Kontingent ({used}/{settings.youtube_daily_quota}), wieder ab {reset_txt}"
    return {"used": used, "limit": settings.youtube_daily_quota, "free": free,
            "possible": max(min(by_quota, by_daily_limit), 0), "reset": reset_txt, "reason": reason}


def run_uploads(settings: Settings, db: Database, channel: ChannelConfig, *, dry_run: bool = False,
                now: datetime | None = None, publish_at: str | None = None, immediate: bool = False,
                only: int | None = None) -> int:
    """publish_at (UTC-ISO) gilt nur für das erste Video der Warteschlange (Test eines Wunschtermins).
    immediate=True: alle Videos dieses Laufs sofort öffentlich, ohne Termin.
    Meldet ehrlich, wie viele von wie vielen hochgeladen wurden (result.report)."""
    now = now or datetime.now(timezone.utc)
    pending = sorted(db.by_status(Status.FREIGEGEBEN, channel.key), key=lambda t: (t.updated_at, t.id))
    if only is not None:  # einzelnes Video (z. B. aus dem Webinterface mit eigenem Termin)
        pending = [t for t in pending if t.id == only]
    if not pending:
        log.info("Keine freigegebenen Videos für %s", channel.key)
        report(WARN, "Nichts zu tun – keine freigegebenen Videos.")
        return 0

    day = quota_day(now)
    q = quota_status(settings, db, now)
    budget = q["possible"]
    log.info("%d freigegeben, Quota heute %d/%d → max. %d Uploads", len(pending), q["used"], q["limit"], budget)
    quota_hint = q["reason"]
    if budget <= 0:
        summary = f"0 von {plural(len(pending), 'Video', 'Videos')} hochgeladen – {quota_hint}"
        if not dry_run:
            notify(settings, f"⚠️ Upload [{channel.key}]: {summary}")
        report(WARN, summary)
        return 0
    taken = db.taken_publish_slots(channel.key)

    youtube = None if dry_run else youtube_client(channel)
    done, errors, quota_hit = [], [], False
    for topic in pending[:budget]:
        # Bereits zugewiesenen Slot (z. B. nach Retry) behalten, wenn er noch in der Zukunft liegt
        slot = topic.publish_at
        if immediate:
            slot = None
        elif publish_at:
            slot, publish_at = publish_at, None  # Wunschtermin nur für das erste Video
        elif not slot or datetime.fromisoformat(slot.replace("Z", "+00:00")) < now + MIN_LEAD:
            slot = next_free_slot(channel.publish_slots, taken, now)
        if slot:
            taken.add(slot)
        body = build_metadata(topic, channel, slot)
        if dry_run:
            print(f"[dry-run] #{topic.id} → {slot or 'sofort öffentlich'}  {body['snippet']['title']}")
            continue
        db.update_fields(topic.id, publish_at=slot)  # sofort → kein Termin (kein Schein-Slot mehr)
        try:
            video_id = upload_video(youtube, topic.video_path, body)
        except Exception as exc:
            if _is_quota_error(exc):
                db.add_quota(day, settings.youtube_daily_quota)  # Tag als verbraucht markieren
                quota_hit = True
                break
            log.exception("Upload #%s fehlgeschlagen", topic.id)
            db.transition(topic.id, Status.FEHLER, note=f"Upload: {type(exc).__name__}: {exc}"[:1000])
            errors.append(f"#{topic.id}: {type(exc).__name__}: {exc}"[:300])
            continue
        db.add_quota(day, UPLOAD_COST)
        extra = {"yt_published_at": to_utc_iso(now)} if slot is None else {}
        db.transition(topic.id, Status.HOCHGELADEN, youtube_id=video_id, **extra)
        print(f"Hochgeladen: #{topic.id} → Video-ID: {video_id}  https://youtu.be/{video_id}  "
              + ("(sofort öffentlich)" if slot is None else f"(privat, geplant {slot})"), flush=True)
        done.append(format_upload_line(topic, body["snippet"]["title"], video_id, slot))

    if dry_run:
        report(OK, f"Dry-Run: {plural(min(budget, len(pending)), 'Video', 'Videos')} würden hochgeladen.")
        return 0

    n, total = len(done), len(pending)
    reasons = []
    if quota_hit or (total - n - len(errors) > 0 and budget < total):
        reasons.append(quota_hint)
    if errors:
        reasons.append(plural(len(errors), "Fehler", "Fehler"))
    summary = f"{n} von {plural(total, 'Video', 'Videos')} hochgeladen" + (f" – {'; '.join(reasons)}" if reasons else "")
    status = OK if n == total else (ERROR if n == 0 and errors else WARN)

    msg = f"📤 Upload [{channel.key}]: {summary}"
    if done:
        msg += "\n\n" + "\n\n".join(done)
    if errors:
        msg += "\n⚠️ " + "\n⚠️ ".join(errors)
    notify(settings, msg)
    report(status, summary)
    return 1 if status == ERROR else 0
