"""Web server: the patient chat page, the staff dashboard, and their JSON APIs.

Run:  uvicorn server:app --reload
Then open http://localhost:8000 (chat) and http://localhost:8000/staff (front desk sign-in:
frontdesk / SmileDemo2026, or set STAFF_DEFAULT_PASSWORD before the first start).

Set RESEED=1 to rebuild the demo database on startup (the old one is backed up first).
Set CLINIC_TZ (default America/Chicago) if the clinic isn't in Central time.
"""

import os
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

import auth
import tools
from bot import Bot, PRACTICE
from seed import backup, seed

HERE = Path(__file__).parent


def load_env_file(path=HERE / ".env"):
    """Minimal .env support so the key can live in a file. Real env vars win."""
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"'))


load_env_file()
DB_PATH = Path(os.environ.get("DB_PATH", HERE / "dental.db"))
if os.environ.get("RESEED") == "1" or not DB_PATH.exists():
    saved = backup(DB_PATH)
    if saved:
        print(f"RESEED=1: backed up the old database to {saved.name}")
    seed(DB_PATH, tools.clinic_now().date(), tools.clinic_now()).close()

con = tools.connect(DB_PATH)
auth.ensure_staff(con)          # adds the staff tables to older databases, and the demo accounts
db_lock = threading.Lock()
calendar_checked_on = None      # the clinic date the rolling calendar was last extended


def keep_calendar_open():
    """Once per clinic day, add slots so there are always CALENDAR_DAYS_AHEAD days to book."""
    global calendar_checked_on
    today = tools.clinic_now().date()
    if calendar_checked_on != today:
        with db_lock:
            tools.extend_calendar(con, today, today + timedelta(days=tools.CALENDAR_DAYS_AHEAD))
        calendar_checked_on = today


keep_calendar_open()
bot = Bot(con, db_lock=db_lock)
conversations = {}          # conversation id -> bot.Conversation (in memory; fine for one server)
conversations_lock = threading.Lock()

app = FastAPI(title=f"{PRACTICE['name']} assistant")


# ---------- pages ----------

@app.get("/")
def chat_page():
    return FileResponse(HERE / "static" / "index.html")


COOKIE = "staff_session"


def staff_user(staff_session: str | None = Cookie(default=None)):
    with db_lock:
        return auth.current_user(con, staff_session)


def require_staff(user=Depends(staff_user)):
    if user is None:
        raise HTTPException(401, "Please sign in")
    return user


@app.get("/staff")
def staff_page(user=Depends(staff_user)):
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return FileResponse(HERE / "static" / "staff.html")


@app.get("/login")
def login_page(user=Depends(staff_user)):
    if user is not None:
        return RedirectResponse("/staff", status_code=303)
    return FileResponse(HERE / "static" / "login.html")


@app.get("/api/health")
def health(response: Response):
    """Readiness: the database answers, there are slots to book, and a Gemini key is configured."""
    checks = {}
    try:
        with db_lock:
            horizon = con.execute("SELECT max(starts_at) FROM slots").fetchone()[0]
        checks["database"] = "ok"
        checks["bookable_until"] = horizon
    except sqlite3.Error as e:
        checks["database"] = f"error: {e}"
    checks["gemini_key"] = "set" if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") else "missing"
    ok = checks["database"] == "ok" and checks["gemini_key"] == "set"
    if not ok:
        response.status_code = 503
    return {"ok": ok, **checks}


# ---------- patient chat ----------

class ChatIn(BaseModel):
    conversation_id: str = Field(min_length=8, max_length=64)
    message: str = Field(min_length=1, max_length=2000)


@app.post("/api/chat")
def chat(body: ChatIn):
    keep_calendar_open()
    with conversations_lock:
        conv = conversations.get(body.conversation_id)
        if conv is None:
            conv = conversations[body.conversation_id] = bot.new_conversation(body.conversation_id)
    return bot.reply(conv, body.message)


class FeedbackIn(BaseModel):
    conversation_id: str = Field(min_length=8, max_length=64)
    rating: int = Field(ge=-1, le=1)
    comment: str | None = Field(default=None, max_length=1000)


