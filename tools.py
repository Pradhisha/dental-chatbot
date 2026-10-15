"""The 7 booking tools the LLM can call.

Plain Python against SQLite, no AI in here. The LLM only turns the patient's
words into arguments; every rule (who can touch which appointment, what's
open, no double-booking, emergency alerts) is enforced in this file or in the
database itself.

Every tool returns a dict:
    {"ok": True, ...data}                                  on success
    {"ok": False, "error": "<code>", "message": "<text>"}  on failure
The message is written for the LLM so it can recover (ask for the phone,
offer alternatives, and so on).
"""

import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

# All appointment times are stored as clinic-local wall-clock times ("2026-10-20T09:00").
# "Now" always comes from the clinic's timezone, never the server's, so a server running
# in UTC still knows it's 9pm in Dallas.
CLINIC_TZ = ZoneInfo(os.environ.get("CLINIC_TZ", "America/Chicago"))
OPEN_HOUR, CLOSE_HOUR = 8, 18         # last 1-hour slot starts at 17:00
CLOSED_WEEKDAYS = {6}                 # Sunday
CALENDAR_DAYS_AHEAD = 60              # how far ahead bookable slots always exist


def clinic_now() -> datetime:
    return datetime.now(CLINIC_TZ).replace(tzinfo=None, second=0, microsecond=0)


def is_open_day(d: date) -> bool:
    return d.weekday() not in CLOSED_WEEKDAYS


def extend_calendar(con, start: date, through: date) -> int:
    """Make sure every opening hour from start to through has a slot. Safe to run repeatedly;
    existing slots and their bookings are untouched. Returns how many slots were added."""
    rows = []
    d = start
    while d <= through:
        if is_open_day(d):
            for h in range(OPEN_HOUR, CLOSE_HOUR):
                begin = datetime(d.year, d.month, d.day, h)
                rows.append((begin.isoformat(timespec="minutes"),
                             (begin + timedelta(hours=1)).isoformat(timespec="minutes")))
        d += timedelta(days=1)
    with con:
        before = con.total_changes
        con.executemany("INSERT OR IGNORE INTO slots (starts_at, ends_at) VALUES (?,?)", rows)
        return con.total_changes - before


APPOINTMENT_TYPES = ("cleaning", "checkup", "emergency")
MAX_FAILED_VERIFICATIONS = 3          # per conversation, so nobody can guess birthdays
TIME_OF_DAY = {                       # by slot start hour
    "morning": (8, 12),
    "afternoon": (12, 16),
    "evening": (16, 18),
    "any": (0, 24),
}
MAX_SEARCH_DAYS = 60                  # widest date range one search may cover
ALTERNATIVES_LOOKAHEAD_DAYS = 30      # how far past the range to look when nothing is open


@dataclass
class Session:
    """Per-conversation state. Lives on the server, never in the prompt."""
    conversation_id: str | None = None
    verified_patient_ids: set[int] = field(default_factory=set)
    booker_id: int | None = None      # first patient verified or registered in this chat
    failed_verifications: int = 0
    fixed_now: datetime | None = None # for tests and demos

    def now(self) -> datetime:
        return self.fixed_now or clinic_now()

    def add_patient(self, patient_id: int) -> None:
        self.verified_patient_ids.add(patient_id)
        if self.booker_id is None:
            self.booker_id = patient_id


