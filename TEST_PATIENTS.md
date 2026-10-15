# Test patients

Created by `python seed.py`. Ages and appointment dates are relative to the day you run it (examples below assume Tue Oct 13, 2026). To verify someone, the bot needs **name + date of birth**, and the **phone** only when two patients share both.

## Who to use for what

| Patient | DOB | Phone | Insurance | Medical notes | Tests |
|---|---|---|---|---|---|
| Maria Garcia | 1985-03-12 | 214-555-0101 | Delta Dental | | Parent who booked the family; has an upcoming cleaning |
| Sofia Garcia | 2017-06-30 | 214-555-0101 | Delta Dental | | Child (9), shares parent's phone, booked by Maria |
| Mateo Garcia | 2020-01-08 | 214-555-0101 | Delta Dental | Dental anxiety | Child (6); anxious-patient tone |
| Lucas Garcia | ~2 years old | 214-555-0101 | Delta Dental | First visit ever | Toddler with **no** appointment: "add my youngest to the family booking" |
| James Carter | 1968-11-02 | 469-555-0142 | Aetna | Takes warfarin | **Two** upcoming appointments, so "reschedule my appointment" must ask which one |
| Linda Carter | 1970-04-19 | 469-555-0143 | Aetna | | Spouse; only a **cancelled** appointment, so she has nothing upcoming |
| Dorothy Williams | 1942-08-25 | 972-555-0177 | Humana Medicare Advantage | Hard of hearing; denture | Elderly (84); only a **past** visit, which can't be rescheduled |
| Harold Kim | 1959-02-14 | 214-555-0188 | Blue Cross Blue Shield | Heart valve: needs premedication | Has a visit **later today**: same-day cancel |
| Tyler Brooks | 1999-07-21 | 817-555-0123 | **none** | | Uninsured: self-pay / membership pitch; has an emergency booked tomorrow |
| Aaliyah Johnson | 1993-05-05 | 214-555-0166 | Texas Medicaid | Pregnant | Medicaid: does "all major insurance" cover it? (bot should say staff will confirm) |
| Bob Hendricks | 1979-12-01 | 972-555-0199 | Careington discount plan | | A discount plan, not insurance |
| Michael Smith | 1985-09-17 | 214-555-0111 | Cigna | | Same name as the next row; has an appointment |
| Michael Smith | 1992-02-03 | 214-555-0112 | MetLife | | Same name, different DOB: must **never** see the other Michael's appointment |
| David Lee | 1990-05-14 | 469-555-0120 | Guardian | | Same name **and** DOB as the next row: the bot must ask for the phone |
| David Lee | 1990-05-14 | 469-555-0121 | United Concordia | | Has an appointment; only the phone tells the two apart |
| Priya Patel | 2004-02-29 | 214-555-0129 | Cigna | | Leap-day birthday ("Feb 29 2004", "2/29/04") |
| Ethan Nguyen-Tran | turns 18 tomorrow | 817-555-0155 | Guardian | | Minor today, adult tomorrow; hyphenated surname |
| Siobhán O'Brien | 1988-10-10 | 214-555-0133 | MetLife | Latex allergy | Accent + apostrophe ("Siobhan OBrien" should still match); past emergency |
| José Muñoz-Álvarez | 1975-03-03 | 972-555-0144 | Delta Dental | Type 2 diabetes; prefers Spanish | Accents, hyphen; may write in Spanish |
| Christopher Alexander Montgomery-Fitzgerald III | 1983-07-04 | 214-555-0150 | Aetna | | Very long name with suffix; user may type "Chris Montgomery" |

Six more "background" patients exist only to fill the calendar. Don't use them in tests.

**Medical notes** are a new nullable column on `patients`, entered by staff. The bot should never ask for them or read them back. That keeps the chatbot out of health data, which is a good point for the interview. They exist so the staff page can show alerts like "needs premedication" next to an appointment.

## Calendar edge cases

- **Next Friday is fully booked**, to test "nothing open that day, here are the nearest times".
- About 30% of other future slots are taken (fixed random seed, so it's the same every run).
- Sundays don't exist in `slots`, so "this Sunday" must get "we're closed Sundays".
- Slots from the last two weeks exist, so the bot must never offer a time in the past.
- The family's slots (next Thursday 3–5pm) run right up to closing, so "add one more after them" has to go to another time.

## Also seeded

- A high-urgency emergency notification for Tyler that staff haven't acknowledged, and an acknowledged callback for Linda.
- One conversation rated thumbs-down with feedback, so the staff page isn't empty.

## Edge cases to type at the bot (no seeded patient needed)

- New patient with a **future DOB** or a 7-digit phone: must be rejected.
- New patient who is already in the system (e.g. registers as Maria Garcia, 1985-03-12): should switch to verification.
- Right name, **wrong DOB**: "I couldn't find you", with no hint that the name exists.
- "Book me, my wife and our two kids back-to-back sometime early next month."
- "Sometime after the 5th but not Wednesdays, mornings only."
- Facial swelling with trouble swallowing: must say **call 911** before anything else.

## Staff sign-in

The staff page at `/staff` needs a sign-in. Two demo accounts are created on first start:

| Username | Name | Role | Password |
|---|---|---|---|
| frontdesk | Jordan Ellis | Front desk | SmileDemo2026 |
| manager | Dana Lopez | Office manager | SmileDemo2026 |

Set `STAFF_DEFAULT_PASSWORD` before the first start to use a different password. Five wrong passwords lock the account for 15 minutes. "Mark seen" records who acknowledged each alert.
