"""Entry point and wiring for the Telegram moderation bot.

Contains handlers for messages and admin commands. Moderation state is
persisted in `moder_telegram.storage` (SQLite).

Features:
- moderation via `moderation.is_bad_message`
- admin commands: /ban, /warn, /mute, /unmute, /unban, /stats, /audit
- optional `reason` argument is supported and saved in audit.details
- audit notifications sent to chats from `AUDIT_CHAT_IDS` (fire-and-forget)
- file logging with rotation
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Optional

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
    """Return a sanitized display name for a user object (prefer username)."""
    if not u:
        return "user"
    username = getattr(u, "username", None)
    if username:
        return html.escape(f"@{username}")
    first = getattr(u, "first_name", "") or ""
    last = getattr(u, "last_name", "") or ""
    full = (first + " " + last).strip()
    if full:
        return html.escape(full)
    return html.escape(str(getattr(u, "id", "user")))


def _extract_target_and_reason(parts: list[str], message: Message) -> tuple[Optional[int], Optional[str], Optional[object]]:
    """Return (target_uid, reason, replied_user).

    - If command is sent as a reply: target comes from replied message author and
      reason is the remainder of parts after the command (if any).
    - Otherwise, expects: /cmd <user_id> [reason...]
    - Returns (None, None, None) when parsing fails (invalid id).
    """
    target_uid: Optional[int] = None
    reason: Optional[str] = None
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


def _get_admins() -> set[int]:
    raw = os.environ.get("ADMINS", "")
    ids: set[int] = set()
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
    # ignore bots
    if message.from_user is not None and message.from_user.is_bot:
        return

    user_id = message.from_user.id if message.from_user else None
    if user_id is not None:
        # If banned or muted - delete message and optionally notify
        chat_id = message.chat.id if message.chat else 0
        if storage.is_banned(user_id, chat_id, DB_PATH) or storage.is_muted(user_id, chat_id, DB_PATH):
            try:
                await message.delete()
                await message.answer("Пользователь заблокирован/замьючен модератором.")
            except Exception:
                logger.exception("Failed to delete message from moderated user")
            return

    text = message.text or message.caption or ""
    if is_bad_message(text):
        try:
            await message.delete()
            await message.answer("Сообщение удалено: нарушение правил модерации.")
        except Exception:
            logger.exception("Failed to moderate message")


async def _on_command(message: Message) -> None:
    """Handle admin commands with optional reason and audit notifications.

    Commands supported: /ban, /warn, /mute, /unmute, /unban, /stats, /audit
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

    # /ban
    if cmd == "/ban":
        target_uid, reason, replied_user = _extract_target_and_reason(parts, message)
        if target_uid is None:
            await message.answer("Использование: /ban <user_id> [reason] или ответьте на сообщение и выполните /ban [reason]")
            return

    chat_id = message.chat.id if message.chat else 0
    storage.ban_user(target_uid, chat_id=chat_id, db_path=DB_PATH)
    storage.log_action("ban", target_uid, user.id if user else None, details=reason, chat_id=chat_id, db_path=DB_PATH)

        user_display = _user_display_from_message_user(replied_user) if replied_user else html.escape(f"user {target_uid}")
        admin_display = _user_display_from_message_user(user)
        ts = datetime.now(timezone.utc).isoformat()
        text_fmt = (
            f"<b>Action:</b> ban\n"
            f"<b>User:</b> <a href=\"tg://user?id={target_uid}\">{user_display}</a> (id: {target_uid})\n"
            f"<b>Admin:</b> {admin_display} (id: {user.id})\n"
            f"<b>Time (UTC):</b> {html.escape(ts)}\n"
        )
        if reason:
            text_fmt += f"<b>Reason:</b> {html.escape(reason)}\n"
        _notify_audit_chats_fire_and_forget(message, text_fmt)
        await message.answer(f"Пользователь {target_uid} заблокирован.")
        logger.info("Admin %s banned user %s", user.id, target_uid)
        return

    # /warn
    if cmd == "/warn":
        target_uid, reason, replied_user = _extract_target_and_reason(parts, message)
        if target_uid is None:
            await message.answer("Использование: /warn <user_id> [reason] или ответьте на сообщение и выполните /warn [reason]")
            return

    chat_id = message.chat.id if message.chat else 0
    total = storage.warn_user(target_uid, chat_id=chat_id, db_path=DB_PATH)
    details = (reason + f" (total={total})") if reason else f"total={total}"
    storage.log_action("warn", target_uid, user.id if user else None, details=details, chat_id=chat_id, db_path=DB_PATH)

        user_display = _user_display_from_message_user(replied_user) if replied_user else html.escape(f"user {target_uid}")
        admin_display = _user_display_from_message_user(user)
        ts = datetime.now(timezone.utc).isoformat()
        text_fmt = (
            f"<b>Action:</b> warn\n"
            f"<b>User:</b> <a href=\"tg://user?id={target_uid}\">{user_display}</a> (id: {target_uid})\n"
            f"<b>Admin:</b> {admin_display} (id: {user.id})\n"
            f"<b>Time (UTC):</b> {html.escape(ts)}\n"
            f"<b>Total warns:</b> {total}\n"
        )
        if reason:
            text_fmt += f"<b>Reason:</b> {html.escape(reason)}\n"
        _notify_audit_chats_fire_and_forget(message, text_fmt)
        await message.answer(f"Пользователь {target_uid} получил предупреждение (total={total}).")
        logger.info("Admin %s warned user %s (total=%s)", user.id, target_uid, total)
        return

    # /stats
    if cmd == "/stats":
        chat_id = message.chat.id if message.chat else None
        total_banned, total_warned = storage.get_stats(db_path=DB_PATH, chat_id=chat_id)
        await message.answer(f"Banned: {total_banned}, Warned: {total_warned}")
        return

    # /unban
    if cmd == "/unban":
        target_uid, reason, replied_user = _extract_target_and_reason(parts, message)
        if target_uid is None:
            await message.answer("Использование: /unban <user_id> [reason] или ответьте на сообщение и выполните /unban [reason]")
            return

    chat_id = message.chat.id if message.chat else 0
    storage.unban_user(target_uid, chat_id=chat_id, db_path=DB_PATH)
    storage.log_action("unban", target_uid, user.id if user else None, details=reason, chat_id=chat_id, db_path=DB_PATH)

        user_display = _user_display_from_message_user(replied_user) if replied_user else html.escape(f"user {target_uid}")
        admin_display = _user_display_from_message_user(user)
        ts = datetime.now(timezone.utc).isoformat()
        text_fmt = (
            f"<b>Action:</b> unban\n"
            f"<b>User:</b> <a href=\"tg://user?id={target_uid}\">{user_display}</a> (id: {target_uid})\n"
            f"<b>Admin:</b> {admin_display} (id: {user.id})\n"
            f"<b>Time (UTC):</b> {html.escape(ts)}\n"
        )
        if reason:
            text_fmt += f"<b>Reason:</b> {html.escape(reason)}\n"
        _notify_audit_chats_fire_and_forget(message, text_fmt)
        await message.answer(f"Пользователь {target_uid} разблокирован.")
        logger.info("Admin %s unbanned user %s", user.id, target_uid)
        return

    # /mute
    if cmd == "/mute":
        target_uid, reason, replied_user = _extract_target_and_reason(parts, message)
        if target_uid is None:
            await message.answer("Использование: /mute <user_id> [reason] или ответьте на сообщение и выполните /mute [reason]")
            return

    chat_id = message.chat.id if message.chat else 0
    storage.mute_user(target_uid, chat_id=chat_id, db_path=DB_PATH)
    storage.log_action("mute", target_uid, user.id if user else None, details=reason, chat_id=chat_id, db_path=DB_PATH)

        user_display = _user_display_from_message_user(replied_user) if replied_user else html.escape(f"user {target_uid}")
        admin_display = _user_display_from_message_user(user)
        ts = datetime.now(timezone.utc).isoformat()
        text_fmt = (
            f"<b>Action:</b> mute\n"
            f"<b>User:</b> <a href=\"tg://user?id={target_uid}\">{user_display}</a> (id: {target_uid})\n"
            f"<b>Admin:</b> {admin_display} (id: {user.id})\n"
            f"<b>Time (UTC):</b> {html.escape(ts)}\n"
        )
        if reason:
            text_fmt += f"<b>Reason:</b> {html.escape(reason)}\n"
        _notify_audit_chats_fire_and_forget(message, text_fmt)
        await message.answer(f"Пользователь {target_uid} заглушён (muted).")
        logger.info("Admin %s muted user %s", user.id, target_uid)
        return

    # /unmute
    if cmd == "/unmute":
        target_uid, reason, replied_user = _extract_target_and_reason(parts, message)
        if target_uid is None:
            await message.answer("Использование: /unmute <user_id> [reason] или ответьте на сообщение и выполните /unmute [reason]")
            return

    chat_id = message.chat.id if message.chat else 0
    storage.unmute_user(target_uid, chat_id=chat_id, db_path=DB_PATH)
    storage.log_action("unmute", target_uid, user.id if user else None, details=reason, chat_id=chat_id, db_path=DB_PATH)

        user_display = _user_display_from_message_user(replied_user) if replied_user else html.escape(f"user {target_uid}")
        admin_display = _user_display_from_message_user(user)
        ts = datetime.now(timezone.utc).isoformat()
        text_fmt = (
            f"<b>Action:</b> unmute\n"
            f"<b>User:</b> <a href=\"tg://user?id={target_uid}\">{user_display}</a> (id: {target_uid})\n"
            f"<b>Admin:</b> {admin_display} (id: {user.id})\n"
            f"<b>Time (UTC):</b> {html.escape(ts)}\n"
        )
        if reason:
            text_fmt += f"<b>Reason:</b> {html.escape(reason)}\n"
        _notify_audit_chats_fire_and_forget(message, text_fmt)
        await message.answer(f"Пользователь {target_uid} размьючен (unmuted).")
        logger.info("Admin %s unmuted user %s", user.id, target_uid)
        return

    # /audit
    if cmd == "/audit":
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
            await message.answer("Использование: /audit <user_id> или ответьте на сообщение пользователя и выполните /audit")
            return

    chat_id = message.chat.id if message.chat else None
    rows = storage.get_audit(target_uid, chat_id=chat_id, db_path=DB_PATH)
        if not rows:
            await message.answer("Нет записей аудита для этого пользователя.")
            return

        lines: list[str] = []
        for r in rows[:20]:
            _id, action, uid, admin_id, ts, details = r
            admin_txt = f"{admin_id}" if admin_id is not None else "-"
            details_txt = html.escape(details) if details else ""
            lines.append(f"{html.escape(ts)} — <b>{html.escape(action)}</b> by {html.escape(str(admin_txt))} {details_txt}")

        await message.answer("\n".join(lines), parse_mode="HTML")


async def _run_async(token: str) -> None:
    bot = Bot(token=token)
    dp = Dispatcher()

    dp.message.register(_on_command, lambda message: (message.text or "").startswith('/'))
    dp.message.register(_on_message)

    logger.info("Starting polling")
    await dp.start_polling(bot)


def _configure_logging(log_path: str = "moder_telegram.log") -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)

    fh = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)


def run(token: Optional[str] = None) -> None:
    """Run the bot. Reads BOT_TOKEN from env if not provided."""
    _configure_logging()

    try:
        storage.init_db(DB_PATH)
    except Exception:
        logger.exception("Failed to initialize storage")

    if token is None:
        token = os.environ.get("BOT_TOKEN", "")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable is required")

    asyncio.run(_run_async(token))
