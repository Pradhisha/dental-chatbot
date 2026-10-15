"""The AI layer: Gemini + the 7 booking tools, in a hand-written tool loop.

One call to Bot.reply() handles one patient message:
  1. add the message to the conversation history
  2. ask Gemini (with the tools declared)
  3. if Gemini asks for tools, run them from tools.py and send the results back
  4. repeat up to MAX_TOOL_ROUNDS, then return the text plus UI data
     (cards and the booking summary) built from the tool results

Run in the terminal:  python bot.py
"""

import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from google import genai
from google.genai import types

import tools

log = logging.getLogger("bot")

# Tried in order; the second is the fallback. Override with GEMINI_MODELS="model-a,model-b" to pin
# exact versions for a release, since the "-latest" aliases move when Google ships a new model.
MODELS = [m.strip() for m in os.environ.get("GEMINI_MODELS", "gemini-flash-latest,gemini-flash-lite-latest").split(",") if m.strip()]
MAX_HISTORY_TURNS = 20      # patient messages kept in what's sent to Gemini; older ones are dropped
TIMEOUT_MS = 30_000
RETRIES_PER_MODEL = 2
QUOTA_COOLDOWN_S = 60       # how long to skip a model that hit its quota, if Google doesn't say
MAX_TOOL_ROUNDS = 5

PRACTICE = {
    "name": "Bright Smile Dental",
    "phone": "(214) 555-0100",
    "address": "1200 Elm Street, Dallas, TX 75201",
}

FALLBACK_REPLY = (f"Sorry, I'm having trouble right now. Please try again in a minute, "
                  f"or call us at {PRACTICE['phone']}.")
LOOP_CAP_REPLY = (f"I'm having trouble finishing this request. Please call us at {PRACTICE['phone']} "
                  f"and the front desk will sort it out.")


# Phrases that mean "call 911", checked in code so the warning never depends on the model.
RED_FLAGS = re.compile(
    r"can'?t breathe|cannot breathe|trouble breathing|hard to breathe|short(ness)? of breath|"
    r"can'?t swallow|cannot swallow|trouble swallowing|hard to swallow|"
    r"swell\w* (?:\w+ ){0,3}(?:face|eye|neck|throat|jaw)|(?:face|eye|neck|throat|jaw) (?:\w+ ){0,2}swell|"
    r"bleeding (?:\w+ ){0,2}(?:won'?t|will not|doesn'?t|does not|can'?t) stop|"
    r"(?:won'?t|can'?t|doesn'?t) stop bleeding|heavy bleeding|"
    r"broken jaw|jaw (?:is )?broken|car (?:accident|crash)|passed out|unconscious|chest pain",
    re.I)


def is_red_flag(text: str) -> bool:
    return bool(RED_FLAGS.search(text))


# ---------- tool declarations (what Gemini sees) ----------

S, I, O, A = "STRING", "INTEGER", "OBJECT", "ARRAY"

