"""Aktionen, die Telegram-Bot, CLI und Webinterface gemeinsam nutzen."""
from __future__ import annotations

import logging

import httpx

from .config import Settings
from .db import Database, Status, Topic

log = logging.getLogger(__name__)


def retire_review_message(settings: Settings, message_id: int | None, text: str) -> None:
    """Buttons der Telegram-Review-Nachricht entfernen und kurz vermerken, was passiert ist."""
    if not (message_id and settings.telegram_bot_token and settings.telegram_chat_id):
        return
    base = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
    try:
        httpx.post(f"{base}/editMessageReplyMarkup", timeout=15, json={
            "chat_id": settings.telegram_chat_id, "message_id": message_id,
            "reply_markup": {"inline_keyboard": []}})
        httpx.post(f"{base}/sendMessage", timeout=15, json={
            "chat_id": settings.telegram_chat_id, "reply_to_message_id": message_id, "text": text})
    except Exception as exc:  # Telegram-Probleme dürfen die Aktion nicht verhindern
        log.warning("Telegram-Nachricht nicht aktualisiert: %s", type(exc).__name__)


def decide(settings: Settings, db: Database, topic_id: int, approve: bool, source: str = "Webinterface") -> Topic:
    """Freigeben/Ablehnen außerhalb von Telegram (z. B. im Webinterface)."""
    topic = db.transition(topic_id, Status.FREIGEGEBEN if approve else Status.ABGELEHNT)
    word = "✅ freigegeben" if approve else "❌ abgelehnt"
    retire_review_message(settings, topic.telegram_message_id, f"#{topic_id} {word} (über {source}).")
    return topic


def regenerate(settings: Settings, db: Database, topic_id: int) -> Topic:
    """Thema (wartend/abgelehnt/fehler) mit neuem Skript + Video neu einreihen."""
    t = db.get(topic_id)
    if t.status == Status.WARTET:
        db.transition(t.id, Status.ABGELEHNT, note="abgelehnt zur Neuerzeugung")
    elif t.status not in (Status.ABGELEHNT, Status.FEHLER):
        raise ValueError(f"Thema {t.id} ist im Status {t.status} – regenerate geht nur für "
                         "wartend/abgelehnt/fehler.")
    old_msg = t.telegram_message_id
    topic = db.regenerate(t.id)
    retire_review_message(settings, old_msg, f"❌ #{topic_id} abgelehnt – wird beim nächsten Lauf neu erzeugt.")
    return topic
