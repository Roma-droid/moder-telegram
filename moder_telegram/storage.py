"""Simple SQLite persistence for bans and warnings.

This module provides small helper functions that open a sqlite3 database on
each call. It's intentionally simple and synchronous; for high load consider
using an async DB driver or running DB calls in an executor.

DB_PATH can be overridden via the DB_PATH environment variable.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Tuple

DEFAULT_DB = "moder_telegram.db"


def _get_db_path() -> str:
    return os.environ.get("DB_PATH", DEFAULT_DB)


def init_db(db_path: str | None = None) -> None:
    """Create tables if they don't exist."""
    path = db_path or _get_db_path()
    with sqlite3.connect(path) as conn:
        cur = conn.cursor()
        # chat-scoped tables: include chat_id so moderation is per-group
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bans (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                banned_at TEXT NOT NULL,
                PRIMARY KEY(chat_id, user_id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS warns (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                count INTEGER NOT NULL,
                PRIMARY KEY(chat_id, user_id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mutes (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                muted_at TEXT NOT NULL,
                PRIMARY KEY(chat_id, user_id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                action TEXT NOT NULL,
                user_id INTEGER,
                admin_id INTEGER,
                timestamp TEXT NOT NULL,
                details TEXT
            )
            """
        )
        conn.commit()


def ban_user(user_id: int, chat_id: int | None = None, db_path: str | None = None) -> None:
    path = db_path or _get_db_path()
    cid = 0 if chat_id is None else int(chat_id)
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(path) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO bans(chat_id, user_id, banned_at) VALUES(?, ?, ?)",
            (cid, user_id, now),
        )
        conn.commit()


def unban_user(user_id: int, chat_id: int | None = None, db_path: str | None = None) -> None:
    path = db_path or _get_db_path()
    cid = 0 if chat_id is None else int(chat_id)
    with sqlite3.connect(path) as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM bans WHERE chat_id = ? AND user_id = ?", (cid, user_id))
        conn.commit()


def mute_user(user_id: int, chat_id: int | None = None, db_path: str | None = None) -> None:
    path = db_path or _get_db_path()
    cid = 0 if chat_id is None else int(chat_id)
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(path) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO mutes(chat_id, user_id, muted_at) VALUES(?, ?, ?)",
            (cid, user_id, now),
        )
        conn.commit()


def unmute_user(user_id: int, chat_id: int | None = None, db_path: str | None = None) -> None:
    path = db_path or _get_db_path()
    cid = 0 if chat_id is None else int(chat_id)
    with sqlite3.connect(path) as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM mutes WHERE chat_id = ? AND user_id = ?", (cid, user_id))
        conn.commit()


def is_muted(user_id: int, chat_id: int | None = None, db_path: str | None = None) -> bool:
    path = db_path or _get_db_path()
    cid = 0 if chat_id is None else int(chat_id)
    with sqlite3.connect(path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM mutes WHERE chat_id = ? AND user_id = ? LIMIT 1", (cid, user_id))
        return cur.fetchone() is not None


def log_action(action: str, user_id: int | None, admin_id: int | None, details: str | None = None, chat_id: int | None = None, db_path: str | None = None) -> None:
    """Record an audit action in the audit table."""
    path = db_path or _get_db_path()
    now = datetime.now(timezone.utc).isoformat()
    cid = None if chat_id is None else int(chat_id)
    with sqlite3.connect(path) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO audit(chat_id, action, user_id, admin_id, timestamp, details) VALUES(?, ?, ?, ?, ?, ?)",
            (cid, action, user_id, admin_id, now, details),
        )
        conn.commit()


def get_audit(user_id: int, limit: int = 50, chat_id: int | None = None, db_path: str | None = None):
    """Return recent audit rows for a user ordered by timestamp desc.

    Each row is (id, action, user_id, admin_id, timestamp, details).
    """
    path = db_path or _get_db_path()
    cid = None if chat_id is None else int(chat_id)
    with sqlite3.connect(path) as conn:
        cur = conn.cursor()
        if cid is None:
            cur.execute(
                "SELECT id, action, user_id, admin_id, timestamp, details FROM audit WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?",
                (user_id, limit),
            )
        else:
            cur.execute(
                "SELECT id, action, user_id, admin_id, timestamp, details FROM audit WHERE user_id = ? AND chat_id = ? ORDER BY timestamp DESC LIMIT ?",
                (user_id, cid, limit),
            )
        return cur.fetchall()


def is_banned(user_id: int, chat_id: int | None = None, db_path: str | None = None) -> bool:
    path = db_path or _get_db_path()
    cid = 0 if chat_id is None else int(chat_id)
    with sqlite3.connect(path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM bans WHERE chat_id = ? AND user_id = ? LIMIT 1", (cid, user_id))
        return cur.fetchone() is not None


def warn_user(user_id: int, chat_id: int | None = None, db_path: str | None = None) -> int:
    """Increment warn count for a user and return the new total."""
    path = db_path or _get_db_path()
    cid = 0 if chat_id is None else int(chat_id)
    with sqlite3.connect(path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT count FROM warns WHERE chat_id = ? AND user_id = ?", (cid, user_id))
        row = cur.fetchone()
        if row is None:
            cur.execute("INSERT INTO warns(chat_id, user_id, count) VALUES(?, ?, ?)", (cid, user_id, 1))
            total = 1
        else:
            total = row[0] + 1
            cur.execute("UPDATE warns SET count = ? WHERE chat_id = ? AND user_id = ?", (total, cid, user_id))
        conn.commit()
        return total


def get_warn_count(user_id: int, chat_id: int | None = None, db_path: str | None = None) -> int:
    path = db_path or _get_db_path()
    cid = 0 if chat_id is None else int(chat_id)
    with sqlite3.connect(path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT count FROM warns WHERE chat_id = ? AND user_id = ?", (cid, user_id))
        row = cur.fetchone()
        return row[0] if row is not None else 0


def get_stats(db_path: str | None = None, chat_id: int | None = None) -> Tuple[int, int]:
    """Return (total_banned, total_warned_users).

    total_warned_users is number of distinct users with count > 0.
    """
    path = db_path or _get_db_path()
    cid = None if chat_id is None else int(chat_id)
    with sqlite3.connect(path) as conn:
        cur = conn.cursor()
        if cid is None:
            cur.execute("SELECT COUNT(*) FROM bans")
            total_banned = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM warns WHERE count > 0")
            total_warned = cur.fetchone()[0]
        else:
            cur.execute("SELECT COUNT(*) FROM bans WHERE chat_id = ?", (cid,))
            total_banned = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM warns WHERE chat_id = ? AND count > 0", (cid,))
            total_warned = cur.fetchone()[0]
        return int(total_banned), int(total_warned)
