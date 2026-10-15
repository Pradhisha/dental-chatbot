"""Seed the dental chatbot database with test patients, slots and appointments.

Usage:
    python seed.py                      # creates dental.db next to this file
    python seed.py --db other.db        # different file
    python seed.py --today 2026-10-13   # pin "today" so dates are reproducible

Rebuilds the database from schema.sql. An existing database is first copied to
dental.db.bak-<timestamp>, so nothing is lost by accident. Dates are relative to
today in the clinic's timezone, so the data never goes stale. See TEST_PATIENTS.md for
what each patient is meant to test.
"""

import argparse
import random
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import shutil

import auth
from tools import CALENDAR_DAYS_AHEAD, CLOSE_HOUR, OPEN_HOUR, clinic_now, extend_calendar, is_open_day

HERE = Path(__file__).parent
PAST_DAYS = 14                         # past slots exist so "never offer a past time" can be tested
FILL_RATE = 0.30                       # share of future slots already taken


def years_ago(today, years, days_offset=0):
    """A date of birth that makes someone `years` old, shifted by days_offset."""
    try:
        d = today.replace(year=today.year - years)
    except ValueError:                 # today is Feb 29
        d = today.replace(year=today.year - years, day=28)
    return (d + timedelta(days=days_offset)).isoformat()


def build_patients(today):
    # (key, full_name, phone, date_of_birth, insurance_name, medical_notes)
    return [
        # Garcia family: one parent, three kids, all on the parent's phone.
        ("maria",   "Maria Garcia",  "2145550101", "1985-03-12", "Delta Dental", None),
        ("sofia",   "Sofia Garcia",  "2145550101", "2017-06-30", "Delta Dental", None),
        ("mateo",   "Mateo Garcia",  "2145550101", "2020-01-08", "Delta Dental", "Dental anxiety, does best with morning visits"),
        ("lucas",   "Lucas Garcia",  "2145550101", years_ago(today, 2, -40), "Delta Dental", "Never seen a dentist (first visit)"),
        # Married couple, separate phones, same insurer.
        ("james",   "James Carter",  "4695550142", "1968-11-02", "Aetna", "Takes warfarin (blood thinner)"),
        ("linda",   "Linda Carter",  "4695550143", "1970-04-19", "Aetna", None),
        # Older adults.
        ("dorothy", "Dorothy Williams", "9725550177", "1942-08-25", "Humana Medicare Advantage", "Hard of hearing; full upper denture"),
        ("harold",  "Harold Kim",    "2145550188", "1959-02-14", "Blue Cross Blue Shield", "Artificial heart valve: needs antibiotic premedication"),
        # Insurance edge cases.
        ("tyler",   "Tyler Brooks",  "8175550123", "1999-07-21", None, None),
        ("aaliyah", "Aaliyah Johnson", "2145550166", "1993-05-05", "Texas Medicaid", "Pregnant, second trimester"),
        ("bob",     "Bob Hendricks", "9725550199", "1979-12-01", "Careington discount plan", None),
        # Identity edge cases.
        ("smith1",  "Michael Smith", "2145550111", "1985-09-17", "Cigna", None),
        ("smith2",  "Michael Smith", "2145550112", "1992-02-03", "MetLife", None),
        ("lee1",    "David Lee",     "4695550120", "1990-05-14", "Guardian", None),
        ("lee2",    "David Lee",     "4695550121", "1990-05-14", "United Concordia", None),
        ("priya",   "Priya Patel",   "2145550129", "2004-02-29", "Cigna", None),
        ("ethan",   "Ethan Nguyen-Tran", "8175550155", years_ago(today, 18, 1), "Guardian", None),  # turns 18 tomorrow
        # Names that break naive string handling.
        ("siobhan", "Siobhán O'Brien", "2145550133", "1988-10-10", "MetLife", "Latex allergy"),
        ("jose",    "José Muñoz-Álvarez", "9725550144", "1975-03-03", "Delta Dental", "Type 2 diabetes; prefers Spanish"),
        ("chris",   "Christopher Alexander Montgomery-Fitzgerald III", "2145550150", "1983-07-04", "Aetna", None),
    ]


