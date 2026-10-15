"""Staff accounts and sign-in.

Passwords are hashed with scrypt (Python standard library), sessions are random
tokens stored in the database and sent as an HttpOnly cookie. No extra packages.
"""

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

SESSION_HOURS = 8                 # one working day
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15

# Demo accounts created on first start. Override the password with STAFF_DEFAULT_PASSWORD.
DEFAULT_STAFF = [
    ("frontdesk", "Jordan Ellis", "front_desk"),
    ("manager", "Dana Lopez", "admin"),
]
DEMO_PASSWORD = "SmileDemo2026"



def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return hmac.compare_digest(digest.hex(), digest_hex)


def ensure_staff(con) -> None:
    """Bring an older database up to date and add the demo accounts once. Safe to call every start."""
    con.executescript((Path(__file__).parent / "schema.sql").read_text())
    columns = {r[1] for r in con.execute("PRAGMA table_info(staff_notifications)")}
    if "acknowledged_by" not in columns:
        con.execute("ALTER TABLE staff_notifications ADD COLUMN acknowledged_by INTEGER REFERENCES staff_users(id)")
    if con.execute("SELECT count(*) FROM staff_users").fetchone()[0] == 0:
        password = os.environ.get("STAFF_DEFAULT_PASSWORD", DEMO_PASSWORD)
        con.executemany(
            "INSERT INTO staff_users (username, full_name, role, password_hash) VALUES (?,?,?,?)",
            [(u, name, role, hash_password(password)) for u, name, role in DEFAULT_STAFF])
    con.commit()


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def login(con, username: str, password: str):
    """Returns (session token, user dict) or (None, error message). Same message for unknown
    user and wrong password, so the page doesn't reveal which usernames exist."""
    user = con.execute("SELECT * FROM staff_users WHERE username = ?", ((username or "").strip(),)).fetchone()
    now = _utc_now()
    if user and user["locked_until"] and datetime.fromisoformat(user["locked_until"]) > now:
        return None, f"Too many failed attempts. Try again in {LOCKOUT_MINUTES} minutes or ask your manager."
    if not user or not check_password(password or "", user["password_hash"]):
        if user:
            failed = user["failed_logins"] + 1
            locked = (now + timedelta(minutes=LOCKOUT_MINUTES)).isoformat() if failed >= MAX_FAILED_LOGINS else None
            with con:
                con.execute("UPDATE staff_users SET failed_logins = ?, locked_until = ? WHERE id = ?",
                            (0 if locked else failed, locked, user["id"]))
            if locked:
                return None, f"Too many failed attempts. Try again in {LOCKOUT_MINUTES} minutes or ask your manager."
        return None, "Wrong username or password."

    token = secrets.token_urlsafe(32)
    with con:
        con.execute("UPDATE staff_users SET failed_logins = 0, locked_until = NULL, last_login_at = ? WHERE id = ?",
                    (now.isoformat(), user["id"]))
        con.execute("INSERT INTO staff_sessions (token, user_id, expires_at) VALUES (?,?,?)",
                    (token, user["id"], (now + timedelta(hours=SESSION_HOURS)).isoformat()))
    return token, {"id": user["id"], "username": user["username"], "full_name": user["full_name"], "role": user["role"]}


def current_user(con, token: str | None):
    if not token:
        return None
    row = con.execute(
        """SELECT u.id, u.username, u.full_name, u.role, s.expires_at FROM staff_sessions s
           JOIN staff_users u ON u.id = s.user_id WHERE s.token = ?""", (token,)).fetchone()
    if row is None or datetime.fromisoformat(row["expires_at"]) <= _utc_now():
        return None
    return {"id": row["id"], "username": row["username"], "full_name": row["full_name"], "role": row["role"]}


def logout(con, token: str | None) -> None:
    if token:
        with con:
            con.execute("DELETE FROM staff_sessions WHERE token = ?", (token,))
