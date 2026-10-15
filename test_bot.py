"""Tests for the AI layer and the web API, with a scripted fake Gemini.

No API key or network needed: FakeClient replays the responses each test lists,
so these check our loop, fallbacks and UI data, not Gemini's judgement.
"""

from datetime import date, datetime

import pytest
from google.genai import types

import bot as botmod
import tools
from seed import seed

TODAY = date(2026, 10, 13)
NOW = datetime(2026, 10, 13, 9, 0)


def call(name, **args):
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(
        role="model", parts=[types.Part.from_function_call(name=name, args=args)]))])


def say(text):
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(
        role="model", parts=[types.Part(text=text)]))])


class FakeClient:
    """Stands in for genai.Client. Each item is a response, or an exception to raise."""
    def __init__(self, *script):
        self.script = list(script)
        self.models_used = []
        self.models = self

    def generate_content(self, model, contents, config):
        self.models_used.append(model)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(botmod.time, "sleep", lambda s: None)


@pytest.fixture
def con(tmp_path):
    seed(tmp_path / "t.db", TODAY, NOW).close()
    c = tools.connect(tmp_path / "t.db")
    yield c
    c.close()


def make(con, *script):
    client = FakeClient(*script)
    b = botmod.Bot(con, client=client)
    return b, b.new_conversation("test-conversation", fixed_now=NOW), client


def test_family_booking_flow_builds_cards_and_summary(con):
    b, conv, _ = make(con,
        call("verify_patient", full_name="Maria Garcia", date_of_birth="1985-03-12"),
        say("Thanks Maria! I see Sofia, Mateo and Lucas on your account. Who's coming?"),
        call("find_available_slots", start_date="2026-10-29", end_date="2026-10-31", consecutive=2),
        say("Here are back-to-back times."))

    first = b.reply(conv, "Maria Garcia, 3/12/1985")
    assert [c["kind"] for c in first["cards"]] == ["family"]
    assert len(first["cards"][0]["members"]) == 3
    assert first["summary"]["patients"][0]["full_name"] == "Maria Garcia"

    second = b.reply(conv, "Sofia and Mateo, later next week")
    slots = second["cards"][0]
    assert slots["kind"] == "slots" and slots["consecutive"] == 2
    assert all(len(o["slot_ids"]) == 2 for o in slots["options"])

    sofia, mateo = (p for p in conv.session.verified_patient_ids
                    if conv.patients[p]["full_name"] in ("Sofia Garcia", "Mateo Garcia"))
    ids = slots["options"][0]["slot_ids"]
    b.client = FakeClient(
        call("book_appointments", bookings=[{"patient_id": sofia, "slot_id": ids[0], "type": "checkup"},
                                            {"patient_id": mateo, "slot_id": ids[1], "type": "checkup"}]),
        say("All set! [[choices: Add another visit | I'm done]]"))
    third = b.reply(conv, "Yes, book those")
    assert third["reply"] == "All set!"
    assert third["choices"] == ["Add another visit", "I'm done"]
    assert third["cards"][0]["kind"] == "booked"
    assert len(third["summary"]["done"]) == 2


def test_emergency_booking_shows_alert(con):
    b, conv, _ = make(con,
        call("verify_patient", full_name="Tyler Brooks", date_of_birth="1999-07-21"),
        call("find_available_slots", start_date="2026-10-13", end_date="2026-10-13"),
        say("The soonest is today at 10 AM. Book it?"))
    out = b.reply(conv, "Tyler Brooks 7/21/1999, my tooth broke")
    slot = out["cards"][0]["options"][0]["slot_ids"][0]

    b.client = FakeClient(
        call("book_appointments", bookings=[{"patient_id": next(iter(conv.patients)), "slot_id": slot,
                                             "type": "emergency", "emergency_summary": "Broken front tooth"}]),
        say("You're booked and our team has been notified."))
    out = b.reply(conv, "yes")
    assert [c["kind"] for c in out["cards"]] == ["booked", "alert"]
    assert out["summary"]["staff_alerted"] is True


@pytest.mark.parametrize("text, expected", [
    ("my face is swelling and I can't swallow", True),
    ("swelling in my jaw and neck", True),
    ("the bleeding won't stop", True),
    ("I have trouble breathing", True),
    ("my tooth hurts really bad", False),
    ("I'd like a cleaning next week", False),
    ("slight swelling of my gum", False),
])
def test_red_flag_detection(text, expected):
    assert botmod.is_red_flag(text) is expected