# Background patients only exist to fill the calendar. Not meant for testing.
BACKGROUND = [
    ("Karen Mitchell", "2145550201", "1971-01-15", "Aetna"),
    ("Daniel Ortiz", "2145550202", "1988-06-22", "Cigna"),
    ("Emily Zhang", "2145550203", "1995-09-09", "Delta Dental"),
    ("Marcus Reed", "2145550204", "1980-12-30", None),
    ("Grace Hall", "2145550205", "1966-03-27", "MetLife"),
    ("Samuel Adeyemi", "2145550206", "2001-11-11", "Guardian"),
]


def is_open(d):
    return is_open_day(d)


def backup(db_path: Path) -> Path | None:
    """Copy an existing database aside before it's replaced. Returns the backup path."""
    if not db_path.exists():
        return None
    target = db_path.with_name(f"{db_path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(db_path, target)
    return target


def next_weekday(today, weekday, weeks_ahead=1):
    """The given weekday (0=Mon) in the week `weeks_ahead` weeks from now."""
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=weeks_ahead)
    return monday + timedelta(days=weekday)


def next_open_day(d):
    d += timedelta(days=1)
    while not is_open(d):
        d += timedelta(days=1)
    return d


def seed(db_path, today, now):
    db_path.unlink(missing_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript((HERE / "schema.sql").read_text())
    auth.ensure_staff(con)

    # Patients
    pid = {}
    for key, name, phone, dob, ins, notes in build_patients(today):
        cur = con.execute(
            "INSERT INTO patients (full_name, phone, date_of_birth, insurance_name, medical_notes) VALUES (?,?,?,?,?)",
            (name, phone, dob, ins, notes))
        pid[key] = cur.lastrowid
    background_ids = [
        con.execute("INSERT INTO patients (full_name, phone, date_of_birth, insurance_name) VALUES (?,?,?,?)", row).lastrowid
        for row in BACKGROUND
    ]

    # Slots: 1 hour, 8am-6pm, Mon-Sat, from two weeks ago to CALENDAR_DAYS_AHEAD ahead.
    con.row_factory = None
    extend_calendar(con, today - timedelta(days=PAST_DAYS), today + timedelta(days=CALENDAR_DAYS_AHEAD))
    slot = {datetime.fromisoformat(starts): sid for sid, starts in con.execute("SELECT id, starts_at FROM slots")}

    def at(day, hour):
        return slot[datetime(day.year, day.month, day.day, hour)]

    def book(patient, day, hour, type_, status="booked", summary=None, booked_by=None):
        return con.execute(
            "INSERT INTO appointments (patient_id, slot_id, type, status, emergency_summary, booked_by_patient_id) VALUES (?,?,?,?,?,?)",
            (pid[patient], at(day, hour), type_, status, summary, pid.get(booked_by))).lastrowid

    tomorrow = next_open_day(today)
    next_tue = next_weekday(today, 1)
    next_thu = next_weekday(today, 3)
    next_fri = next_weekday(today, 4)
    in_two_weeks_wed = next_weekday(today, 2, weeks_ahead=2)
    last_week_mon = next_weekday(today, 0, weeks_ahead=-1)
    last_week_wed = next_weekday(today, 2, weeks_ahead=-1)

    # Garcia family: Maria booked the three of them back-to-back.
    book("maria", next_thu, 15, "cleaning", booked_by="maria")
    book("sofia", next_thu, 16, "checkup", booked_by="maria")
    book("mateo", next_thu, 17, "checkup", booked_by="maria")
    # James has two upcoming visits, so "reschedule my appointment" is ambiguous.
    book("james", next_tue, 9, "cleaning")
    book("james", in_two_weeks_wed, 14, "checkup")
    # Linda: only a cancelled appointment (it must not show as upcoming).
    book("linda", next_tue, 10, "cleaning", status="cancelled")
    # Dorothy: only a past visit (must not be reschedulable).
    book("dorothy", last_week_mon, 9, "checkup")
    # Harold: a visit later today, to test same-day cancel.
    later_today_hour = max(now.hour + 2, OPEN_HOUR) if now.date() == today else 16
    if is_open(today) and later_today_hour < CLOSE_HOUR:
        book("harold", today, later_today_hour, "checkup")
    else:
        book("harold", tomorrow, 8, "checkup")
    # Tyler: an emergency booked tomorrow, with the alert staff haven't seen yet.
    tyler_appt = book("tyler", tomorrow, 9, "emergency",
                      summary="Cracked molar, sharp pain when chewing since last night")
    # Michael Smith #1 has a visit; Michael Smith #2 must never see it.
    book("smith1", next_tue, 11, "checkup")
    # David Lee #2 has a visit; the phone number is the only thing telling the two apart.
    book("lee2", next_tue, 13, "cleaning")
    # Siobhán had an emergency last week.
    book("siobhan", last_week_wed, 11, "emergency", summary="Lost a filling, mild sensitivity")

    # Fill the calendar. Next Friday is fully booked ("nothing open that day");
    # every other future slot is taken with probability FILL_RATE.
    rng = random.Random(42)
    taken = {r[0] for r in con.execute("SELECT slot_id FROM appointments WHERE status='booked'")}
    for start, slot_id in slot.items():
        if slot_id in taken or start.date() < today:
            continue
        if start.date() == next_fri or rng.random() < FILL_RATE:
            con.execute("INSERT INTO appointments (patient_id, slot_id, type) VALUES (?,?,?)",
                        (rng.choice(background_ids), slot_id, rng.choice(["cleaning", "checkup"])))

    # A sample conversation with feedback, and staff notifications for the staff page.
    con.execute("INSERT INTO conversations (id, rating, feedback) VALUES (?,?,?)",
                ("demo-conversation-1", -1, "It kept asking for my birthday twice"))
    con.execute("INSERT INTO messages (conversation_id, role, content) VALUES (?,?,?)",
                ("demo-conversation-1", "user", "I need to cancel my cleaning"))
    con.execute(
        "INSERT INTO staff_notifications (kind, urgency, summary, patient_id) VALUES (?,?,?,?)",
        ("emergency", "high", f"Emergency booked (appointment {tyler_appt}): cracked molar, sharp pain", pid["tyler"]))
    con.execute(
        "INSERT INTO staff_notifications (kind, urgency, summary, patient_id, acknowledged_at) VALUES (?,?,?,?,datetime('now'))",
        ("callback", "normal", "Asked about a billing charge from her last visit", pid["linda"]))

    con.commit()
    return con


def summary(con, today):
    q = lambda sql, *a: con.execute(sql, a).fetchone()[0]
    future = today.isoformat()
    print(f"patients: {q('SELECT count(*) FROM patients')}")
    print(f"slots: {q('SELECT count(*) FROM slots')} "
          f"({q('SELECT count(*) FROM slots WHERE starts_at >= ?', future)} from today on)")
    open_future = q("""SELECT count(*) FROM slots s WHERE s.starts_at >= ? AND NOT EXISTS
                       (SELECT 1 FROM appointments a WHERE a.slot_id = s.id AND a.status = 'booked')""", future)
    print(f"open future slots: {open_future}")
    print(f"appointments: {q('SELECT count(*) FROM appointments')}")
    print(f"staff notifications: {q('SELECT count(*) FROM staff_notifications')}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(HERE / "dental.db"))
    ap.add_argument("--today", help="YYYY-MM-DD, defaults to the real date")
    args = ap.parse_args()
    today = date.fromisoformat(args.today) if args.today else clinic_now().date()
    now = clinic_now() if not args.today else datetime(today.year, today.month, today.day, 9)
    saved = backup(Path(args.db))
    if saved:
        print(f"Backed up the old database to {saved.name}")
    con = seed(Path(args.db), today, now)
    summary(con, today)
