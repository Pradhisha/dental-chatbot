"""Tests for staff accounts: password hashing, lockout, sessions, and upgrading an old database."""

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import auth
import tools
from seed import seed


@pytest.fixture
def con(tmp_path):
    seed(tmp_path / "t.db", date(2026, 10, 13), datetime(2026, 10, 13, 9)).close()
    c = tools.connect(tmp_path / "t.db")
    yield c
    c.close()


def test_passwords_are_hashed_not_stored(con):
    stored = con.execute("SELECT password_hash FROM staff_users WHERE username = 'frontdesk'").fetchone()[0]
    assert "SmileDemo2026" not in stored and stored.startswith("scrypt$")
    assert auth.check_password("SmileDemo2026", stored)
    assert not auth.check_password("smiledemo2026", stored)


def test_demo_accounts_are_created_once(con):
    auth.ensure_staff(con)
    auth.ensure_staff(con)
    assert [r[0] for r in con.execute("SELECT username FROM staff_users ORDER BY id")] == ["frontdesk", "manager"]


def test_login_and_session(con):
    token, user = auth.login(con, "manager", "SmileDemo2026")
    assert token and user["role"] == "admin"
    assert auth.current_user(con, token)["full_name"] == "Dana Lopez"
    auth.logout(con, token)
    assert auth.current_user(con, token) is None


def test_expired_session_is_rejected(con):
    token, _ = auth.login(con, "frontdesk", "SmileDemo2026")
    with con:
        con.execute("UPDATE staff_sessions SET expires_at = ?", ((datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)).isoformat(),))
    assert auth.current_user(con, token) is None


def test_lockout_after_five_wrong_passwords(con):
    for _ in range(auth.MAX_FAILED_LOGINS - 1):
        assert auth.login(con, "frontdesk", "nope")[1] == "Wrong username or password."
    assert "Too many failed attempts" in auth.login(con, "frontdesk", "nope")[1]
    # Even the right password is refused while locked.
    token, message = auth.login(con, "frontdesk", "SmileDemo2026")
    assert token is None and "Too many failed attempts" in message


def test_old_database_is_upgraded(tmp_path):
    """A database made before staff sign-in existed gets the new tables and column on start."""
    schema = (Path(auth.__file__).parent / "schema.sql").read_text()
    old_schema = schema[:schema.index("-- Front desk accounts")]
    old_schema = old_schema.replace("acknowledged_at TEXT,", "acknowledged_at TEXT").replace(
        "  acknowledged_by INTEGER REFERENCES staff_users(id)\n", "")
    assert "acknowledged_by" not in old_schema
    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.executescript(old_schema)
    old.execute("INSERT INTO staff_notifications (kind, urgency, summary) VALUES ('callback', 'normal', 'kept')")
    old.commit()
    old.close()

    con = tools.connect(db)
    auth.ensure_staff(con)
    assert tuple(con.execute("SELECT summary, acknowledged_by FROM staff_notifications").fetchone()) == ("kept", None)
    assert con.execute("SELECT count(*) FROM staff_users").fetchone()[0] == 2
