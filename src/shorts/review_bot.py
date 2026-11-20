"""Telegram-Review-Bot: verschickt fertige Videos zur Freigabe und verarbeitet die Buttons.

Reagiert ausschließlich auf TELEGRAM_CHAT_ID. Nachrichten aus anderen Chats werden ignoriert
(nur deren Chat-ID wird geloggt, damit man beim Einrichten die eigene ID findet).
"""
from __future__ import annotations

import logging
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from .config import Settings
from .db import Database, Status, Topic, TransitionError
from .notify import notify
from .script import word_count

log = logging.getLogger(__name__)

POLL_SECONDS = 60
TG_TEXT_LIMIT = 4000


def format_review(topic: Topic) -> str:
    """Review-Text: alles, was für Faktencheck und Freigabe nötig ist."""
    s = topic.script or {}
    narration = s.get("narration", "")
    lines = [
        f"🎬 #{topic.id} [{topic.channel}] {s.get('title', topic.title)}",
        f"Quelle: {topic.source_name}",
        topic.url,
        "",
        f"📝 Sprechtext ({word_count(narration)} Wörter):",
        narration,
        "",
        "🔎 Belege:",
    ]
    for ev in s.get("evidence", []):
        lines.append(f"• {ev.get('claim', '')}\n  „{ev.get('quote', '')}“")
    if topic.note:
        lines += ["", f"⚠️ {topic.note}"]
    lines += ["", f"Beschreibung: {s.get('description', '')}",
              f"Hashtags: {' '.join(s.get('hashtags', []))}"]
    text = "\n".join(lines)
    return text if len(text) <= TG_TEXT_LIMIT else text[: TG_TEXT_LIMIT - 20] + "\n… (gekürzt)"


def review_keyboard(topic_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Freigeben", callback_data=f"ok:{topic_id}"),
        InlineKeyboardButton("❌ Ablehnen", callback_data=f"no:{topic_id}"),
    ]])


def parse_callback(data: str) -> tuple[Status, int] | None:
    action, _, raw_id = data.partition(":")
    target = {"ok": Status.FREIGEGEBEN, "no": Status.ABGELEHNT}.get(action)
    if target is None or not raw_id.isdigit():
        return None
    return target, int(raw_id)


class ReviewBot:
    def __init__(self, settings: Settings) -> None:
        if not settings.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN muss in .env gesetzt sein")
        self.s = settings
        # Ohne Chat-ID: Einrichtungsmodus – nur eingehende Chat-IDs loggen, nichts versenden
        self.chat_id: int | None = int(settings.telegram_chat_id) if settings.telegram_chat_id else None
        self.db = Database(settings.db_path)

    def _authorized(self, update: Update) -> bool:
        chat = update.effective_chat
        if chat is not None and self.chat_id is not None and chat.id == self.chat_id:
            return True
        log.warning("Ignoriere Update aus fremdem Chat (chat_id=%s)", chat.id if chat else "?")
        return False

    # -------------------------------------------------------------- Versand
    async def send_pending(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        for topic in self.db.by_status(Status.VIDEO):
            try:
                await self._send_one(context, topic)
            except Exception:
                log.exception("Versand von Thema %s fehlgeschlagen – nächster Versuch in %ss",
                              topic.id, POLL_SECONDS)
                break  # Telegram-Problem: nicht alle Themen durchprobieren

    async def _send_one(self, context: ContextTypes.DEFAULT_TYPE, topic: Topic) -> None:
        if not topic.video_path or not Path(topic.video_path).exists():
            self.db.transition(topic.id, Status.FEHLER, note=f"Videodatei fehlt: {topic.video_path}")
            return
        s = topic.script or {}
        with open(topic.video_path, "rb") as f:
            await context.bot.send_video(
                chat_id=self.chat_id, video=f, supports_streaming=True,
                caption=f"#{topic.id} {s.get('title', topic.title)}"[:1000],
                read_timeout=120, write_timeout=120)
        msg = await context.bot.send_message(
            chat_id=self.chat_id, text=format_review(topic), reply_markup=review_keyboard(topic.id),
            disable_web_page_preview=True)
        self.db.transition(topic.id, Status.WARTET, telegram_message_id=msg.message_id)

    # -------------------------------------------------------------- Handler
    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or not self._authorized(update):
            return
        parsed = parse_callback(query.data or "")
        if parsed is None:
            await query.answer("Unbekannte Aktion")
            return
        target, topic_id = parsed
        try:
            self.db.transition(topic_id, target)
        except (TransitionError, KeyError):
            await query.answer("Schon erledigt.")
            return
        label = "✅ FREIGEGEBEN – wird beim nächsten Upload eingeplant" if target == Status.FREIGEGEBEN \
            else "❌ ABGELEHNT"
        await query.answer(label[:60])
        await query.edit_message_text(f"{query.message.text}\n\n{label}" if query.message else label,
                                      disable_web_page_preview=True)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        counts = self.db.counts()
        text = "\n".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "Keine Themen."
        await update.effective_message.reply_text(text)

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await update.effective_message.reply_text(
            "Ich schicke neue Shorts zur Freigabe.\n/status – Themen je Status")

    async def log_foreign(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._authorized(update)  # loggt fremde Chat-IDs, antwortet nie

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("Bot-Fehler", exc_info=context.error)
        notify(self.s, f"⚠️ Review-Bot-Fehler: {type(context.error).__name__}: {context.error}")

    def build(self) -> Application:
        app = Application.builder().token(self.s.telegram_bot_token).build()
        app.add_error_handler(self.on_error)
        if self.chat_id is None:
            log.warning("TELEGRAM_CHAT_ID fehlt – Einrichtungsmodus: schreib dem Bot, die chat_id "
                        "erscheint hier im Log. Danach in .env eintragen und Bot neu starten.")
            app.add_handler(MessageHandler(filters.ALL, self.log_foreign))
            return app
        mine = filters.Chat(chat_id=self.chat_id)
        app.add_handler(CommandHandler("status", self.cmd_status, filters=mine))
        app.add_handler(CommandHandler(["start", "help"], self.cmd_help, filters=mine))
        app.add_handler(CallbackQueryHandler(self.on_button))
        app.add_handler(MessageHandler(~mine, self.log_foreign))
        app.job_queue.run_repeating(self.send_pending, interval=POLL_SECONDS, first=5)
        return app


def run_bot(settings: Settings) -> None:
    bot = ReviewBot(settings)
    log.info("Review-Bot startet (Chat %s)", bot.chat_id or "– Einrichtungsmodus")
    bot.build().run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