def connect(db_path) -> sqlite3.Connection:
    # The web server shares one connection across request threads, behind a lock.
    con = sqlite3.connect(db_path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


# ---------- helpers ----------

def _ok(**data):
    return {"ok": True, **data}


def _err(code, message, **data):
    return {"ok": False, "error": code, "message": message, **data}


def normalize_name(name: str) -> str:
    """'Siobhán O'Brien ' -> 'siobhanobrien'. Accents, case, spaces and punctuation don't matter."""
    stripped = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in stripped if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", stripped.lower())


def normalize_phone(phone: str) -> str | None:
    """Any US format -> 10 digits, or None if it isn't a valid number."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _label(starts_at: str) -> str:
    """'2026-10-20T09:00' -> 'Tue Oct 20, 9:00 AM'."""
    dt = datetime.fromisoformat(starts_at)
    hour = dt.hour % 12 or 12
    return f"{dt:%a %b} {dt.day}, {hour}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'}"


def _upcoming_appointments(con, session, patient_id):
    rows = con.execute(
        """SELECT a.id, a.type, s.starts_at FROM appointments a JOIN slots s ON s.id = a.slot_id
           WHERE a.patient_id = ? AND a.status = 'booked' AND s.starts_at > ?
           ORDER BY s.starts_at""",
        (patient_id, session.now().isoformat(timespec="minutes"))).fetchall()
    return [{"appointment_id": r["id"], "type": r["type"], "starts_at": r["starts_at"],
             "label": _label(r["starts_at"])} for r in rows]


def _patient_summary(row):
    # Deliberately leaves out phone and medical_notes: the bot never reads those back.
    return {"patient_id": row["id"], "full_name": row["full_name"],
            "insurance_name": row["insurance_name"]}


def _open_slots(con, session, start: date, end: date, time_of_day="any"):
    """Open slots between two dates (inclusive), never in the past, in time order."""
    lo, hi = TIME_OF_DAY[time_of_day]
    rows = con.execute(
        """SELECT s.id, s.starts_at, s.ends_at FROM slots s
           WHERE s.starts_at > ? AND s.starts_at >= ? AND s.starts_at < ?
             AND NOT EXISTS (SELECT 1 FROM appointments a
                             WHERE a.slot_id = s.id AND a.status = 'booked')
           ORDER BY s.starts_at""",
        (session.now().isoformat(timespec="minutes"), start.isoformat(),
         (end + timedelta(days=1)).isoformat())).fetchall()
    return [r for r in rows if lo <= datetime.fromisoformat(r["starts_at"]).hour < hi]


def _runs(slots, n):
    """Groups of n back-to-back open slots. A slot can start more than one run."""
    runs = []
    for i in range(len(slots) - n + 1):
        group = slots[i:i + n]
        if all(group[j]["ends_at"] == group[j + 1]["starts_at"] for j in range(n - 1)):
            runs.append(group)
    return runs


def _spread(options, limit):
    """Pick options across different days (round-robin) instead of 5 on one morning."""
    by_day = {}
    for opt in options:
        by_day.setdefault(opt[0]["starts_at"][:10], []).append(opt)
    picked, round_ = [], 0
    while len(picked) < limit and any(round_ < len(v) for v in by_day.values()):
        for day in sorted(by_day):
            if round_ < len(by_day[day]) and len(picked) < limit:
                picked.append(by_day[day][round_])
        round_ += 1
    return sorted(picked, key=lambda g: g[0]["starts_at"])


def _format_options(groups):
    return [{"slot_ids": [s["id"] for s in g], "starts_at": g[0]["starts_at"],
             "ends_at": g[-1]["ends_at"], "label": _label(g[0]["starts_at"])} for g in groups]


def _alternatives_near(con, session, slot_id, limit=3):
    """Open slots around a slot that just turned out to be taken."""
    row = con.execute("SELECT starts_at FROM slots WHERE id = ?", (slot_id,)).fetchone()
    if not row:
        return []
    day = datetime.fromisoformat(row["starts_at"]).date()
    slots = _open_slots(con, session, day, day + timedelta(days=7))
    return _format_options(_spread([[s] for s in slots], limit))


def _age(dob: str, today: date) -> int:
    born = date.fromisoformat(dob)
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def _children_on_file(con, session, guardian):
    """Minors who share a verified adult's phone. A parent can book and manage visits
    for their kids, so they're added to the session too. Adults sharing a phone are not."""
    today = session.now().date()
    if _age(guardian["date_of_birth"], today) < 18:
        return []
    children = []
    for r in con.execute("SELECT * FROM patients WHERE phone = ? AND id != ?", (guardian["phone"], guardian["id"])):
        if _age(r["date_of_birth"], today) < 18:
            session.add_patient(r["id"])
            children.append({**_patient_summary(r), "age": _age(r["date_of_birth"], today),
                             "upcoming_appointments": _upcoming_appointments(con, session, r["id"])})
    return children


def _ensure_conversation(con, session):
    if session.conversation_id:
        con.execute("INSERT OR IGNORE INTO conversations (id) VALUES (?)", (session.conversation_id,))
    return session.conversation_id


def _owned_appointment(con, session, appointment_id):
    """The appointment, if it belongs to a patient verified in this chat. Otherwise None,
    so the bot can't tell 'not yours' apart from 'doesn't exist'."""
    row = con.execute(
        """SELECT a.*, s.starts_at FROM appointments a JOIN slots s ON s.id = a.slot_id
           WHERE a.id = ?""", (appointment_id,)).fetchone()
    if row is None or row["patient_id"] not in session.verified_patient_ids:
        return None
    return row


# ---------- the 7 tools ----------

def verify_patient(con, session, full_name: str, date_of_birth: str, phone: str | None = None):
    """Find an existing patient by name + date of birth (phone breaks ties)."""
    if session.failed_verifications >= MAX_FAILED_VERIFICATIONS:
        return _err("too_many_attempts", "Too many failed attempts in this chat. Don't try again; "
                                         "ask them to call the office to confirm their details.")
    dob = _parse_date(date_of_birth)
    if dob is None:
        return _err("invalid_date_of_birth", "Date of birth must be YYYY-MM-DD.")
    wanted = normalize_name(full_name)
    matches = [r for r in con.execute("SELECT * FROM patients WHERE date_of_birth = ?", (dob.isoformat(),))
               if normalize_name(r["full_name"]) == wanted]

    if len(matches) > 1:
        if not phone:
            return _err("need_phone", "More than one patient matches. Ask for the phone number on file.")
        digits = normalize_phone(phone)
        matches = [r for r in matches if r["phone"] == digits]
    if len(matches) != 1:
        session.failed_verifications += 1
        # No hint about near matches: that would leak who is a patient here.
        return _err("not_found", "No patient matches those details. Ask them to double-check, "
                                 "or offer to register them as a new patient.")

    patient = matches[0]
    session.add_patient(patient["id"])
    return _ok(patient=_patient_summary(patient),
               upcoming_appointments=_upcoming_appointments(con, session, patient["id"]),
               family_on_file=_children_on_file(con, session, patient))


def register_patient(con, session, full_name: str, phone: str, date_of_birth: str,
                     insurance_name: str | None = None):
    """Create a new patient. insurance_name None means self-pay."""
    full_name = (full_name or "").strip()
    if len(normalize_name(full_name)) < 2:
        return _err("invalid_name", "Ask for the patient's full name.")
    digits = normalize_phone(phone)
    if digits is None:
        return _err("invalid_phone", "Phone must be a 10-digit US number.")
    dob = _parse_date(date_of_birth)
    today = session.now().date()
    if dob is None or dob > today or dob.year < today.year - 120:
        return _err("invalid_date_of_birth", "Date of birth must be a real past date, as YYYY-MM-DD.")

    existing = [r for r in con.execute("SELECT * FROM patients WHERE date_of_birth = ?", (dob.isoformat(),))
                if normalize_name(r["full_name"]) == normalize_name(full_name)]
    if existing:
        return _err("already_exists", "This person is already a patient. Use verify_patient instead.")

    insurance = (insurance_name or "").strip() or None
    with con:
        cur = con.execute(
            "INSERT INTO patients (full_name, phone, date_of_birth, insurance_name) VALUES (?,?,?,?)",
            (full_name, digits, dob.isoformat(), insurance))
    session.add_patient(cur.lastrowid)
    return _ok(patient={"patient_id": cur.lastrowid, "full_name": full_name, "insurance_name": insurance})


def find_available_slots(con, session, start_date: str, end_date: str, time_of_day: str = "any",
                         consecutive: int = 1, limit: int = 5):
    """Open times in a date range. consecutive=N finds N back-to-back slots (family bookings).
    If nothing is open, returns the next openings after the range instead."""
    start, end = _parse_date(start_date), _parse_date(end_date)
    if start is None or end is None:
        return _err("invalid_date", "Dates must be YYYY-MM-DD.")
    if end < start:
        return _err("invalid_range", "end_date is before start_date.")
    today = session.now().date()
    if end < today:
        return _err("date_in_past", f"That range is in the past. Today is {today.isoformat()}.")
    if time_of_day not in TIME_OF_DAY:
        return _err("invalid_time_of_day", f"time_of_day must be one of {list(TIME_OF_DAY)}.")
    if not 1 <= consecutive <= 4:
        return _err("invalid_consecutive", "consecutive must be between 1 and 4.")
    start = max(start, today)
    end = min(end, start + timedelta(days=MAX_SEARCH_DAYS))
    limit = max(1, min(limit, 10))

    def search(a, b):
        return _spread(_runs(_open_slots(con, session, a, b, time_of_day), consecutive), limit)

    found = search(start, end)
    if found:
        return _ok(options=_format_options(found))
    later = search(end + timedelta(days=1), end + timedelta(days=ALTERNATIVES_LOOKAHEAD_DAYS))
    return _ok(options=[], next_available=_format_options(later),
               message="Nothing open in that range (the office is closed Sundays). "
                       "Offer next_available instead.")


def book_appointments(con, session, bookings: list[dict]):
    """Book one or more appointments in a single transaction: all succeed or none do.
    Each booking: {patient_id, slot_id, type, emergency_summary?}."""
    if not bookings:
        return _err("no_bookings", "Nothing to book.")
    slot_ids = [b.get("slot_id") for b in bookings]
    if len(set(slot_ids)) != len(slot_ids):
        return _err("duplicate_slot", "Each person needs a different slot.")

    now = session.now().isoformat(timespec="minutes")
    for b in bookings:
        if b.get("patient_id") not in session.verified_patient_ids:
            return _err("patient_not_verified",
                        f"Patient {b.get('patient_id')} isn't verified in this chat. Verify or register them first.")
        if b.get("type") not in APPOINTMENT_TYPES:
            return _err("invalid_type", f"type must be one of {list(APPOINTMENT_TYPES)}.")
        if b["type"] == "emergency" and not (b.get("emergency_summary") or "").strip():
            return _err("missing_emergency_summary", "Ask for a short description of the emergency first.")
        slot = con.execute("SELECT starts_at FROM slots WHERE id = ?", (b["slot_id"],)).fetchone()
        if slot is None or slot["starts_at"] <= now:
            return _err("invalid_slot", f"Slot {b['slot_id']} doesn't exist or is in the past. "
                                        "Only offer slots from find_available_slots.")

    booked = []
    try:
        with con:  # one transaction: commits at the end, rolls back on any error
            conversation_id = _ensure_conversation(con, session)
            for b in bookings:
                booked_by = session.booker_id if session.booker_id != b["patient_id"] else None
                cur = con.execute(
                    """INSERT INTO appointments (patient_id, slot_id, type, emergency_summary, booked_by_patient_id)
                       VALUES (?,?,?,?,?)""",
                    (b["patient_id"], b["slot_id"], b["type"], b.get("emergency_summary"), booked_by))
                if b["type"] == "emergency":
                    # Alert staff in code, so it never depends on the model remembering to.
                    con.execute(
                        """INSERT INTO staff_notifications (kind, urgency, summary, patient_id, conversation_id)
                           VALUES ('emergency', 'high', ?, ?, ?)""",
                        (f"Emergency booked (appointment {cur.lastrowid}): {b['emergency_summary']}",
                         b["patient_id"], conversation_id))
                booked.append(cur.lastrowid)
    except sqlite3.IntegrityError:
        taken = next((s for s in slot_ids if con.execute(
            "SELECT 1 FROM appointments WHERE slot_id = ? AND status = 'booked'", (s,)).fetchone()), None)
        return _err("slot_unavailable", "That time was just taken. Nothing was booked. Offer the alternatives.",
                    slot_id=taken, alternatives=_alternatives_near(con, session, taken))

    rows = con.execute(
        f"""SELECT a.id, a.patient_id, a.type, s.starts_at, p.full_name FROM appointments a
            JOIN slots s ON s.id = a.slot_id JOIN patients p ON p.id = a.patient_id
            WHERE a.id IN ({','.join('?' * len(booked))}) ORDER BY s.starts_at""", booked).fetchall()
    return _ok(appointments=[{"appointment_id": r["id"], "patient_id": r["patient_id"],
                              "full_name": r["full_name"], "type": r["type"],
                              "starts_at": r["starts_at"], "label": _label(r["starts_at"])} for r in rows],
               staff_alerted=any(b["type"] == "emergency" for b in bookings))


def reschedule_appointment(con, session, appointment_id: int, new_slot_id: int):
    """Move an upcoming appointment to another open slot."""
    appt = _owned_appointment(con, session, appointment_id)
    now = session.now().isoformat(timespec="minutes")
    if appt is None or appt["status"] != "booked" or appt["starts_at"] <= now:
        return _err("not_found", "No upcoming appointment with that id for the verified patient.")
    if appt["slot_id"] == new_slot_id:
        return _err("same_slot", "That's the time they already have.")
    slot = con.execute("SELECT starts_at FROM slots WHERE id = ?", (new_slot_id,)).fetchone()
    if slot is None or slot["starts_at"] <= now:
        return _err("invalid_slot", "Only offer slots from find_available_slots.")
    try:
        with con:
            con.execute("UPDATE appointments SET slot_id = ?, updated_at = datetime('now') WHERE id = ?",
                        (new_slot_id, appointment_id))
    except sqlite3.IntegrityError:
        return _err("slot_unavailable", "That time was just taken. The appointment wasn't moved.",
                    alternatives=_alternatives_near(con, session, new_slot_id))
    return _ok(appointment_id=appointment_id, old_label=_label(appt["starts_at"]),
               new_starts_at=slot["starts_at"], new_label=_label(slot["starts_at"]))


def cancel_appointment(con, session, appointment_id: int):
    """Cancel an upcoming appointment. The slot becomes bookable again."""
    appt = _owned_appointment(con, session, appointment_id)
    if appt is None or appt["status"] != "booked" or appt["starts_at"] <= session.now().isoformat(timespec="minutes"):
        return _err("not_found", "No upcoming appointment with that id for the verified patient.")
    with con:
        con.execute("UPDATE appointments SET status = 'cancelled', updated_at = datetime('now') WHERE id = ?",
                    (appointment_id,))
    return _ok(appointment_id=appointment_id, cancelled_label=_label(appt["starts_at"]))


def notify_staff(con, session, kind: str, summary: str, urgency: str = "normal",
                 patient_id: int | None = None):
    """Hand something to a human: escalation, callback request, or an emergency that isn't being booked."""
    if kind not in ("emergency", "escalation", "callback"):
        return _err("invalid_kind", "kind must be emergency, escalation or callback.")
    if urgency not in ("normal", "high"):
        return _err("invalid_urgency", "urgency must be normal or high.")
    if not (summary or "").strip():
        return _err("missing_summary", "Write a one-line summary for staff.")
    if patient_id is not None and patient_id not in session.verified_patient_ids:
        patient_id = None  # don't attach an unverified patient; staff still get the summary
    with con:
        cur = con.execute(
            """INSERT INTO staff_notifications (kind, urgency, summary, patient_id, conversation_id)
               VALUES (?,?,?,?,?)""",
            (kind, urgency, summary.strip(), patient_id, _ensure_conversation(con, session)))
    return _ok(notification_id=cur.lastrowid)