def test_red_flag_shows_911_and_alerts_staff_even_if_model_forgets(con):
    b, conv, _ = make(con, say("I'm sorry to hear that. Would you like to book an emergency visit?"))
    before = con.execute("SELECT count(*) FROM staff_notifications WHERE urgency = 'high'").fetchone()[0]
    out = b.reply(conv, "My face is swelling up and it's hard to swallow")
    assert out["cards"][0]["kind"] == "call911"
    assert out["summary"]["staff_alerted"] is True
    assert con.execute("SELECT count(*) FROM staff_notifications WHERE urgency = 'high'").fetchone()[0] == before + 1

    b.client = FakeClient(say("Sure."))
    assert all(c["kind"] != "call911" for c in b.reply(conv, "ok thanks")["cards"])


def test_falls_back_to_second_model(con):
    b, conv, client = make(con, RuntimeError("503 overloaded"), RuntimeError("503 overloaded"), say("We're open 8 to 6."))
    out = b.reply(conv, "When are you open?")
    assert out["reply"] == "We're open 8 to 6."
    assert client.models_used == ["gemini-flash-latest", "gemini-flash-latest", "gemini-flash-lite-latest"]


QUOTA = RuntimeError("429 RESOURCE_EXHAUSTED. {'error': {'details': [{'retryDelay': '68502s'}]}}")


def test_quota_error_skips_straight_to_next_model_and_remembers(con):
    b, conv, client = make(con, QUOTA, say("Hi!"), say("Hello again."))
    assert b.reply(conv, "hello")["reply"] == "Hi!"
    assert client.models_used == ["gemini-flash-latest", "gemini-flash-lite-latest"]   # no retry on 429
    assert b.reply(conv, "hi")["reply"] == "Hello again."
    assert client.models_used[-1] == "gemini-flash-lite-latest"                     # exhausted model skipped
    assert botmod.quota_retry_seconds(QUOTA) == 68502


def test_all_models_down_gives_friendly_reply_and_clean_history(con):
    b, conv, _ = make(con, *[RuntimeError("down")] * 4)
    out = b.reply(conv, "hello")
    assert out["reply"] == botmod.FALLBACK_REPLY
    assert conv.history == []


def test_tool_loop_is_capped(con):
    b, conv, _ = make(con, *[call("find_available_slots", start_date="2026-10-20", end_date="2026-10-20")] * botmod.MAX_TOOL_ROUNDS)
    assert b.reply(conv, "any time")["reply"] == botmod.LOOP_CAP_REPLY


def test_bad_tool_arguments_go_back_to_the_model(con):
    b, conv, _ = make(con, call("cancel_appointment", appointment="12"), say("Which appointment?"))
    out = b.reply(conv, "cancel it")
    assert out["reply"] == "Which appointment?"
    response_part = conv.history[2].parts[0].function_response
    assert response_part.response["error"] == "bad_arguments"


def test_messages_are_saved(con):
    b, conv, _ = make(con, call("verify_patient", full_name="Linda Carter", date_of_birth="1970-04-19"), say("Hi Linda!"))
    b.reply(conv, "Linda Carter 4/19/1970")
    roles = [r[0] for r in con.execute("SELECT role FROM messages WHERE conversation_id = 'test-conversation' ORDER BY id")]
    assert roles == ["user", "tool", "assistant"]


def test_history_is_capped_without_splitting_tool_calls(con):
    b, conv, _ = make(con)
    b.client = FakeClient(*[call("find_available_slots", start_date="2026-10-20", end_date="2026-10-20"), say("ok")] * 30)
    for i in range(30):
        b.reply(conv, f"message {i}")
    texts = [c.parts[0].text for c in conv.history if c.role == "user" and c.parts[0].text]
    assert len(texts) == botmod.MAX_HISTORY_TURNS and texts[-1] == "message 29"
    assert conv.history[0].role == "user" and conv.history[0].parts[0].text   # starts at a patient message


