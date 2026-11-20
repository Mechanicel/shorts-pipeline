"""Kurze Telegram-Benachrichtigungen (Fehler, Statusmeldungen) – nutzbar aus jedem Job."""
from __future__ import annotations

import logging

import httpx

from .config import Settings

log = logging.getLogger(__name__)


def notify(settings: Settings, text: str) -> bool:
    """Schickt eine Nachricht an TELEGRAM_CHAT_ID. Ohne Konfiguration nur Log-Ausgabe."""
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        log.info("[notify, Telegram nicht konfiguriert] %s", text)
        return False
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": settings.telegram_chat_id, "text": text[:4000],
                  "disable_web_page_preview": True},
            timeout=15)
        resp.raise_for_status()
        return True
    except Exception as exc:  # Benachrichtigung darf nie selbst den Job crashen
        log.error("Telegram-Benachrichtigung fehlgeschlagen: %s", type(exc).__name__)
        return False
