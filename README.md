# Bright Smile Dental: AI booking assistant

A web chat that books, reschedules and cancels dental appointments, handles emergencies, and answers practice questions, plus a signed-in dashboard for the front desk. Built on Gemini with tool calling, FastAPI and SQLite.

*Bright Smile Dental is a placeholder practice. All patients are fake seed data.*

## Run it

Needs Python 3.10+ and a Gemini API key from [Google AI Studio](https://aistudio.google.com/).

```bash
uv venv --seed && source .venv/bin/activate      # or: python -m venv .venv
uv pip install -r requirements.txt                # or: pip install -r requirements.txt
export GEMINI_API_KEY=...                         # or put it in .env (see .env.example)
pytest -q                                         # 58 tests, no API key needed
uvicorn server:app --reload
```

- Patient chat: http://localhost:8000
- Front desk: http://localhost:8000/staff (sign in as `frontdesk` / `SmileDemo2026`)
- Readiness check: http://localhost:8000/api/health

The database is created and seeded on first start. Test patients are listed in [TEST_PATIENTS.md](TEST_PATIENTS.md). `RESEED=1` rebuilds it (the old file is backed up first).

## How it works

```
Browser chat ──POST /api/chat──▶ server.py ──▶ bot.py ──▶ Gemini (tool calling)
                                                 │  ▲
                                   tool calls    ▼  │ results
                                               tools.py ──▶ SQLite (dental.db)
Front desk ──/staff (signed in)──▶ server.py ──▶ alerts, upcoming visits, feedback
```

1. Each patient message goes to `bot.py`, which sends Gemini the conversation, a system prompt (practice facts, rules, the next three weeks of dates, and whether the office is open right now) and 7 tool definitions.
2. Gemini answers in text or asks for a tool: `verify_patient`, `register_patient`, `find_available_slots`, `book_appointments`, `reschedule_appointment`, `cancel_appointment`, `notify_staff`.
3. `tools.py` runs the tool against SQLite and returns the result to Gemini. This repeats, up to 5 rounds per message.
4. The page shows Gemini's reply plus cards built from the tool results (time buttons, family checklist, booking confirmation, alerts), and a "Your booking" summary.

## Key decisions

- **Gemini never owns a fact.** It turns words into tool arguments. The code decides what's open, who is verified, and what's allowed. Gemini can't double-book (a unique index in the database prevents it, even under a race) or invent a time (the UI only shows times returned by the database).
- **Identity lives on the server.** A patient can only see or change appointments for people verified in that conversation. "Not yours" and "doesn't exist" return the same answer. Three wrong birthdays lock verification for the chat.
- **Parents can book for their children.** Verifying an adult unlocks patients under 18 who share their phone. A child can't unlock the parent, and adults sharing a phone don't unlock each other.
- **Safety doesn't depend on the model.** Booking an emergency writes the staff alert in the same transaction. Messages mentioning trouble breathing or swallowing, facial swelling or uncontrolled bleeding always show a 911 banner and alert staff, even if Gemini doesn't.
- **The chat runs 24/7; office hours (Mon–Sat 8am–6pm, clinic timezone) only change what it promises.** After hours it still books, but tells emergencies that staff will see the alert when the office opens.
- **Medical history stays out of the chat.** Staff-entered notes (blood thinners, allergies) appear on the staff page only. The bot never asks for or repeats them.
- **Simple stack on purpose.** One hand-written tool loop, no agent framework; SQLite, no server database; plain HTML pages, no frontend build.

## Testing

- `pytest -q` runs 58 tests against a seeded database with a scripted stand-in for Gemini. They cover booking rules, identity and privacy, family bookings, calendar and timezone handling, model fallback, staff sign-in, and every endpoint. They check our code, not the model's judgment.
- `python evals/run_evals.py` runs 12 real conversations against Gemini (needs the key) and checks what actually happened in the database and the replies: new-patient booking, asking before booking, back-to-back family booking, ambiguous reschedule, 911 triage, emergency booking, prompt injection, privacy, "early next month", Sunday closure, Spanish, and verification lockout. Transcripts are saved to `evals/results-*.md`. Results vary run to run, so run it several times.

## Known limits of this demo

These are deliberate for a take-home and listed so nobody mistakes the demo for production:

- One schedule of 1-hour slots for the whole practice: no providers, chairs, durations, holidays or locations, and no practice-management system.
- Conversations are kept in server memory (one process). A restart forgets open chats; bookings are safe in the database.
- Patient identity is name + date of birth (+ phone). Staff sign-in is username and password with lockout, no SSO or MFA.
- Confirmation before booking is enforced by the prompt and checked by evals, not by a server-side confirm step.
- No CI, deployment config, backups or monitoring. `/api/health` is the only readiness signal.
- Model names default to Gemini's `-latest` aliases. Set `GEMINI_MODELS` to pin exact versions for a release.

## Prioritization: what I'd build next at scale

*Draft outline. Rewrite in your own words before submitting.*

At hundreds of locations and 10,000+ conversations a day:

1. **Patient safety and trust first:** clinically reviewed emergency rules, a monitored alert channel (SMS/pager) with acknowledgement and escalation, and a server-side prepare → confirm → commit step with idempotency keys so nothing is booked without the patient's explicit yes.
2. **Compliance:** PHI data-flow and vendor review (BAA with the model provider), data minimization, encryption, retention and deletion, immutable audit logs. Staff SSO, MFA and role-based access; stronger patient verification (one-time codes) and explicit guardian relationships.
3. **Real scheduling:** integrate each location's practice-management calendar; model providers, chairs, service durations, holidays and per-location timezones and hours.
4. **Scale and operations:** Postgres, durable conversation state (Redis or the database) so any server can answer, locked dependencies, migrations, CI running unit tests and evals, backups, monitoring, cost tracking per conversation.
5. **Quality loop:** run the eval suite on every prompt or model change, add adversarial and multilingual cases, and turn thumbs-down feedback into new eval scenarios.

## Project layout

| File | What it does |
|---|---|
| `server.py` | FastAPI app: pages, chat API, staff API, sign-in |
| `bot.py` | Gemini tool loop, system prompt, 911 check, UI cards |
| `tools.py` | The 7 tools and all booking rules; clinic clock and rolling calendar |
| `auth.py` | Staff accounts (scrypt hashes), sessions, lockout |
| `schema.sql` | Database schema (safe to re-run) |
| `seed.py` | Test patients, slots and appointments |
| `static/` | Chat page, staff dashboard, sign-in page |
| `test_*.py` | Unit and API tests |
| `evals/run_evals.py` | Real-model behavior evals |