TOOL_DECLARATIONS = [
    {"name": "verify_patient",
     "description": "Look up an existing patient by full name and date of birth. Also returns their upcoming "
                    "appointments and any children on file who share their phone (the parent may book for them). "
                    "If the result is need_phone, ask for the phone number and call again.",
     "parameters": {"type": O, "required": ["full_name", "date_of_birth"], "properties": {
         "full_name": {"type": S},
         "date_of_birth": {"type": S, "description": "YYYY-MM-DD"},
         "phone": {"type": S, "description": "Only when the previous call returned need_phone"}}}},
    {"name": "register_patient",
     "description": "Create a new patient. Collect full name, phone, date of birth and insurance first. "
                    "insurance_name is empty for self-pay.",
     "parameters": {"type": O, "required": ["full_name", "phone", "date_of_birth"], "properties": {
         "full_name": {"type": S}, "phone": {"type": S},
         "date_of_birth": {"type": S, "description": "YYYY-MM-DD"},
         "insurance_name": {"type": S, "description": "Insurance company, or empty if none"}}}},
    {"name": "find_available_slots",
     "description": "Find open 1-hour appointment times in a date range. Use consecutive=N to find N "
                    "back-to-back times for a family. If nothing is open, the result has next_available.",
     "parameters": {"type": O, "required": ["start_date", "end_date"], "properties": {
         "start_date": {"type": S, "description": "YYYY-MM-DD"},
         "end_date": {"type": S, "description": "YYYY-MM-DD, inclusive"},
         "time_of_day": {"type": S, "enum": ["morning", "afternoon", "evening", "any"]},
         "consecutive": {"type": I, "description": "1-4, default 1"},
         "limit": {"type": I, "description": "default 5"}}}},
    {"name": "book_appointments",
     "description": "Book one or more appointments at once (all succeed or none do). Only use slot_ids from "
                    "find_available_slots, and only after the patient confirmed. For an emergency, include "
                    "emergency_summary; staff are alerted automatically.",
     "parameters": {"type": O, "required": ["bookings"], "properties": {
         "bookings": {"type": A, "items": {"type": O, "required": ["patient_id", "slot_id", "type"], "properties": {
             "patient_id": {"type": I}, "slot_id": {"type": I},
             "type": {"type": S, "enum": ["cleaning", "checkup", "emergency"]},
             "emergency_summary": {"type": S}}}}}}},
    {"name": "reschedule_appointment",
     "description": "Move an upcoming appointment to a new slot, after the patient confirmed.",
     "parameters": {"type": O, "required": ["appointment_id", "new_slot_id"], "properties": {
         "appointment_id": {"type": I}, "new_slot_id": {"type": I}}}},
    {"name": "cancel_appointment",
     "description": "Cancel an upcoming appointment, after the patient confirmed.",
     "parameters": {"type": O, "required": ["appointment_id"], "properties": {
         "appointment_id": {"type": I}}}},
    {"name": "notify_staff",
     "description": "Hand something to the front desk: billing disputes, complaints, clinical questions, "
                    "'talk to a person', or an emergency where the patient doesn't want to book.",
     "parameters": {"type": O, "required": ["kind", "summary"], "properties": {
         "kind": {"type": S, "enum": ["emergency", "escalation", "callback"]},
         "summary": {"type": S, "description": "One line for staff"},
         "urgency": {"type": S, "enum": ["normal", "high"]},
         "patient_id": {"type": I}}}},
]

TOOL_FUNCTIONS = {
    "verify_patient": tools.verify_patient,
    "register_patient": tools.register_patient,
    "find_available_slots": tools.find_available_slots,
    "book_appointments": tools.book_appointments,
    "reschedule_appointment": tools.reschedule_appointment,
    "cancel_appointment": tools.cancel_appointment,
    "notify_staff": tools.notify_staff,
}


# ---------- system prompt ----------

def calendar_block(today: date) -> str:
    """Spell out the next few weeks so the model never does date arithmetic on its own."""
    monday = today - timedelta(days=today.weekday())
    def week(offset):
        start = monday + timedelta(weeks=offset)
        return f"{start.isoformat()} to {(start + timedelta(days=5)).isoformat()}"
    first_next = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    days = [today + timedelta(days=i) for i in range(22)]
    listing = ", ".join(f"{d:%a} {d.isoformat()}" for d in days)
    return (f"Today is {today:%A} {today.isoformat()}.\n"
            f"This week (Mon-Sat): {week(0)}. Next week: {week(1)}. Week after: {week(2)}.\n"
            f"'Later this week' = the rest of this week. 'Later next week' = Thu-Sat of next week.\n"
            f"'Early next month' = {first_next.isoformat()} to {(first_next + timedelta(days=9)).isoformat()}.\n"
            f"Next 3 weeks: {listing}.")


def office_status(now: datetime) -> str:
    """Whether the office is staffed right now, and when it next opens. The chat itself runs 24/7."""
    if now.weekday() != 6 and 8 <= now.hour < 18:
        return f"It is now {now:%A %-I:%M %p}. The office is open, so staff see alerts right away."
    nxt = now if now.hour < 8 and now.weekday() != 6 else now + timedelta(days=1)
    while nxt.weekday() == 6:
        nxt += timedelta(days=1)
    return (f"It is now {now:%A %-I:%M %p}. The office is CLOSED; staff will see alerts when it opens "
            f"{nxt:%A} at 8 AM. You can still book, change and cancel appointments now.")


