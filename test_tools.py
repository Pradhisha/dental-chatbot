"""Tests for the booking tools, run against the seeded test patients.

Run: pytest -q

"Today" is pinned to Tue 2026-10-13 at 9:00 AM, so the seeded calendar is
always the same. See TEST_PATIENTS.md for who's who.
"""

from datetime import date, datetime, timedelta

import pytest

import tools
from seed import seed

TODAY = date(2026, 10, 13)
NOW = datetime(2026, 10, 13, 9, 0)
NEXT_FRIDAY = "2026-10-23"           # fully booked by the seed


@pytest.fixture
def con(tmp_path):
    seed(tmp_path / "test.db", TODAY, NOW).close()
    c = tools.connect(tmp_path / "test.db")
    yield c
    c.close()


@pytest.fixture
def session():
    return tools.Session(conversation_id="test-convo", fixed_now=NOW)


def slot_at(con, starts_at):
    return con.execute("SELECT id FROM slots WHERE starts_at = ?", (starts_at,)).fetchone()["id"]


def open_slot(con, session, day="2026-10-27"):
    return tools.find_available_slots(con, session, day, day)["options"][0]["slot_ids"][0]


def verify(con, session, name, dob, phone=None):
    result = tools.verify_patient(con, session, name, dob, phone)
    assert result["ok"], result
    return result


def count(con, sql, *args):
    return con.execute(sql, args).fetchone()[0]


# ---------- the six from the plan ----------

def test_double_booking_is_rejected_with_alternatives(con, session):
    tyler = verify(con, session, "Tyler Brooks", "1999-07-21")["patient"]["patient_id"]
    marias_slot = slot_at(con, "2026-10-22T15:00")

    result = tools.book_appointments(con, session, [{"patient_id": tyler, "slot_id": marias_slot, "type": "cleaning"}])

    assert result["error"] == "slot_unavailable"
    assert result["alternatives"], "should offer other times"


def test_family_booking_is_all_or_nothing(con, session):
    maria = verify(con, session, "Maria Garcia", "1985-03-12")["patient"]["patient_id"]
    sofia = verify(con, session, "Sofia Garcia", "2017-06-30")["patient"]["patient_id"]
    lucas = con.execute("SELECT id FROM patients WHERE full_name = 'Lucas Garcia'").fetchone()["id"]
    session.add_patient(lucas)
    before = count(con, "SELECT count(*) FROM appointments")

    result = tools.book_appointments(con, session, [
        {"patient_id": maria, "slot_id": open_slot(con, session), "type": "cleaning"},
        {"patient_id": sofia, "slot_id": open_slot(con, session, "2026-10-28"), "type": "checkup"},
        {"patient_id": lucas, "slot_id": slot_at(con, NEXT_FRIDAY + "T10:00"), "type": "checkup"},  # taken
    ])

    assert result["error"] == "slot_unavailable"
    assert count(con, "SELECT count(*) FROM appointments") == before, "nobody should be booked"


def test_patient_cannot_cancel_someone_elses_appointment(con, session):
    verify(con, session, "Michael Smith", "1992-02-03")          # the second Michael Smith
    other_smiths_appt = con.execute(
        """SELECT a.id FROM appointments a JOIN patients p ON p.id = a.patient_id
           WHERE p.date_of_birth = '1985-09-17'""").fetchone()["id"]

    result = tools.cancel_appointment(con, session, other_smiths_appt)

    assert result["error"] == "not_found"
    assert count(con, "SELECT count(*) FROM appointments WHERE id = ? AND status = 'booked'", other_smiths_appt) == 1


def test_same_name_and_birthday_needs_phone(con, session):
    first = tools.verify_patient(con, session, "David Lee", "1990-05-14")
    assert first["error"] == "need_phone"
    assert session.verified_patient_ids == set()

    second = verify(con, session, "David Lee", "1990-05-14", phone="(469) 555-0121")
    assert second["patient"]["insurance_name"] == "United Concordia"
    assert len(second["upcoming_appointments"]) == 1


def test_fully_booked_day_offers_next_available(con, session):
    result = tools.find_available_slots(con, session, NEXT_FRIDAY, NEXT_FRIDAY)

    assert result["options"] == []
    assert result["next_available"]
    assert all(o["starts_at"] > NEXT_FRIDAY for o in result["next_available"])


