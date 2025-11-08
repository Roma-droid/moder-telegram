import sqlite3
from moder_telegram import storage


def test_ban_and_unban(tmp_path):
    db = tmp_path / "test.db"
    storage.init_db(str(db))
    assert not storage.is_banned(1, str(db))
    storage.ban_user(1, str(db))
    assert storage.is_banned(1, str(db))
    storage.unban_user(1, str(db))
    assert not storage.is_banned(1, str(db))


def test_warn_and_stats(tmp_path):
    db = tmp_path / "test2.db"
    storage.init_db(str(db))
    assert storage.get_warn_count(2, str(db)) == 0
    t = storage.warn_user(2, str(db))
    assert t == 1
    t2 = storage.warn_user(2, str(db))
    assert t2 == 2
    assert storage.get_warn_count(2, str(db)) == 2
    # ban another user and check stats
    storage.ban_user(3, str(db))
    b, w = storage.get_stats(str(db))
    assert b == 1
    assert w == 1


def test_db_file_created(tmp_path):
    db = tmp_path / "test_create.db"
    assert not db.exists()
    storage.init_db(str(db))
    assert db.exists()
    # Ensure tables exist
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('bans','warns','mutes')")
    rows = cur.fetchall()
    assert len(rows) == 3
    conn.close()


def test_mute_and_unmute(tmp_path):
    db = tmp_path / "test_mute.db"
    storage.init_db(str(db))
    assert not storage.is_muted(5, str(db))
    storage.mute_user(5, str(db))
    assert storage.is_muted(5, str(db))
    storage.unmute_user(5, str(db))
    assert not storage.is_muted(5, str(db))


def test_audit_log_created(tmp_path):
    db = tmp_path / "test_audit.db"
    storage.init_db(str(db))
    # Ensure audit table exists and can record
    storage.log_action("ban", 10, 99, details="test", db_path=str(db))
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cur.execute("SELECT action, user_id, admin_id, details FROM audit WHERE user_id = ?", (10,))
    row = cur.fetchone()
    assert row is not None
    assert row[0] == "ban"
    assert row[1] == 10
    assert row[2] == 99
    assert row[3] == "test"
    conn.close()