def system_prompt(now: datetime) -> str:
    today = now.date()
    return f"""You are the online assistant for {PRACTICE['name']}, a dental practice. You help patients book,
reschedule and cancel appointments and answer questions about the practice. Be warm, brief and clear.
Many patients are older, anxious or in pain: use plain words, ask for one or two things at a time,
and mirror their tone (short with short replies, gentle with worried ones).
Reply in the language the patient writes in.

PRACTICE FACTS (only these; never invent others such as prices or dentist names)
- Hours: Monday to Saturday, 8am to 6pm. Closed Sundays. Appointments are 1 hour.
- Phone {PRACTICE['phone']}. Address {PRACTICE['address']}.
- Insurance: we accept all major dental insurance. For plans like Medicaid or discount plans, say the
  team will confirm coverage before the visit.
- No insurance: we offer self-pay pricing, an in-house membership plan, and financing. Mention these kindly.
- Appointment types: cleaning, checkup, emergency.

RIGHT NOW
{office_status(now)}
The chat is available 24/7; only the office has hours.

DATES
{calendar_block(today)}
Turn phrases like "later next week" into start_date/end_date yourself using the lines above.

RULES
1. Existing patients: verify with full name + date of birth before showing or changing anything.
   New patients: collect full name, phone, date of birth and insurance (or none), then register.
   Ask for at most two things per message, never a numbered form. A natural order: name and date of birth,
   then phone, then insurance, then visit type. Accept dates in any format ("March 3 1990", "3/3/90")
   and convert them yourself; never ask the patient to use a specific format.
2. Never invent an appointment time. Only offer times returned by find_available_slots, and use their labels.
3. Before booking, rescheduling or cancelling, repeat the details and get a clear yes.
4. Families: verify the parent; their children on file can be booked too. Use consecutive=N for back-to-back.
5. Emergencies: if there is facial swelling that affects breathing or swallowing, heavy bleeding that won't stop,
   or a serious injury, tell them to call 911 now, before anything else. Otherwise get a one-line description,
   book the soonest slot as type emergency with emergency_summary (staff are alerted automatically),
   and tell the patient the team has been notified. If they don't want to book, use notify_staff.
   When the office is closed, say honestly that staff will see it when the office opens, and that for severe
   pain or swelling that can't wait they should go to an urgent care or emergency room.
6. Don't ask for or discuss medical history, and don't diagnose. For clinical questions, offer a staff callback.
7. If a tool returns ok=false, read its message, fix the problem or explain it simply. Never show ids or codes.
8. When the patient has a small set of obvious answers, end your message with one line in exactly this form:
   [[choices: Option one | Option two]]
   Use it for confirmations, e.g. [[choices: Yes, book it | Change something]].
   Don't list appointment times this way; the app shows them as buttons automatically.
"""


# ---------- conversation state ----------

@dataclass
class Conversation:
    id: str
    session: tools.Session
    history: list = field(default_factory=list)          # types.Content, sent to Gemini each turn
    patients: dict = field(default_factory=dict)         # patient_id -> {"full_name", "age"?, "role"}
    done: list = field(default_factory=list)             # what was booked/moved/cancelled, for the summary
    staff_alerted: bool = False
    red_flag: bool = False                               # this turn's message mentioned a 911 symptom
    lock: threading.Lock = field(default_factory=threading.Lock)


def is_quota_error(e) -> bool:
    """429 RESOURCE_EXHAUSTED: the API key used up its requests for this model (free tier: per day)."""
    text = str(e)
    return getattr(e, "code", None) == 429 or "RESOURCE_EXHAUSTED" in text or text.startswith("429")


def quota_retry_seconds(e) -> float:
    """Gemini says when to retry, e.g. 'retryDelay': '68502s'. Fall back to a short cooldown."""
    m = re.search(r"retryDelay['\"]?:\s*['\"](\d+(?:\.\d+)?)s", str(e))
    return float(m.group(1)) if m else QUOTA_COOLDOWN_S


# ---------- the bot ----------

