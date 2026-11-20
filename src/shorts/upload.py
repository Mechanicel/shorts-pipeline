"""YouTube-Upload freigegebener Videos: privat + publishAt auf die Wochen-Slots verteilt.

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


def quota_day(now: datetime) -> str:
    return now.astimezone(PACIFIC).date().isoformat()


# ------------------------------------------------------------------ Metadaten
def build_metadata(topic: Topic, channel: ChannelConfig, publish_at: str) -> dict:
    s = topic.script or {}
    hashtags = list(dict.fromkeys(s.get("hashtags", []) + channel.default_hashtags))  # ohne Duplikate
    title = s.get("title", topic.title).replace("<", "").replace(">", "")[:100]
    description = (f"{s.get('description', '')}\n\nQuelle: {topic.url}\n\n"
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
            "privacyStatus": "private",
            "publishAt": publish_at,
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": channel.contains_synthetic_media,
        },
    }


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


def run_uploads(settings: Settings, db: Database, channel: ChannelConfig, *, dry_run: bool = False,
                now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    pending = sorted(db.by_status(Status.FREIGEGEBEN, channel.key), key=lambda t: t.updated_at)
    if not pending:
        log.info("Keine freigegebenen Videos für %s", channel.key)
        return 0

    day = quota_day(now)
    used = db.quota_used(day)
    budget = min(settings.youtube_max_uploads_per_day,
                 (settings.youtube_daily_quota - used) // UPLOAD_COST)
    log.info("%d freigegeben, Quota heute %d/%d → max. %d Uploads", len(pending), used,
             settings.youtube_daily_quota, budget)
    taken = db.taken_publish_slots(channel.key)

    youtube = None if dry_run else youtube_client(channel)
    done, errors = [], []
    for topic in pending[:max(budget, 0)]:
        # Bereits zugewiesenen Slot (z. B. nach Retry) behalten, wenn er noch in der Zukunft liegt
        slot = topic.publish_at
        if not slot or datetime.fromisoformat(slot.replace("Z", "+00:00")) < now + MIN_LEAD:
            slot = next_free_slot(channel.publish_slots, taken, now)
        taken.add(slot)
        body = build_metadata(topic, channel, slot)
        if dry_run:
            print(f"[dry-run] #{topic.id} → {slot}  {body['snippet']['title']}")
            continue
        db.update_fields(topic.id, publish_at=slot)
        try:
            video_id = upload_video(youtube, topic.video_path, body)
        except Exception as exc:
            if _is_quota_error(exc):
                db.add_quota(day, settings.youtube_daily_quota)  # Tag als verbraucht markieren
                errors.append(f"Quota erschöpft bei #{topic.id} – Rest morgen")
                break
            log.exception("Upload #%s fehlgeschlagen", topic.id)
            db.transition(topic.id, Status.FEHLER, note=f"Upload: {type(exc).__name__}: {exc}"[:1000])
            errors.append(f"#{topic.id}: {type(exc).__name__}: {exc}"[:300])
            continue
        db.add_quota(day, UPLOAD_COST)
        db.transition(topic.id, Status.HOCHGELADEN, youtube_id=video_id)
        local = datetime.fromisoformat(slot.replace("Z", "+00:00")).astimezone(BERLIN)
        done.append(f"#{topic.id} → {local:%a %d.%m. %H:%M} https://youtu.be/{video_id}")

    if not dry_run and (done or errors):
        msg = f"📤 Upload [{channel.key}]: {len(done)} eingeplant"
        if done:
            msg += "\n" + "\n".join(done)
        if errors:
            msg += "\n⚠️ " + "\n⚠️ ".join(errors)
        rest = len(pending) - len(done)
        if rest > 0:
            msg += f"\n{rest} warten auf den nächsten Upload-Lauf."
        notify(settings, msg)
    return 1 if errors and not done else 0