def test_emergency_booking_alerts_staff_automatically(con, session):
    patient = tools.register_patient(con, session, "Nina Park", "214-555-0999", "1990-01-01", None)
    before = count(con, "SELECT count(*) FROM staff_notifications WHERE kind = 'emergency'")

    result = tools.book_appointments(con, session, [{
        "patient_id": patient["patient"]["patient_id"], "slot_id": open_slot(con, session, "2026-10-14"),
        "type": "emergency", "emergency_summary": "Knocked out front tooth"}])

    assert result["ok"] and result["staff_alerted"]
    assert count(con, "SELECT count(*) FROM staff_notifications WHERE kind = 'emergency'") == before + 1


# ---------- other edge cases from TEST_PATIENTS.md ----------

def test_accents_and_punctuation_still_match(con, session):
    assert verify(con, session, "siobhan obrien", "1988-10-10")["patient"]["full_name"] == "Siobhán O'Brien"


def test_wrong_birthday_gives_no_hint(con, session):
    result = tools.verify_patient(con, session, "Maria Garcia", "1985-03-13")
    assert result["error"] == "not_found"
    assert "Maria" not in result["message"]


def test_cannot_book_for_unverified_patient(con, session):
    result = tools.book_appointments(con, session, [{"patient_id": 1, "slot_id": open_slot(con, session), "type": "checkup"}])
    assert result["error"] == "patient_not_verified"


def test_register_rejects_bad_input_and_existing_patients(con, session):
    assert tools.register_patient(con, session, "Baby Future", "2145550000", "2027-01-01")["error"] == "invalid_date_of_birth"
    assert tools.register_patient(con, session, "Short Phone", "555-0101", "1990-01-01")["error"] == "invalid_phone"
    assert tools.register_patient(con, session, "maria garcia", "2145550101", "1985-03-12")["error"] == "already_exists"


def test_register_without_insurance_is_self_pay(con, session):
    result = tools.register_patient(con, session, "Ava Stone", "+1 (214) 555-0777", "2001-04-04", "  ")
    assert result["patient"]["insurance_name"] is None
    assert count(con, "SELECT count(*) FROM patients WHERE phone = '2145550777'") == 1


def test_consecutive_slots_are_back_to_back(con, session):
    options = tools.find_available_slots(con, session, "2026-10-26", "2026-10-31", consecutive=3)["options"]
    assert options
    for opt in options:
        hours = [datetime.fromisoformat(con.execute("SELECT starts_at FROM slots WHERE id = ?", (sid,)).fetchone()[0]).hour
                 for sid in opt["slot_ids"]]
        assert hours == [hours[0], hours[0] + 1, hours[0] + 2]


def test_never_offers_past_slots_or_sundays(con, session):
    options = tools.find_available_slots(con, session, "2026-10-01", "2026-10-19", limit=10)["options"]
    assert options
    assert all(o["starts_at"] > NOW.isoformat() for o in options)
    assert all(datetime.fromisoformat(o["starts_at"]).weekday() != 6 for o in options)


def test_options_are_spread_across_days(con, session):
    options = tools.find_available_slots(con, session, "2026-10-26", "2026-10-31")["options"]
    assert len({o["starts_at"][:10] for o in options}) > 1


def test_patient_with_two_appointments_sees_both(con, session):
    assert len(verify(con, session, "James Carter", "1968-11-02")["upcoming_appointments"]) == 2


def test_cancelled_and_past_appointments_are_not_upcoming(con, session):
    assert verify(con, session, "Linda Carter", "1970-04-19")["upcoming_appointments"] == []
    assert verify(con, session, "Dorothy Williams", "1942-08-25")["upcoming_appointments"] == []


def test_cannot_reschedule_a_past_appointment(con, session):
    verify(con, session, "Dorothy Williams", "1942-08-25")
    past = con.execute("SELECT a.id FROM appointments a JOIN patients p ON p.id = a.patient_id "
                       "WHERE p.full_name = 'Dorothy Williams'").fetchone()["id"]
    assert tools.reschedule_appointment(con, session, past, open_slot(con, session))["error"] == "not_found"


