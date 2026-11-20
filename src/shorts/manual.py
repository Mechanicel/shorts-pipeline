"""Manueller Upload-Modus (Standard, solange das YouTube-API-Projekt nicht auditiert ist).

Der tägliche Upload-Job lädt nichts hoch. Er vergibt jedem freigegebenen Video einen Termin
(gleiche Slot-Logik wie der API-Upload) und schickt am Termin-Tag ein Upload-Paket an Telegram:
MP4 als Datei (Originalqualität), Titel, Beschreibung, Tags, Termin – jeweils als eigene,
einzeln kopierbare Nachricht. Der Button „✅ Hochgeladen“ (verarbeitet vom Review-Bot) schließt ab.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import httpx

from .config import ChannelConfig, Settings
from .db import Database, Status, Topic
from .notify import notify
from .result import ERROR, OK, WARN, plural, report
from .upload import BERLIN, WOCHENTAGE, build_metadata, next_free_slot

log = logging.getLogger(__name__)

TG_LIMIT = 4096


class TelegramSender(Protocol):
    def send_document(self, path: Path, caption: str) -> int: ...
    def send_message(self, text: str, reply_markup: dict | None = None) -> int: ...


class HttpTelegramSender:
    def __init__(self, settings: Settings) -> None:
        if not (settings.telegram_bot_token and settings.telegram_chat_id):
            raise RuntimeError("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID fehlen – Upload-Pakete brauchen Telegram")
        self._base = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
        self._chat = settings.telegram_chat_id

    def _ok(self, resp: httpx.Response) -> int:
        resp.raise_for_status()
        return int(resp.json()["result"]["message_id"])

    def send_document(self, path: Path, caption: str) -> int:
        with path.open("rb") as f:
            return self._ok(httpx.post(f"{self._base}/sendDocument", timeout=180, data={
                "chat_id": self._chat, "caption": caption[:1000], "disable_content_type_detection": "true"},
                files={"document": (path.name, f, "video/mp4")}))

    def send_message(self, text: str, reply_markup: dict | None = None) -> int:
        payload: dict = {"chat_id": self._chat, "text": text[:TG_LIMIT], "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self._ok(httpx.post(f"{self._base}/sendMessage", timeout=30, json=payload))


def slot_local(slot: str) -> datetime:
    return datetime.fromisoformat(slot.replace("Z", "+00:00")).astimezone(BERLIN)


def format_slot(slot: str) -> str:
    local = slot_local(slot)
    return f"{WOCHENTAGE[local.weekday()]} {local:%d.%m. %H:%M}"


def is_due(slot: str, now: datetime) -> bool:
    """Fällig am Termin-Tag (Berliner Datum) oder wenn der Termin schon vorbei ist."""
    return slot_local(slot).date() <= now.astimezone(BERLIN).date()


def assign_slots(db: Database, channel: ChannelConfig, now: datetime) -> list[tuple[Topic, str]]:
    """Gibt allen freigegebenen Videos einen Termin (in Freigabe-Reihenfolge) und speichert ihn."""
    taken = db.taken_publish_slots(channel.key)
    planned = []
    for topic in sorted(db.by_status(Status.FREIGEGEBEN, channel.key), key=lambda t: (t.updated_at, t.id)):
        slot = topic.publish_at
        if not slot:
            slot = next_free_slot(channel.publish_slots, taken, now)
            taken.add(slot)
            db.update_fields(topic.id, publish_at=slot)
        planned.append((db.get(topic.id), slot))
    return planned


def package_messages(topic: Topic, channel: ChannelConfig, slot: str) -> dict:
    """Inhalte des Upload-Pakets (rein, ohne Versand – testbar)."""
    meta = build_metadata(topic, channel, slot)
    sn = meta["snippet"]
    when = format_slot(slot)
    return {
        "caption": f"📦 #{topic.id} Upload-Paket · Termin {when}\nEs folgen: Titel, Beschreibung, Tags.",
        "title": sn["title"],
        "description": sn["description"],
        "tags": ", ".join(sn["tags"]),
        "final": (f"🗓 #{topic.id} vorgeschlagener Termin: {when}\n\n"
                  "1. MP4 oben speichern → studio.youtube.com → Erstellen → Video hochladen\n"
                  "2. Titel, Beschreibung, Tags aus den Nachrichten oben kopieren\n"
                  "3. Nicht für Kinder · Sichtbarkeit: Planen → Termin oben\n"
                  "4. Danach hier bestätigen:"),
        "button": {"inline_keyboard": [[{"text": "✅ Hochgeladen", "callback_data": f"up:{topic.id}"}]]},
    }


def send_package(sender: TelegramSender, topic: Topic, channel: ChannelConfig, slot: str) -> int:
    if not topic.video_path or not Path(topic.video_path).exists():
        raise FileNotFoundError(f"Videodatei fehlt: {topic.video_path}")
    msgs = package_messages(topic, channel, slot)
    sender.send_document(Path(topic.video_path), msgs["caption"])
    for key in ("title", "description", "tags"):  # je eine Nachricht → einzeln kopierbar
        sender.send_message(msgs[key])
    return sender.send_message(msgs["final"], reply_markup=msgs["button"])


def run_manual(settings: Settings, db: Database, channel: ChannelConfig, *, dry_run: bool = False,
               now: datetime | None = None, sender: TelegramSender | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    if dry_run:  # nur anzeigen, nichts speichern
        taken = db.taken_publish_slots(channel.key)
        for t in sorted(db.by_status(Status.FREIGEGEBEN, channel.key), key=lambda t: (t.updated_at, t.id)):
            slot = t.publish_at or next_free_slot(channel.publish_slots, taken, now)
            taken.add(slot)
            print(f"[dry-run] #{t.id} → {format_slot(slot)} {'(fällig)' if is_due(slot, now) else ''}")
        return 0

    planned = assign_slots(db, channel, now)
    due = [(t, s) for t, s in planned if is_due(s, now)]
    log.info("Manueller Modus: %d freigegeben, %d heute fällig", len(planned), len(due))
    if not due:
        report(WARN, "Nichts fällig – " + (f"{plural(len(planned), 'Video wartet', 'Videos warten')} auf spätere Termine."
                                          if planned else "keine freigegebenen Videos."))
        return 0
    sender = sender or HttpTelegramSender(settings)
    errors = []
    for topic, slot in due:
        try:
            msg_id = send_package(sender, topic, channel, slot)
        except Exception as exc:
            log.exception("Upload-Paket #%s fehlgeschlagen", topic.id)
            errors.append(f"#{topic.id}: {type(exc).__name__}")  # Topic bleibt freigegeben → morgen erneut
            continue
        db.transition(topic.id, Status.PAKET, telegram_message_id=msg_id)
    if errors:
        notify(settings, "⚠️ Upload-Pakete fehlgeschlagen (nächster Versuch morgen): " + ", ".join(errors))
    sent = len(due) - len(errors)
    summary = f"{sent} von {plural(len(due), 'Upload-Paket', 'Upload-Paketen')} an Telegram geschickt"
    status = OK if not errors else (ERROR if sent == 0 else WARN)
    report(status, summary + (f" – {plural(len(errors), 'Fehler', 'Fehler')}" if errors else ""))
    return 1 if status == ERROR else 0