def test_calendar_resolves_fuzzy_phrases():
    text = botmod.calendar_block(date(2026, 10, 14))
    assert "Today is Wednesday 2026-10-14" in text
    assert "Next week: 2026-10-19 to 2026-10-24" in text
    assert "'Early next month' = 2026-11-01 to 2026-11-10" in text


def test_office_status_knows_when_closed():
    assert "office is open" in botmod.office_status(datetime(2026, 10, 14, 10, 0))
    assert "opens Thursday" in botmod.office_status(datetime(2026, 10, 14, 19, 0))      # Wed evening
    assert "opens Monday" in botmod.office_status(datetime(2026, 10, 17, 18, 30))      # Sat after close
    assert "opens Monday" in botmod.office_status(datetime(2026, 10, 18, 12, 0))       # Sunday
    assert "opens Thursday" in botmod.office_status(datetime(2026, 10, 15, 6, 0))      # Thu early morning


def test_tool_declarations_are_valid_for_gemini():
    tool = types.Tool(function_declarations=botmod.TOOL_DECLARATIONS)
    assert {f.name for f in tool.function_declarations} == set(botmod.TOOL_FUNCTIONS)


# ---------- web API ----------

@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-used")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("RESEED", "1")
    import importlib
    import server
    server = importlib.reload(server)
    from fastapi.testclient import TestClient
    return server, TestClient(server.app)


def test_chat_feedback_and_staff_endpoints(api):
    server, client = api
    server.bot.client = FakeClient(say("Hello! [[choices: Book a visit | Ask a question]]"))
    res = client.post("/api/chat", json={"conversation_id": "browser-uuid-1", "message": "hi"})
    assert res.status_code == 200
    assert res.json()["choices"] == ["Book a visit", "Ask a question"]

    assert client.post("/api/feedback", json={"conversation_id": "browser-uuid-1", "rating": -1,
                                              "comment": "Too slow"}).status_code == 200
    assert client.post("/api/login", json={"username": "frontdesk", "password": "SmileDemo2026"}).status_code == 200
    staff = client.get("/api/staff").json()
    assert any(f["feedback"] == "Too slow" for f in staff["feedback"])
    assert staff["kpis"]["open_emergencies"] >= 1      # Tyler's seeded alert

    alert = next(a for a in staff["alerts"] if not a["acknowledged_at"])
    assert client.post(f"/api/staff/alerts/{alert['id']}/ack").status_code == 200
    assert client.post(f"/api/staff/alerts/{alert['id']}/ack").status_code == 404
    seen = next(a for a in client.get("/api/staff").json()["alerts"] if a["id"] == alert["id"])
    assert seen["acknowledged_by"] == "Jordan Ellis"


def test_pages_are_served(api):
    _, client = api
    assert "Your booking" in client.get("/").text
    assert "Staff sign-in" in client.get("/login").text


def test_staff_area_requires_sign_in(api):
    _, client = api
    assert client.get("/staff", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/api/staff").status_code == 401
    assert client.post("/api/staff/alerts/1/ack").status_code == 401

    bad = client.post("/api/login", json={"username": "frontdesk", "password": "wrong"})
    assert bad.status_code == 401 and bad.json()["detail"] == "Wrong username or password."
    assert client.post("/api/login", json={"username": "nobody", "password": "x"}).json()["detail"] == "Wrong username or password."

    assert client.post("/api/login", json={"username": "FrontDesk", "password": "SmileDemo2026"}).status_code == 200
    assert "Front desk" in client.get("/staff").text
    assert client.get("/api/me").json()["full_name"] == "Jordan Ellis"
    assert client.get("/login", follow_redirects=False).headers["location"] == "/staff"

    client.post("/api/logout")
    assert client.get("/api/staff").status_code == 401


def test_staff_counts_are_not_capped_by_the_lists(api):
    server, client = api
    with server.con:
        server.con.executemany("INSERT INTO staff_notifications (kind, urgency, summary) VALUES ('callback','normal',?)",
                               [(f"call back {i}",) for i in range(45)])
    client.post("/api/login", json={"username": "frontdesk", "password": "SmileDemo2026"})
    data = client.get("/api/staff").json()
    assert len(data["alerts"]) == 30
    assert data["kpis"]["open_alerts"] == 46        # 45 new + Tyler's seeded emergency


def test_health_reports_readiness(api):
    _, client = api
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["database"] == "ok" and res.json()["bookable_until"]