def test_reschedule_frees_the_old_slot(con, session):
    appt = verify(con, session, "Michael Smith", "1985-09-17")["upcoming_appointments"][0]
    old_slot = slot_at(con, appt["starts_at"])
    new_slot = open_slot(con, session)

    assert tools.reschedule_appointment(con, session, appt["appointment_id"], new_slot)["ok"]
    assert count(con, "SELECT count(*) FROM appointments WHERE slot_id = ? AND status = 'booked'", old_slot) == 0


def test_cancelled_slot_can_be_booked_again(con, session):
    appt = verify(con, session, "Harold Kim", "1959-02-14")["upcoming_appointments"][0]   # later today
    assert tools.cancel_appointment(con, session, appt["appointment_id"])["ok"]

    tyler = verify(con, session, "Tyler Brooks", "1999-07-21")["patient"]["patient_id"]
    result = tools.book_appointments(con, session, [
        {"patient_id": tyler, "slot_id": slot_at(con, appt["starts_at"]), "type": "checkup"}])
    assert result["ok"]


def test_emergency_needs_a_summary(con, session):
    tyler = verify(con, session, "Tyler Brooks", "1999-07-21")["patient"]["patient_id"]
    result = tools.book_appointments(con, session, [{"patient_id": tyler, "slot_id": open_slot(con, session), "type": "emergency"}])
    assert result["error"] == "missing_emergency_summary"


def test_notify_staff_for_escalation(con, session):
    result = tools.notify_staff(con, session, "escalation", "Wants to dispute a bill", patient_id=999)
    assert result["ok"]
    row = con.execute("SELECT patient_id, conversation_id FROM staff_notifications WHERE id = ?",
                      (result["notification_id"],)).fetchone()
    assert row["patient_id"] is None, "unverified patient id must not be attached"
    assert row["conversation_id"] == "test-convo"


def test_parent_gets_access_to_their_children_only(con, session):
    family = verify(con, session, "Maria Garcia", "1985-03-12")["family_on_file"]
    assert sorted(c["full_name"] for c in family) == ["Lucas Garcia", "Mateo Garcia", "Sofia Garcia"]
    assert all(c["patient_id"] in session.verified_patient_ids for c in family)


def test_child_does_not_unlock_parent(con, session):
    assert verify(con, session, "Sofia Garcia", "2017-06-30")["family_on_file"] == []
    assert len(session.verified_patient_ids) == 1


def test_adults_sharing_nothing_get_no_family(con, session):
    assert verify(con, session, "James Carter", "1968-11-02")["family_on_file"] == []


def test_verification_locks_after_three_failures(con, session):
    for dob in ("1985-01-01", "1985-01-02", "1985-01-03"):
        assert tools.verify_patient(con, session, "Maria Garcia", dob)["error"] == "not_found"
    # Even the right birthday is refused now.
    assert tools.verify_patient(con, session, "Maria Garcia", "1985-03-12")["error"] == "too_many_attempts"
    assert session.verified_patient_ids == set()


def test_calendar_extends_without_touching_bookings(con, session):
    last = con.execute("SELECT max(starts_at) FROM slots").fetchone()[0]
    booked = con.execute("SELECT count(*) FROM appointments").fetchone()[0]
    end = date.fromisoformat(last[:10]) + timedelta(days=14)
    added = tools.extend_calendar(con, TODAY, end)
    assert added > 0
    assert tools.extend_calendar(con, TODAY, end) == 0          # running again adds nothing
    assert con.execute("SELECT max(starts_at) FROM slots").fetchone()[0][:10] >= (end - timedelta(days=1)).isoformat()
    assert con.execute("SELECT count(*) FROM appointments").fetchone()[0] == booked
    assert not con.execute("SELECT 1 FROM slots WHERE strftime('%w', starts_at) = '0'").fetchone()   # no Sundays


def test_seed_always_has_two_months_to_book(con):
    last = con.execute("SELECT max(starts_at) FROM slots").fetchone()[0]
    assert date.fromisoformat(last[:10]) >= TODAY + timedelta(days=tools.CALENDAR_DAYS_AHEAD - 1)


def test_clinic_clock_ignores_server_timezone(monkeypatch):
    import os, time
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        from zoneinfo import ZoneInfo
        expected = datetime.now(ZoneInfo("America/Chicago")).replace(tzinfo=None, second=0, microsecond=0)
        assert abs((tools.clinic_now() - expected).total_seconds()) < 61
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
