"""YouTube-Upload freigegebener Videos: privat + publishAt auf die Wochen-Slots verteilt (oder --sofort öffentlich).

Kontingent: videos.insert hat einen eigenen Topf (100/Tag, Reset Mitternacht Pacific Time); maßgeblich sind
Googles Fehlermeldungen. Fristen: Videos mit Frist/Stichtag erscheinen mindestens 3 Tage vorher (deadline.py).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import ChannelConfig, Settings
from .deadline import choose_slot, deadline_of
from .merkliste import STYLE_V3, description_v3
from .db import Database, Status, Topic
from .notify import notify
from .result import ERROR, OK, WARN, plural, report

log = logging.getLogger(__name__)

BERLIN = ZoneInfo("Europe/Berlin")
PACIFIC = ZoneInfo("America/Los_Angeles")
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
    if s.get("style") == STYLE_V3:  # Merkliste: komplette Liste mit Status in die Beschreibung
        description = f"{description_v3(s, topic.url, topic.background_credit)}\n\n{' '.join(hashtags)}"
    else:
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
    # Scopes aus der Datei übernehmen (Upload + ggf. Analytics), damit ein Refresh keine Rechte aus der Datei streicht
    creds = Credentials.from_authorized_user_file(str(path))
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
        status, response = request.next_chunk()  # Retries steuert upload_with_retry
        if status:
            log.info("Upload %d%%", int(status.progress() * 100))
    return response["id"]


# ------------------------------------------------------------------ Google-Fehler auswerten
STOP_REASONS = {
    "quotaExceeded": "Google meldet quotaExceeded (Projekt-Kontingent erschöpft)",
    "uploadLimitExceeded": "Google meldet uploadLimitExceeded (Upload-Limit des Kanals erreicht)",
}
RETRY_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "backendError", "internalError"}
RETRY_ATTEMPTS = 3


class UploadStopped(Exception):
    """Kontingent/Upload-Limit: Batch sofort beenden, nicht wiederholen."""

    def __init__(self, reason: str) -> None:
        super().__init__(STOP_REASONS.get(reason, reason))
        self.reason = reason


def http_error_reason(exc: Exception) -> tuple[str, int]:
    """(reason, HTTP-Status) aus einem googleapiclient-HttpError (error.errors[0].reason)."""
    import json as _json

    status = int(getattr(getattr(exc, "resp", None), "status", 0) or 0)
    reason = ""
    try:
        content = exc.content.decode() if isinstance(exc.content, bytes) else str(exc.content)
        err = _json.loads(content).get("error", {})
        reason = (err.get("errors") or [{}])[0].get("reason", "") or err.get("status", "")
    except Exception:
        pass
    return reason, status


def upload_with_retry(youtube, path: str, body: dict, sleep=time.sleep) -> str:
    """Retry mit exponentiellem Backoff nur bei Rate-Limits, 5xx und Netzwerkfehlern."""
    from googleapiclient.errors import HttpError

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return upload_video(youtube, path, body)
        except HttpError as exc:
            reason, status = http_error_reason(exc)
            if reason in STOP_REASONS:
                raise UploadStopped(reason) from exc
            retryable = reason in RETRY_REASONS or status >= 500
            if not retryable or attempt == RETRY_ATTEMPTS:
                raise
            log.warning("Upload-Versuch %d fehlgeschlagen (%s %s) – neuer Versuch", attempt, status, reason)
        except (ConnectionError, TimeoutError, OSError) as exc:
            if attempt == RETRY_ATTEMPTS:
                raise
            log.warning("Upload-Versuch %d: Netzwerkfehler %s – neuer Versuch", attempt, type(exc).__name__)
        sleep(2 ** attempt)  # 2 s, 4 s
    raise RuntimeError("unreachable")


def parse_publish_at(text: str, now: datetime) -> str:
    """'2026-11-20 20:30' (Berliner Zeit) → UTC-ISO; muss mind. 15 Minuten in der Zukunft liegen."""
    local = datetime.strptime(text.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=BERLIN)
    if local < now + timedelta(minutes=15):
        raise ValueError(f"--publish-at {text} liegt nicht mindestens 15 Minuten in der Zukunft")
    return to_utc_iso(local)


def quota_reset(now: datetime) -> datetime:
    """Nächster YouTube-Reset: Mitternacht America/Los_Angeles (zoneinfo, sommerzeitfest), in Berliner Zeit."""
    pt = now.astimezone(PACIFIC)
    nxt = datetime(pt.year, pt.month, pt.day, tzinfo=PACIFIC) + timedelta(days=1)
    return nxt.astimezone(BERLIN)


def _fmt(dt: datetime) -> str:
    return f"{WOCHENTAGE[dt.weekday()]} {dt:%d.%m. %H:%M}"


def active_block(db: Database, now: datetime) -> dict | None:
    """Von Google gemeldete Sperre (quotaExceeded/uploadLimitExceeded), solange der Reset nicht erreicht ist."""
    block = db.get_setting("upload_block")
    if block and now < datetime.fromisoformat(block["until"]):
        return block
    return None


def set_block(db: Database, reason: str, now: datetime) -> dict:
    block = {"reason": reason, "reported_at": now.isoformat(), "until": quota_reset(now).isoformat(),
             "notified_scheduled": False}
    db.set_setting("upload_block", block)
    return block


def upload_status(settings: Settings, db: Database, now: datetime) -> dict:
    """Nur zur Anzeige: eigene Upload-Zählung + Reset-Zeit + ggf. Googles Sperre. Blockiert nichts."""
    reset = quota_reset(now)
    block = active_block(db, now)
    info = {"today": db.uploads_on(quota_day(now)), "limit": settings.yt_uploads_per_day,
            "reset": _fmt(reset), "reset_time": f"{reset:%H:%M}", "block": None}
    if block:
        reported = datetime.fromisoformat(block["reported_at"]).astimezone(BERLIN)
        until = datetime.fromisoformat(block["until"]).astimezone(BERLIN)
        info["block"] = {"reason": block["reason"], "reported": f"{reported:%H:%M}", "until": f"{until:%H:%M}",
                         "text": f"Google hat das Kontingent um {reported:%H:%M} gemeldet ({block['reason']}), "
                                 f"Reset um {until:%H:%M}."}
    return info


def run_uploads(settings: Settings, db: Database, channel: ChannelConfig, *, dry_run: bool = False,
                now: datetime | None = None, publish_at: str | None = None, immediate: bool = False,
                only: int | None = None, scheduled: bool = False, sleep=time.sleep) -> int:
    """Lädt freigegebene Videos hoch. Keine Vorab-Blockade: Uploads werden versucht, Googles Fehler entscheiden.

    publish_at (UTC-ISO): Wunschtermin fürs erste Video · immediate: alle sofort öffentlich ·
    only: nur dieses Thema · scheduled: Timer-Lauf (wird bei gemeldeter Sperre bis zum Reset übersprungen).
    """
    now = now or datetime.now(timezone.utc)
    if scheduled and (block := active_block(db, now)):
        until = datetime.fromisoformat(block["until"]).astimezone(BERLIN)
        summary = f"Übersprungen – Google hat {block['reason']} gemeldet, Reset {_fmt(until)}"
        if not block.get("notified_scheduled"):
            notify(settings, f"⏸ Upload [{channel.key}]: {summary}. Manuell im Webinterface weiterhin möglich.")
            db.set_setting("upload_block", {**block, "notified_scheduled": True})
        report(WARN, summary)
        return 0

    pending = sorted(db.by_status(Status.FREIGEGEBEN, channel.key), key=lambda t: (t.updated_at, t.id))
    if only is not None:  # einzelnes Video (z. B. aus dem Webinterface mit eigenem Termin)
        pending = [t for t in pending if t.id == only]
    if not pending:
        log.info("Keine freigegebenen Videos für %s", channel.key)
        report(WARN, "Nichts zu tun – keine freigegebenen Videos.")
        return 0

    day = quota_day(now)
    log.info("%d freigegeben · Uploads heute laut eigener Zählung: %d/%d (nur Info)",
             len(pending), db.uploads_on(day), settings.yt_uploads_per_day)
    taken = db.taken_publish_slots(channel.key)
    youtube = None if dry_run else youtube_client(channel)
    done, errors, stopped, deadline_skips = [], [], None, []
    for topic in pending:
        slot = topic.publish_at
        if immediate:
            slot = None
        elif publish_at:
            slot, publish_at = publish_at, None  # Wunschtermin nur für das erste Video
        elif not slot or datetime.fromisoformat(slot.replace("Z", "+00:00")) < now + MIN_LEAD:
            choice = choose_slot(upcoming_free_slots(channel.publish_slots, taken, now, 40),
                                 deadline_of(topic.script, now.date()), now)
            if choice.warning:
                log.warning("#%s: %s", topic.id, choice.warning)
            if choice.slot is None:  # Frist: kein Termin früh genug → nicht stillschweigend zu spät planen
                deadline_skips.append(f"#{topic.id}: {choice.warning}")
                continue
            slot = choice.slot
        body = build_metadata(topic, channel, slot)
        if dry_run:
            if slot:
                taken.add(slot)
            print(f"[dry-run] #{topic.id} → {slot or 'sofort öffentlich'}  {body['snippet']['title']}")
            continue
        try:
            video_id = upload_with_retry(youtube, topic.video_path, body, sleep=sleep)
        except UploadStopped as exc:
            stopped = exc  # dieses + alle restlichen Videos bleiben unverändert "freigegeben"
            set_block(db, exc.reason, now)
            break
        except Exception as exc:
            log.exception("Upload #%s fehlgeschlagen", topic.id)
            db.transition(topic.id, Status.FEHLER, note=f"Upload: {type(exc).__name__}: {exc}"[:1000])
            errors.append(f"#{topic.id}: {type(exc).__name__}: {str(exc)[:200]}")
            continue
        if slot:
            taken.add(slot)
        db.count_upload(day)
        extra = {"yt_published_at": to_utc_iso(now)} if slot is None else {}
        db.transition(topic.id, Status.HOCHGELADEN, youtube_id=video_id, publish_at=slot, **extra)
        print(f"Hochgeladen: #{topic.id} → Video-ID: {video_id}  https://youtu.be/{video_id}  "
              + ("(sofort öffentlich)" if slot is None else f"(privat, geplant {slot})"), flush=True)
        done.append(format_upload_line(topic, body["snippet"]["title"], video_id, slot))

    if dry_run:
        report(OK, f"Dry-Run: {plural(len(pending), 'Video', 'Videos')} würden hochgeladen.")
        return 0

    if deadline_skips:
        notify(settings, f"⏰ Upload [{channel.key}] – Frist zu knapp, nicht eingeplant:\n" + "\n".join(deadline_skips)
               + "\nIm Webinterface „sofort öffentlich“ wählen oder Freigabe zurücknehmen.")
    n, total = len(done), len(pending)
    reasons = []
    reset_txt = f"{quota_reset(now):%H:%M}"
    if stopped:
        reasons.append(f"Google meldet {stopped.reason}, Reset {reset_txt}")
    if errors:
        reasons.append(plural(len(errors), "Fehler", "Fehler"))
    if deadline_skips:
        reasons.append(f"{len(deadline_skips)} wegen Frist nicht eingeplant (sofort veröffentlichen?)")
    summary = f"{n} von {plural(total, 'Video', 'Videos')} hochgeladen" + (f" – {'; '.join(reasons)}" if reasons else "")
    status = OK if n == total else (ERROR if n == 0 and errors and not stopped else WARN)

    msg = f"📤 Upload [{channel.key}]: {summary}"
    if done:
        msg += "\n\n" + "\n\n".join(done)
    if stopped:
        still_open = len(db.by_status(Status.FREIGEGEBEN, channel.key))
        msg += (f"\n\n⛔ Abbruch: {stopped}. {plural(still_open, 'Video bleibt', 'Videos bleiben')} freigegeben. "
                f"Reset um {reset_txt} (Mitternacht Pacific Time); Timer-Uploads pausieren bis dahin, "
                "manuell im Webinterface weiter möglich.")
    if errors:
        msg += "\n⚠️ " + "\n⚠️ ".join(errors)
    notify(settings, msg)
    report(status, summary)
    return 1 if status == ERROR else 0
