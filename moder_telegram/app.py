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
MODERATION_CHAT_IDS_RAW = os.environ.get("MODERATION_CHAT_IDS", "")
AUTO_DELETE_SECONDS = int(os.environ.get("AUTO_DELETE_SECONDS", "0"))


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


def _get_moderation_chats() -> list[int]:
    ids: list[int] = []
    raw = MODERATION_CHAT_IDS_RAW
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            logger.warning("Invalid moderation chat id in MODERATION_CHAT_IDS: %s", part)
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
    """Synchronous extraction for replies and numeric ids.

    Kept for backwards compatibility. For username resolution use
    the async helper `_resolve_target_and_reason` below.
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


async def _resolve_target_and_reason(parts: list[str], message: Message) -> tuple[Optional[int], Optional[str], Optional[object]]:
    """Asynchronously resolve a command target.

    Supports three forms:
    - reply: take user from replied message
    - numeric id: /cmd 12345 [reason]
    - username: /cmd @username [reason] or /cmd username [reason]

    Returns (user_id or None, reason or None, replied_user object or None).
    """
    # If it's a reply - immediate
    if message.reply_to_message and message.reply_to_message.from_user:
        replied_user = message.reply_to_message.from_user
        target_uid = replied_user.id
        reason = " ".join(parts[1:]).strip() if len(parts) >= 2 else None
        return target_uid, reason, replied_user

    # Not a reply - must have at least an argument
    if len(parts) < 2:
        return None, None, None

    arg = parts[1]
    # try numeric id first
    try:
        target_uid = int(arg)
        reason = " ".join(parts[2:]).strip() if len(parts) >= 3 else None
        return target_uid, reason, None
    except ValueError:
        # treat as username
        username = arg.lstrip("@")
        try:
            # Bot.get_chat accepts @username and returns Chat with id
            chat = await message.bot.get_chat(f"@{username}")
            target_uid = int(getattr(chat, "id", None))
            reason = " ".join(parts[2:]).strip() if len(parts) >= 3 else None
            return target_uid, reason, None
        except Exception:
            logger.debug("Failed to resolve username '%s' to id", username)
            return None, None, None


async def _safe_send_audit(chat_id: int, text: str, bot: Bot) -> None:
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception:
        logger.exception("Failed to send audit message to chat %s", chat_id)


async def _reply_with_optional_delete(orig_message: Message, text: str, parse_mode: Optional[str] = None) -> None:
    """Send a reply/answer and optionally delete the sent bot message after AUTO_DELETE_SECONDS.

    This helper preserves existing behaviour if AUTO_DELETE_SECONDS is 0 or unset.
    """
    try:
        sent = await orig_message.answer(text, parse_mode=parse_mode)
    except Exception:
        logger.exception("Failed to send reply message")
        return

    # Schedule auto-delete if configured
    if AUTO_DELETE_SECONDS and AUTO_DELETE_SECONDS > 0:
        async def _del_after(m):
            await asyncio.sleep(AUTO_DELETE_SECONDS)
            try:
                await m.delete()
            except Exception:
                logger.debug("Auto-delete failed for bot message", exc_info=True)

        asyncio.create_task(_del_after(sent))


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
        if storage.is_banned(user_id, DB_PATH) or storage.is_muted(user_id, DB_PATH):
            try:
                await message.delete()
                await _reply_with_optional_delete(message, "Пользователь заблокирован/замьючен модератором.")
            except Exception:
                logger.exception("Failed to delete message from moderated user")
            return

    text = message.text or message.caption or ""
    if is_bad_message(text):
        try:
            await message.delete()
            await _reply_with_optional_delete(message, "Сообщение удалено: нарушение правил модерации.")
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

    # Public help command (available to all users)
    if cmd == "/help":
        help_text = (
            "Доступные команды:\n"
            "<b>/help</b> — показать это сообщение\n"
            "<b>/ban</b> &lt;user_id|@username&gt; [reason] — заблокировать пользователя (админы)\n"
            "<b>/warn</b> &lt;user_id|@username&gt; [reason] — выдать предупреждение (админы)\n"
            "<b>/mute</b> &lt;user_id|@username&gt; [reason] — заглушить пользователя (админы)\n"
            "<b>/unmute</b> &lt;user_id|@username&gt; [reason] — снять заглушение (админы)\n"
            "<b>/unban</b> &lt;user_id|@username&gt; [reason] — разблокировать пользователя (админы)\n"
            "<b>/stats</b> — показать статистику (админы)\n"
            "<b>/audit</b> &lt;user_id&gt; — показать записи аудита для пользователя (админы)\n"
            "<b>/mywarns</b> — показать количество ваших предупреждений\n\n"
            "<b>/rules</b> [section] — показать правила сообщества\n\n"
            "Используйте команду в ответе на сообщение, чтобы применить действие к конкретному сообщению пользователя."
        )
        await _reply_with_optional_delete(message, help_text, parse_mode="HTML")
        return

    # Public command to show warns for the calling user; admins may pass a user_id
    if cmd == "/mywarns":
        calling_user = message.from_user
        if calling_user is None:
            await _reply_with_optional_delete(message, "Не удалось определить пользователя.")
            return

        # If an argument is provided, only admins can query other users
        if len(parts) >= 2:
            admins = _get_admins()
            if calling_user.id not in admins:
                await _reply_with_optional_delete(message, "Только администраторы могут смотреть предупреждения других пользователей.")
                return
            try:
                target_uid = int(parts[1])
            except ValueError:
                await _reply_with_optional_delete(message, "Неверный user_id")
                return

            count = storage.get_warn_count(target_uid, DB_PATH)
            if count:
                await _reply_with_optional_delete(message, f"У пользователя {target_uid} предупреждений: {count}.")
            else:
                await _reply_with_optional_delete(message, f"У пользователя {target_uid} нет предупреждений.")
            return

        # No argument — show calling user's warns
        count = storage.get_warn_count(calling_user.id, DB_PATH)
        if count:
            await _reply_with_optional_delete(message, f"У вас предупреждений: {count}.")
        else:
            await _reply_with_optional_delete(message, "У вас нет предупреждений.")
        return

    # Public command to show community rules
    if cmd == "/rules":
        # Rules sections for quick access
        sections: dict[str, str] = {
            "general": (
                "<b>Правила сообщества — Общие</b>\n"
                "1. Уважайте других участников. Никаких оскорблений и травли.\n"
                "2. Соблюдайте тему чата и инструкции модераторов.\n"
            ),
            "safety": (
                "<b>Правила сообщества — Безопасность</b>\n"
                "1. Запрещён спам, фишинг и мошенничество.\n"
                "2. Не публикуйте личную информацию третьих лиц.\n"
            ),
            "sanctions": (
                "<b>Правила сообщества — Санкции</b>\n"
                "Нарушения могут привести к предупреждениям, мутам и банам.\n"
                "Модераторы принимают решения согласно ситуации.\n"
            ),
        }

        # If a section requested, show it or a helpful list
        if len(parts) >= 2:
            section = parts[1].lower()
            if section in sections:
                await _reply_with_optional_delete(message, sections[section], parse_mode="HTML")
                return
            else:
                available = ", ".join(sorted(sections.keys()))
                await _reply_with_optional_delete(
                    message,
                    f"Раздел '{html.escape(section)}' не найден. Доступные разделы: {available}. Используйте /rules &lt;section&gt;.",
                )
                return

        # No section — show full rules (concise)
        rules_text = (
            "<b>Правила сообщества</b>\n"
            "1. Уважайте других участников. Никаких оскорблений и травли.\n"
            "2. Запрещён спам, фишинг и мошенничество.\n"
            "3. Не публикуйте личную информацию третьих лиц.\n"
            "4. Соблюдайте тему чата и инструкции модераторов.\n"
            "5. Для серьёзных нарушений используйте обращения к модераторам.\n\n"
            "Нарушения могут привести к предупреждениям, мутам и банам.\n"
            f"Доступные разделы: {', '.join(sorted(sections.keys()))}. Используйте /rules &lt;section&gt;."
        )
        await _reply_with_optional_delete(message, rules_text, parse_mode="HTML")
        return

    # Public command to file a complaint/plaint.
    # Usage:
    # - reply to a user's message: /plaint [reason]
    # - /plaint <user_id|@username> [reason]
    # - /plaint <text> (general complaint without a specific user)
    if cmd == "/plaint":
        reporter = message.from_user
        if reporter is None:
            await _reply_with_optional_delete(message, "Не удалось определить пользователя.")
            return

        target_uid: Optional[int] = None
        replied_user = None
        reason: Optional[str] = None

        # reply -> target is the replied user
        if message.reply_to_message and message.reply_to_message.from_user:
            replied_user = message.reply_to_message.from_user
            target_uid = replied_user.id
            reason = " ".join(parts[1:]).strip() if len(parts) >= 2 else None
        elif len(parts) >= 2:
            # if first arg looks like id or username, try resolving; otherwise treat as free-text reason
            first = parts[1]
            if first.startswith("@") or first.isdigit():
                resolved_uid, resolved_reason, resolved_reply = await _resolve_target_and_reason(parts, message)
                if resolved_uid is None and resolved_reason is None:
                    await _reply_with_optional_delete(message, "Не удалось распознать пользователя. Использование: /plaint <user_id|@username> [text] или /plaint <text>")
                    return
                target_uid = resolved_uid
                reason = resolved_reason
                replied_user = resolved_reply
            else:
                reason = " ".join(parts[1:]).strip()
        else:
            await _reply_with_optional_delete(message, "Использование: /plaint <text> или ответьте на сообщение и выполните /plaint [reason]")
            return

        # Record plaint in audit table
        storage.log_action("plaint", target_uid, reporter.id if reporter else None, details=reason, db_path=DB_PATH)

        # Build audit notification
        user_display = _user_display_from_message_user(replied_user) if replied_user else (html.escape(f"user {target_uid}") if target_uid else "-")
        reporter_display = _user_display_from_message_user(reporter)
        ts = datetime.now(timezone.utc).isoformat()
        text_fmt = (
            f"<b>Action:</b> plaint\n"
            f"<b>User:</b> {user_display} (id: {target_uid})\n"
            f"<b>Reporter:</b> {reporter_display} (id: {reporter.id})\n"
            f"<b>Time (UTC):</b> {html.escape(ts)}\n"
        )
        if reason:
            text_fmt += f"<b>Reason:</b> {html.escape(reason)}\n"

        _notify_audit_chats_fire_and_forget(message, text_fmt)
        await _reply_with_optional_delete(message, "Ваша жалоба зарегистрирована. Спасибо.")
        return

    user = message.from_user
    admins = _get_admins()
    if user is None or user.id not in admins:
        await _reply_with_optional_delete(message, "Только администраторы могут использовать эту команду.")
        return

    # Restrict certain moderation commands to configured moderation group(s)
    moderation_cmds = {"/ban", "/warn", "/mute"}
    if cmd in moderation_cmds:
        allowed = _get_moderation_chats()
        # If no moderation chat configured, allow by default (backwards-compatible)
        if allowed:
            chat = getattr(message, "chat", None)
            chat_id = getattr(chat, "id", None)
            if chat_id not in allowed:
                await _reply_with_optional_delete(
                    message,
                    "Команды /ban, /warn и /mute доступны только в модерационной группе.",
                )
                return

    # /ban
    if cmd == "/ban":
        target_uid, reason, replied_user = await _resolve_target_and_reason(parts, message)
        # Support optional explicit username argument: /ban <user_id> @username [reason]
        username_arg: Optional[str] = None
        if not replied_user and len(parts) >= 3:
            # If the command used numeric id as first arg and second arg looks like username
            # treat parts[2] as username and shift reason accordingly.
            try:
                # if parts[1] is numeric and parts[2] starts with @ -> username provided
                int(parts[1])
                if parts[2].startswith("@"):
                    username_arg = parts[2].lstrip("@")
                    reason = " ".join(parts[3:]).strip() or None
            except ValueError:
                # parts[1] was not numeric, leave as-is
                pass
        if target_uid is None:
            await _reply_with_optional_delete(message, "Использование: /ban <user_id> [reason] или ответьте на сообщение и выполните /ban [reason]")
            return

        storage.ban_user(target_uid, DB_PATH)
        storage.log_action("ban", target_uid, user.id if user else None, details=reason, db_path=DB_PATH)

        if replied_user:
            user_display = _user_display_from_message_user(replied_user)
        elif username_arg:
            user_display = html.escape(f"@{username_arg}")
        else:
            user_display = html.escape(f"user {target_uid}")
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
        if username_arg:
            # record provided username in audit details as well
            text_fmt += f"<b>Provided username:</b> {html.escape('@' + username_arg)}\n"
        _notify_audit_chats_fire_and_forget(message, text_fmt)
        await _reply_with_optional_delete(message, f"Пользователь {username_arg or target_uid} заблокирован.")
        logger.info("Admin %s banned user %s", user.id, target_uid)
        return

    # /warn
    if cmd == "/warn":
        target_uid, reason, replied_user = await _resolve_target_and_reason(parts, message)
        if target_uid is None:
            await _reply_with_optional_delete(message, "Использование: /warn <user_id> [reason] или ответьте на сообщение и выполните /warn [reason]")
            return

        total = storage.warn_user(target_uid, DB_PATH)
        details = (reason + f" (total={total})") if reason else f"total={total}"
        storage.log_action("warn", target_uid, user.id if user else None, details=details, db_path=DB_PATH)

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
        await _reply_with_optional_delete(message, f"Пользователь {target_uid} получил предупреждение (total={total}).")
        logger.info("Admin %s warned user %s (total=%s)", user.id, target_uid, total)
        return

    # /stats
    if cmd == "/stats":
        total_banned, total_warned = storage.get_stats(DB_PATH)
        await _reply_with_optional_delete(message, f"Banned: {total_banned}, Warned: {total_warned}")
        return

    # /unban
    if cmd == "/unban":
        target_uid, reason, replied_user = await _resolve_target_and_reason(parts, message)
        if target_uid is None:
            await _reply_with_optional_delete(message, "Использование: /unban <user_id> [reason] или ответьте на сообщение и выполните /unban [reason]")
            return

        storage.unban_user(target_uid, DB_PATH)
        storage.log_action("unban", target_uid, user.id if user else None, details=reason, db_path=DB_PATH)

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
        await _reply_with_optional_delete(message, f"Пользователь {target_uid} разблокирован.")
        logger.info("Admin %s unbanned user %s", user.id, target_uid)
        return

    # /mute
    if cmd == "/mute":
        target_uid, reason, replied_user = await _resolve_target_and_reason(parts, message)
        # Support optional explicit username argument: /mute <user_id> @username [reason]
        username_arg: Optional[str] = None
        if not replied_user and len(parts) >= 3:
            try:
                int(parts[1])
                if parts[2].startswith("@"):
                    username_arg = parts[2].lstrip("@")
                    reason = " ".join(parts[3:]).strip() or None
            except ValueError:
                pass
        if target_uid is None:
            await _reply_with_optional_delete(message, "Использование: /mute <user_id> [reason] или ответьте на сообщение и выполните /mute [reason]")
            return

        storage.mute_user(target_uid, DB_PATH)
        storage.log_action("mute", target_uid, user.id if user else None, details=reason, db_path=DB_PATH)

        if replied_user:
            user_display = _user_display_from_message_user(replied_user)
        elif username_arg:
            user_display = html.escape(f"@{username_arg}")
        else:
            user_display = html.escape(f"user {target_uid}")
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
        if username_arg:
            text_fmt += f"<b>Provided username:</b> {html.escape('@' + username_arg)}\n"
        _notify_audit_chats_fire_and_forget(message, text_fmt)
        await _reply_with_optional_delete(message, f"Пользователь {username_arg or target_uid} заглушён (muted).")
        logger.info("Admin %s muted user %s", user.id, target_uid)
        return

    # /unmute
    if cmd == "/unmute":
        target_uid, reason, replied_user = await _resolve_target_and_reason(parts, message)
        if target_uid is None:
            await _reply_with_optional_delete(message, "Использование: /unmute <user_id> [reason] или ответьте на сообщение и выполните /unmute [reason]")
            return

        storage.unmute_user(target_uid, DB_PATH)
        storage.log_action("unmute", target_uid, user.id if user else None, details=reason, db_path=DB_PATH)

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
        await _reply_with_optional_delete(message, f"Пользователь {target_uid} размьючен (unmuted).")
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
                await _reply_with_optional_delete(message, "Неверный user_id")
                return

        if target_uid is None:
            await _reply_with_optional_delete(message, "Использование: /audit <user_id> или ответьте на сообщение пользователя и выполните /audit")
            return

        rows = storage.get_audit(target_uid, db_path=DB_PATH)
        if not rows:
            await _reply_with_optional_delete(message, "Нет записей аудита для этого пользователя.")
            return

        lines: list[str] = []
        for r in rows[:20]:
            _id, action, uid, admin_id, ts, details = r
            admin_txt = f"{admin_id}" if admin_id is not None else "-"
            details_txt = html.escape(details) if details else ""
            lines.append(f"{html.escape(ts)} — <b>{html.escape(action)}</b> by {html.escape(str(admin_txt))} {details_txt}")

        await _reply_with_optional_delete(message, "\n".join(lines), parse_mode="HTML")


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
