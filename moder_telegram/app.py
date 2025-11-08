"""Entry point and wiring for the Telegram moderation bot.

Design notes:
- Keep `main.py` as a thin runner that calls `moder_telegram.app.run()`.
- The bot logic must be small and delegate pure functions to `moderation.py`
  so they are unit-testable without running the bot.

"""
import asyncio
import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Optional

import asyncio
import html
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher
from aiogram.types import Message

from .moderation import is_bad_message
from . import storage

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", None)
AUDIT_CHAT_IDS_RAW = os.environ.get("AUDIT_CHAT_IDS", "")


def _get_audit_chats() -> list[int]:
    ids: list[int] = []
    raw = AUDIT_CHAT_IDS_RAW
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            logger.warning("Invalid audit chat id in AUDIT_CHAT_IDS: %s", part)
    return ids


def _user_display_from_message_user(u) -> str:
    """Return a display name (prefer username) for a User object; safe-escaped."""
    if not u:
        return "user"
    if getattr(u, "username", None):
        return html.escape(f"@{u.username}")
    name = getattr(u, "first_name", "")
    last = getattr(u, "last_name", "")
    full = (name + " " + last).strip()
    return html.escape(full or str(getattr(u, "id", "user")))


def _extract_target_and_reason(parts: list[str], message: Message):
    """Return (target_uid or None, reason or None, replied_user_object or None).

    If command is used as a reply, target comes from replied user and reason is
    the rest of parts after command. If an id is provided, reason is the rest
    after id.
    """
    target_uid = None
    reason = None
    replied_user = None
    if message.reply_to_message and message.reply_to_message.from_user:
        replied_user = message.reply_to_message.from_user
        target_uid = replied_user.id
        if len(parts) >= 2:
            reason = " ".join(parts[1:]).strip() or None
    elif len(parts) >= 2:
        try:
            target_uid = int(parts[1])
        except ValueError:
            return None, None, None
        if len(parts) >= 3:
            reason = " ".join(parts[2:]).strip() or None
    return target_uid, reason, replied_user


async def _safe_send_audit(chat_id: int, text: str, bot: Bot) -> None:
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception:
        logger.exception("Failed to send audit message to chat %s", chat_id)


def _notify_audit_chats_fire_and_forget(message: Message, text: str) -> None:
    """Schedule sending audit notifications to configured chats without awaiting them."""
    bot = message.bot
    for chat_id in _get_audit_chats():
        asyncio.create_task(_safe_send_audit(chat_id, text, bot))


# Note: persistence is handled via moder_telegram.storage (sqlite). We still
# keep any transient in-memory state minimal.


def _get_admins() -> set[int]:
    """Read ADMINS env var (comma separated user ids)."""
    raw = os.environ.get("ADMINS", "")
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            logger.warning("Invalid admin id in ADMINS: %s", part)
    return ids


async def _on_message(message: Message) -> None:
    # Ignore bots
    if message.from_user is not None and message.from_user.is_bot:
        return

    user_id = message.from_user.id if message.from_user else None
    # If user is explicitly banned (persisted in DB), try to delete message and notify
    if user_id is not None and (storage.is_banned(user_id, DB_PATH) or storage.is_muted(user_id, DB_PATH)):
        try:
            await message.delete()
            await message.answer("Пользователь заблокирован/замьючен модератором.")
        except Exception:
            logger.exception("Failed to delete message from banned user")
        return

    text = message.text or message.caption or ""
    if is_bad_message(text):
        try:
            # Try to delete the offending message and notify the chat
            await message.delete()
            await message.answer("Сообщение удалено: нарушение правил модерации.")
        except Exception:
            logger.exception("Failed to moderate message")