@app.post("/api/feedback")
def feedback(body: FeedbackIn):
    if body.rating == 0:
        raise HTTPException(400, "rating must be 1 or -1")
    with db_lock, con:
        con.execute("INSERT OR IGNORE INTO conversations (id) VALUES (?)", (body.conversation_id,))
        con.execute("UPDATE conversations SET rating = ?, feedback = COALESCE(?, feedback) WHERE id = ?",
                    (body.rating, (body.comment or "").strip() or None, body.conversation_id))
    return {"ok": True}


# ---------- staff sign-in ----------

class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


@app.post("/api/login")
def login(body: LoginIn, response: Response):
    with db_lock:
        token, result = auth.login(con, body.username, body.password)
    if token is None:
        raise HTTPException(401, result)
    # HttpOnly: page scripts can't read it. SameSite=Lax: other sites can't post with it.
    # Add secure=True when served over HTTPS.
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax", max_age=auth.SESSION_HOURS * 3600)
    return {"ok": True, "user": result}


@app.post("/api/logout")
def logout(response: Response, staff_session: str | None = Cookie(default=None)):
    with db_lock:
        auth.logout(con, staff_session)
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(user=Depends(require_staff)):
    return user


# ---------- staff dashboard (signed-in staff only) ----------

@app.get("/api/staff")
def staff_data(user=Depends(require_staff)):
    keep_calendar_open()
    now = tools.clinic_now()
    week_out = (now + timedelta(days=7)).isoformat(timespec="minutes")
    with db_lock:
        alerts = [dict(r) for r in con.execute(
            """SELECT n.id, n.kind, n.urgency, n.summary, n.created_at, n.acknowledged_at, p.full_name, p.phone,
                      su.full_name AS acknowledged_by
               FROM staff_notifications n LEFT JOIN patients p ON p.id = n.patient_id
               LEFT JOIN staff_users su ON su.id = n.acknowledged_by
               ORDER BY n.acknowledged_at IS NOT NULL, n.urgency = 'high' DESC, n.created_at DESC
               LIMIT 30""")]
        upcoming = [dict(r) for r in con.execute(
            """SELECT a.id, a.type, a.emergency_summary, s.starts_at, p.full_name, p.insurance_name,
                      p.medical_notes, b.full_name AS booked_by
               FROM appointments a JOIN slots s ON s.id = a.slot_id JOIN patients p ON p.id = a.patient_id
               LEFT JOIN patients b ON b.id = a.booked_by_patient_id
               WHERE a.status = 'booked' AND s.starts_at >= ? AND s.starts_at < ?
               ORDER BY s.starts_at LIMIT 60""", (now.isoformat(timespec="minutes"), week_out))]
        feedback_rows = [dict(r) for r in con.execute(
            """SELECT id, rating, feedback, created_at FROM conversations
               WHERE rating IS NOT NULL ORDER BY created_at DESC LIMIT 10""")]
        rated = con.execute("SELECT count(*), sum(rating = 1) FROM conversations WHERE rating IS NOT NULL").fetchone()
        # Counts come from their own queries, not from the capped lists above.
        open_emergencies, open_alerts = con.execute(
            """SELECT sum(kind = 'emergency'), count(*) FROM staff_notifications
               WHERE acknowledged_at IS NULL""").fetchone()
        upcoming_count = con.execute(
            """SELECT count(*) FROM appointments a JOIN slots s ON s.id = a.slot_id
               WHERE a.status = 'booked' AND s.starts_at >= ? AND s.starts_at < ?""",
            (now.isoformat(timespec="minutes"), week_out)).fetchone()[0]
    return {
        "user": user,
        "kpis": {
            "open_emergencies": open_emergencies or 0,
            "open_alerts": open_alerts,
            "upcoming_7_days": upcoming_count,
            "thumbs_up_pct": round(100 * (rated[1] or 0) / rated[0]) if rated[0] else None,
            "ratings": rated[0],
        },
        "alerts": alerts,
        "upcoming": upcoming,
        "feedback": feedback_rows,
    }


@app.post("/api/staff/alerts/{alert_id}/ack")
def acknowledge(alert_id: int, user=Depends(require_staff)):
    with db_lock, con:
        cur = con.execute("UPDATE staff_notifications SET acknowledged_at = datetime('now'), acknowledged_by = ? "
                          "WHERE id = ? AND acknowledged_at IS NULL", (user["id"], alert_id))
    if cur.rowcount == 0:
        raise HTTPException(404, "Alert not found or already acknowledged")
    return {"ok": True}