class Bot:
    def __init__(self, con: sqlite3.Connection, client=None, models=MODELS, db_lock=None):
        self.con = con
        self.client = client or genai.Client(http_options=types.HttpOptions(timeout=TIMEOUT_MS))
        self.models = models
        self.db_lock = db_lock or threading.Lock()
        self.quota_out_until = {}   # model -> time.monotonic() when it may be tried again

    def new_conversation(self, conversation_id=None, fixed_now=None) -> Conversation:
        cid = conversation_id or str(uuid.uuid4())
        return Conversation(id=cid, session=tools.Session(conversation_id=cid, fixed_now=fixed_now))

    def reply(self, conv: Conversation, user_text: str) -> dict:
        with conv.lock:
            return self._reply(conv, user_text)

    def _reply(self, conv, user_text):
        user_text = (user_text or "").strip()[:2000]
        if not user_text:
            return self._result(conv, "Could you type your question?", [])
        self._save(conv, "user", user_text)
        conv.red_flag = is_red_flag(user_text)
        if conv.red_flag:
            self._alert_staff_red_flag(conv, user_text)

        trim_history(conv.history)
        turn_start = len(conv.history)
        conv.history.append(types.Content(role="user", parts=[types.Part(text=user_text)]))
        config = types.GenerateContentConfig(
            system_instruction=system_prompt(conv.session.now()),
            tools=[types.Tool(function_declarations=TOOL_DECLARATIONS)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=0.3,
        )
        events = []   # (tool name, args, result) for this turn

        for _ in range(MAX_TOOL_ROUNDS):
            response = self._generate(conv.history, config)
            if response is None:
                del conv.history[turn_start:]          # forget the failed turn so the next one starts clean
                return self._finish(conv, FALLBACK_REPLY, events)

            calls = response.function_calls or []
            if not calls:
                text = (response.text or "").strip() or FALLBACK_REPLY
                conv.history.append(types.Content(role="model", parts=[types.Part(text=text)]))
                return self._finish(conv, text, events)

            conv.history.append(response.candidates[0].content)
            parts = []
            for call in calls:
                args = dict(call.args or {})
                result = self._run_tool(conv, call.name, args)
                events.append((call.name, args, result))
                parts.append(types.Part.from_function_response(name=call.name, response=result))
            conv.history.append(types.Content(role="user", parts=parts))

        return self._finish(conv, LOOP_CAP_REPLY, events)

    # --- helpers ---

    def _generate(self, history, config):
        for model in self.models:
            if time.monotonic() < self.quota_out_until.get(model, 0):
                continue                                    # out of quota: go straight to the next model
            for attempt in range(RETRIES_PER_MODEL):
                try:
                    return self.client.models.generate_content(model=model, contents=history, config=config)
                except Exception as e:  # overloaded, timeout, network, quota
                    log.warning("Gemini %s attempt %d failed: %s", model, attempt + 1, e)
                    if is_quota_error(e):
                        # Retrying won't help until the quota resets, so skip this model until then.
                        self.quota_out_until[model] = time.monotonic() + quota_retry_seconds(e)
                        break
                    time.sleep(1 + attempt)
        return None

    def _run_tool(self, conv, name, args):
        fn = TOOL_FUNCTIONS.get(name)
        if fn is None:
            return {"ok": False, "error": "unknown_tool", "message": f"There is no tool called {name}."}
        try:
            with self.db_lock:
                result = fn(self.con, conv.session, **args)
        except TypeError as e:
            result = {"ok": False, "error": "bad_arguments", "message": str(e)}
        except Exception:
            log.exception("tool %s crashed", name)
            result = {"ok": False, "error": "internal_error", "message": "Something went wrong. Offer a staff callback."}
        self._save(conv, "tool", json.dumps({"tool": name, "args": args, "result": result}, default=str))
        self._track(conv, name, result)
        return result

    def _track(self, conv, name, result):
        """Remember what happened, for the booking summary panel."""
        if not result.get("ok"):
            return
        if name in ("verify_patient", "register_patient"):
            p = result["patient"]
            conv.patients[p["patient_id"]] = {"full_name": p["full_name"],
                                              "role": "new" if name == "register_patient" else "patient"}
            for child in result.get("family_on_file", []):
                conv.patients.setdefault(child["patient_id"], {"full_name": child["full_name"],
                                                               "age": child["age"], "role": "family"})
        elif name == "book_appointments":
            for a in result["appointments"]:
                conv.done.append({"action": "Booked", "who": a["full_name"], "type": a["type"], "when": a["label"]})
            conv.staff_alerted |= result.get("staff_alerted", False)
        elif name == "reschedule_appointment":
            conv.done.append({"action": "Moved", "when": result["new_label"], "from": result["old_label"]})
        elif name == "cancel_appointment":
            conv.done.append({"action": "Cancelled", "when": result["cancelled_label"]})
        elif name == "notify_staff":
            conv.staff_alerted = True

    def _finish(self, conv, text, events):
        self._save(conv, "assistant", text)
        return self._result(conv, text, events)

    def _result(self, conv, text, events):
        text, choices = split_choices(text)
        cards = ([{"kind": "call911"}] if conv.red_flag else []) + build_cards(events)
        return {"reply": text, "choices": choices, "cards": cards, "summary": build_summary(conv)}

    def _alert_staff_red_flag(self, conv, user_text):
        patient_id = next(iter(conv.session.verified_patient_ids), None)
        with self.db_lock:
            tools.notify_staff(self.con, conv.session, "emergency",
                               f"Possible medical emergency described in chat: \"{user_text[:200]}\"",
                               urgency="high", patient_id=patient_id)
        conv.staff_alerted = True

    def _save(self, conv, role, content):
        with self.db_lock, self.con:
            self.con.execute("INSERT OR IGNORE INTO conversations (id) VALUES (?)", (conv.id,))
            self.con.execute("INSERT INTO messages (conversation_id, role, content) VALUES (?,?,?)",
                             (conv.id, role, content))


def trim_history(history, keep=MAX_HISTORY_TURNS):
    """Drop the oldest turns so at most keep-1 earlier patient messages remain (the new one makes keep).
    Cuts only at the start of a patient message, so a tool call is never separated from its result.
    Who's verified lives in the session, not the history, so nothing security-related is lost."""
    starts = [i for i, c in enumerate(history)
              if c.role == "user" and any(getattr(p, "text", None) for p in (c.parts or []))]
    if len(starts) > keep - 1:
        del history[:starts[len(starts) - (keep - 1)]]


# ---------- UI data built from tool results (not from the model's words) ----------

CHOICES_RE = re.compile(r"\[\[choices:(.*?)\]\]\s*$", re.S)


def split_choices(text):
    m = CHOICES_RE.search(text)
    if not m:
        return text, []
    choices = [c.strip() for c in m.group(1).split("|") if c.strip()][:4]
    return text[:m.start()].rstrip(), choices


def build_cards(events):
    cards = []
    for name, args, result in events:
        if name == "find_available_slots" and result.get("ok"):
            options = result["options"] or result.get("next_available", [])
            if options:
                cards.append({"kind": "slots",
                              "title": "Open times" if result["options"] else "Nothing open then. Next openings",
                              "consecutive": args.get("consecutive", 1) or 1,
                              "options": options})
        elif name == "verify_patient" and result.get("ok") and result.get("family_on_file"):
            cards.append({"kind": "family", "parent": result["patient"]["full_name"],
                          "members": [{"full_name": c["full_name"], "age": c["age"]} for c in result["family_on_file"]]})
        elif name == "book_appointments" and result.get("ok"):
            cards.append({"kind": "booked", "appointments": result["appointments"]})
            if result.get("staff_alerted"):
                cards.append({"kind": "alert"})
        elif name == "notify_staff" and result.get("ok") and args.get("kind") == "emergency":
            cards.append({"kind": "alert"})
    # Only the latest set of times matters if the model searched more than once.
    slot_cards = [c for c in cards if c["kind"] == "slots"]
    return [c for c in cards if c["kind"] != "slots"] + slot_cards[-1:]


def build_summary(conv):
    patients = [{"full_name": p["full_name"], "age": p.get("age"), "role": p["role"]} for p in conv.patients.values()]
    return {"patients": patients, "done": conv.done, "staff_alerted": conv.staff_alerted}


# ---------- terminal chat ----------

if __name__ == "__main__":
    db = Path(__file__).with_name("dental.db")
    if not db.exists():
        from seed import seed
        seed(db, tools.clinic_now().date(), tools.clinic_now()).close()
    bot = Bot(tools.connect(db))
    conv = bot.new_conversation()
    print(f"{PRACTICE['name']} assistant. Type 'quit' to exit.\n")
    while True:
        try:
            line = input("You: ")
        except (EOFError, KeyboardInterrupt):
            break
        if line.strip().lower() in ("quit", "exit"):
            break
        out = bot.reply(conv, line)
        print("Bot:", out["reply"])
        for choice in out["choices"]:
            print("   >", choice)
        for card in out["cards"]:
            if card["kind"] == "slots":
                for o in card["options"]:
                    print("   *", o["label"])
        print()
