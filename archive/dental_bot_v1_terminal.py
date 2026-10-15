# Dental practice chatbot (terminal version)
# Run:  python dental_bot.py
# Needs GEMINI_API_KEY in your environment (already set up in Workspace/config/secrets.zsh).

import time

from google import genai
from google.genai import types

# ---- Edit this block with your practice's real details ----
PRACTICE_INFO = """
Practice name: Bright Smile Dental
Address: 123 Main St, Dallas, TX 75201
Phone: (214) 555-0123
Hours: Mon-Thu 8am-5pm, Fri 8am-2pm, closed weekends
Services: cleanings and exams, fillings, crowns, root canals, extractions,
  teeth whitening, Invisalign, pediatric dentistry, emergency visits
Insurance accepted: Delta Dental, Cigna, Aetna, MetLife, Guardian
New patients: welcome; arrive 15 minutes early with ID and insurance card
Cancellation policy: please give 24 hours' notice
"""
# -----------------------------------------------------------

SYSTEM_PROMPT = f"""You are the friendly front-desk assistant for a dental practice.
Answer questions using ONLY the practice information below. If the answer isn't
there, say you're not sure and suggest calling the office.

PRACTICE INFORMATION:
{PRACTICE_INFO}

Rules:
- Keep answers short, warm, and plain-spoken (2-4 sentences usually).
- Do not diagnose conditions or recommend treatments. For symptoms, give general
  comfort tips at most and suggest booking a visit.
- If someone describes severe swelling, trouble breathing or swallowing, uncontrolled
  bleeding, or facial trauma, tell them to call 911 or go to the ER right away.
- For toothaches or broken teeth, tell them to call the office for an emergency visit.
- You cannot book appointments yourself. Give the phone number to book.
- Never ask for or store personal health details, insurance ID numbers, birth
  dates, or Social Security numbers.
- If asked about something unrelated to the dental practice, politely steer back.
"""

# Tried in order; if the first is overloaded, the next is used.
MODELS = ["gemini-flash-latest", "gemini-flash-lite-latest"]

CONFIG = types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, temperature=0.3)


def main():
    # reads GEMINI_API_KEY from the environment; give up on a call after 30s
    client = genai.Client(
        http_options=types.HttpOptions(timeout=30_000, retry_options=types.HttpRetryOptions(attempts=1))
    )
    history = []  # list of types.Content; sent every turn so the bot "remembers"

    print("Bright Smile Dental assistant (type 'quit' to exit)\n")
    while True:
        user_input = input("You: ").strip()
        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit"):
            break

        history.append(types.Content(role="user", parts=[types.Part(text=user_input)]))
        reply = None
        last_error = None
        for attempt in range(3):  # Gemini sometimes returns brief "busy" errors
            for model in MODELS:
                try:
                    response = client.models.generate_content(
                        model=model, contents=history, config=CONFIG
                    )
                    reply = response.text
                    break
                except Exception as e:
                    last_error = e
            if reply is not None:
                break
            time.sleep(2 * (attempt + 1))
        if reply is None:
            history.pop()
            print("Sorry, I couldn't reach the assistant right now:", last_error, "\n")
            continue

        history.append(types.Content(role="model", parts=[types.Part(text=reply)]))
        print("Bot:", reply, "\n")


if __name__ == "__main__":
    main()