async def _on_command(message: Message) -> None:
    """Handle simple admin commands: /ban, /warn, /stats

    Usage:
    - /ban <user_id>
    - /warn <user_id>
    - /stats
    """
    text = (message.text or "").strip()
    if not text:
        return
    parts = text.split()
    cmd = parts[0].lower()
    user = message.from_user
    admins = _get_admins()
    if user is None or user.id not in admins:
        await message.answer("Только администраторы могут использовать эту команду.")
        return

    if cmd == "/ban":
        # Allow ban by reply: admin replies to a user's message and sends /ban
        target_uid = None
        if message.reply_to_message and message.reply_to_message.from_user:
            target_uid = message.reply_to_message.from_user.id
        elif len(parts) >= 2:
            try:
                target_uid = int(parts[1])
            except ValueError:
                await message.answer("Неверный user_id")
                return

        if target_uid is None:
            await message.answer("Использование: /ban <user_id> или ответьте на сообщение пользователя и выполните /ban")
            return

        storage.ban_user(target_uid, DB_PATH)
        # record audit and notify audit chats
        storage.log_action("ban", target_uid, user.id if user else None, details=None, db_path=DB_PATH)
        for chat_id in _get_audit_chats():
            try:
                await message.bot.send_message(chat_id, f"[Audit] Admin {user.id} banned user {target_uid}")
            except Exception:
                logger.exception("Failed to send audit message to chat %s", chat_id)
        await message.answer(f"Пользователь {target_uid} заблокирован.")
        logger.info("Admin %s banned user %s", user.id, target_uid)

    elif cmd == "/warn":
        # Allow warn by reply as well
        target_uid = None
        if message.reply_to_message and message.reply_to_message.from_user:
            target_uid = message.reply_to_message.from_user.id
        elif len(parts) >= 2:
            try:
                target_uid = int(parts[1])
            except ValueError:
                await message.answer("Неверный user_id")
                return

        if target_uid is None:
            await message.answer("Использование: /warn <user_id> или ответьте на сообщение пользователя и выполните /warn")
            return

        total = storage.warn_user(target_uid, DB_PATH)
        storage.log_action("warn", target_uid, user.id if user else None, details=f"total={total}", db_path=DB_PATH)
        for chat_id in _get_audit_chats():
            try:
                await message.bot.send_message(chat_id, f"[Audit] Admin {user.id} warned user {target_uid} (total={total})")
            except Exception:
                logger.exception("Failed to send audit message to chat %s", chat_id)
        await message.answer(f"Пользователь {target_uid} получил предупреждение (total={total}).")
        logger.info("Admin %s warned user %s (total=%s)", user.id, target_uid, total)

    elif cmd == "/stats":
        total_banned, total_warned = storage.get_stats(DB_PATH)
        await message.answer(f"Banned: {total_banned}, Warned: {total_warned}")

    elif cmd == "/unban":
        # Allow unban by reply or by id
        target_uid = None
        if message.reply_to_message and message.reply_to_message.from_user:
            target_uid = message.reply_to_message.from_user.id
        elif len(parts) >= 2:
            try:
                target_uid = int(parts[1])
            except ValueError:
                await message.answer("Неверный user_id")
                return

        if target_uid is None:
            await message.answer("Использование: /unban <user_id> или ответьте на сообщение пользователя и выполните /unban")
            return

        storage.unban_user(target_uid, DB_PATH)
        storage.log_action("unban", target_uid, user.id if user else None, db_path=DB_PATH)
        for chat_id in _get_audit_chats():
            try:
                await message.bot.send_message(chat_id, f"[Audit] Admin {user.id} unbanned user {target_uid}")
            except Exception:
                logger.exception("Failed to send audit message to chat %s", chat_id)
        await message.answer(f"Пользователь {target_uid} разблокирован.")
        logger.info("Admin %s unbanned user %s", user.id, target_uid)

    elif cmd == "/mute":
        # Allow mute by reply or by id
        target_uid = None
        if message.reply_to_message and message.reply_to_message.from_user:
            target_uid = message.reply_to_message.from_user.id
        elif len(parts) >= 2:
            try:
                target_uid = int(parts[1])
            except ValueError:
                await message.answer("Неверный user_id")
                return

        if target_uid is None:
            await message.answer("Использование: /mute <user_id> или ответьте на сообщение пользователя и выполните /mute")
            return

        storage.mute_user(target_uid, DB_PATH)
        storage.log_action("mute", target_uid, user.id if user else None, db_path=DB_PATH)
        for chat_id in _get_audit_chats():
            try:
                await message.bot.send_message(chat_id, f"[Audit] Admin {user.id} muted user {target_uid}")
            except Exception:
                logger.exception("Failed to send audit message to chat %s", chat_id)
        await message.answer(f"Пользователь {target_uid} заглушён (muted).")
        logger.info("Admin %s muted user %s", user.id, target_uid)

    elif cmd == "/unmute":
        # Allow unmute by reply or by id
        target_uid = None
        if message.reply_to_message and message.reply_to_message.from_user:
            target_uid = message.reply_to_message.from_user.id
        elif len(parts) >= 2:
            try:
                target_uid = int(parts[1])
            except ValueError:
                await message.answer("Неверный user_id")
                return

        if target_uid is None:
            await message.answer("Использование: /unmute <user_id> или ответьте на сообщение пользователя и выполните /unmute")
            return

        storage.unmute_user(target_uid, DB_PATH)
        storage.log_action("unmute", target_uid, user.id if user else None, db_path=DB_PATH)
        for chat_id in _get_audit_chats():
            try:
                await message.bot.send_message(chat_id, f"[Audit] Admin {user.id} unmuted user {target_uid}")
            except Exception:
                logger.exception("Failed to send audit message to chat %s", chat_id)
        await message.answer(f"Пользователь {target_uid} размьючен (unmuted).")
        logger.info("Admin %s unmuted user %s", user.id, target_uid)


async def _run_async(token: str) -> None:
    bot = Bot(token=token)
    dp = Dispatcher()

    # Register handlers
    dp.message.register(_on_command, lambda message: (message.text or "").startswith('/'))
    dp.message.register(_on_message)

    logger.info("Starting polling")
    # start_polling takes bot as an argument in aiogram v3
    await dp.start_polling(bot)


def _configure_logging(log_path: str = "moder_telegram.log") -> None:
    root = logging.getLogger()
    if root.handlers:
        # avoid adding duplicate handlers in tests or repeated runs
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Console handler
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)
    # Rotating file handler
    fh = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)


def run(token: Optional[str] = None) -> None:
    """Run the bot (blocking).

    token: BOT token. If not provided, read from BOT_TOKEN env var.
    """
    # Configure logging (file + rotation)
    _configure_logging()

    # Initialize DB
    try:
        storage.init_db(DB_PATH)
    except Exception:
        logger.exception("Failed to initialize storage")

    if token is None:
        token = os.environ.get("BOT_TOKEN", "")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable is required")
    asyncio.run(_run_async(token))
