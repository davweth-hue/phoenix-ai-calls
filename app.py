import os
import re
import time
import json
import requests
from urllib.parse import quote
from flask import Flask, request, Response

app = Flask(__name__)

# =====================
# ENV VARS YOU SET
# =====================
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()

# Where to send calls if urgent (your buyer, or your own phone while testing)
FORWARD_NUMBER = os.environ.get("FORWARD_NUMBER", "").strip()  # e.g. +17025551234 (NOT your Twilio number)

# Your existing lead endpoint (optional). If blank, we'll just skip posting.
LEAD_WEBHOOK_URL = os.environ.get("LEAD_WEBHOOK_URL", "").strip()

SITE_CITY = os.environ.get("SITE_CITY", "Phoenix, AZ").strip()
SITE_NICHE = os.environ.get("SITE_NICHE", "Water Damage").strip()


# =====================
# Helpers
# =====================
def twiml(xml: str) -> Response:
    return Response(xml, mimetype="text/xml")


def post_lead(payload: dict) -> bool:
    """Send lead to your existing pipeline (Zapier/n8n/Make/your /api/lead)."""
    if not LEAD_WEBHOOK_URL:
        return False
    try:
        r = requests.post(LEAD_WEBHOOK_URL, json=payload, timeout=10)
        return 200 <= r.status_code < 300
    except Exception:
        return False


def openai_extract(transcript: str) -> dict:
    """
    Uses OpenAI to extract structured fields and urgency.
    If OPENAI_API_KEY is missing, falls back to a basic keyword heuristic.
    """
    if not OPENAI_API_KEY:
        urgent = bool(re.search(r"\b(flood|flooding|burst|gushing|overflow|standing water|water is everywhere)\b", transcript, re.I))
        return {
            "urgent": urgent,
            "summary": transcript[:220],
            "name": "",
            "callback_phone": "",
            "address": "",
            "issue": transcript[:300],
        }

    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    system = (
        "You are an intake receptionist for emergency water damage in Phoenix. "
        "Extract fields from the caller transcript. "
        "Return ONLY valid JSON with keys: urgent(boolean), name(string), callback_phone(string), "
        "address(string), issue(string), summary(string). "
        "Urgent=true if active flooding, burst pipe, water still flowing, ceiling collapse, "
        "or anything requiring immediate dispatch."
    )

    body = {
        "model": "gpt-4.1-mini",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Transcript:\n{transcript}"},
        ],
        "temperature": 0.2,
    }

    r = requests.post(url, headers=headers, json=body, timeout=15)
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"].strip()

    # Best effort JSON parsing
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        return {
            "urgent": False,
            "summary": transcript[:220],
            "name": "",
            "callback_phone": "",
            "address": "",
            "issue": transcript[:300],
        }


# =====================
# Routes
# =====================

@app.get("/")
def ok():
    return "OK"


@app.route("/voice", methods=["GET", "POST"])
def voice_entry():
    """
    Twilio hits this when a call comes in.
    We ask for a short description (speech input).
    """
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">Thanks for calling. This call may be recorded to help route your request.</Say>
  <Say voice="Polly.Joanna">In one sentence, tell me what happened with the water damage.</Say>
  <Gather input="speech" speechTimeout="auto" action="/voice/issue" method="POST">
    <Say voice="Polly.Joanna">Go ahead.</Say>
  </Gather>
  <Say voice="Polly.Joanna">Sorry, I didn't catch that.</Say>
  <Redirect method="POST">/voice</Redirect>
</Response>"""
    return twiml(xml)


@app.route("/voice/issue", methods=["GET", "POST"])
def voice_issue():
    issue = (request.values.get("SpeechResult") or "").strip()

    if not issue:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">Sorry, I didn't catch that.</Say>
  <Redirect method="POST">/voice</Redirect>
</Response>"""
        return twiml(xml)

    issue_q = quote(issue)

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">Got it.</Say>
  <Say voice="Polly.Joanna">What is the best callback phone number?</Say>
  <Gather input="speech" speechTimeout="auto" action="/voice/phone?issue={issue_q}" method="POST">
    <Say voice="Polly.Joanna">Say the phone number slowly.</Say>
  </Gather>
  <Say voice="Polly.Joanna">Sorry, I didn't catch the phone number.</Say>
  <Redirect method="POST">/voice</Redirect>
</Response>"""
    return twiml(xml)


@app.route("/voice/phone", methods=["GET", "POST"])
def voice_phone():
    issue = (request.values.get("issue") or "").strip()
    phone_spoken = (request.values.get("SpeechResult") or "").strip()

    if not phone_spoken:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">Sorry, I didn't catch the phone number.</Say>
  <Redirect method="POST">/voice</Redirect>
</Response>"""
        return twiml(xml)

    issue_q = quote(issue)
    phone_q = quote(phone_spoken)

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">Thanks.</Say>
  <Say voice="Polly.Joanna">And what is your name?</Say>
  <Gather input="speech" speechTimeout="auto" action="/voice/finalize?issue={issue_q}&phone={phone_q}" method="POST">
    <Say voice="Polly.Joanna">Go ahead.</Say>
  </Gather>
  <Say voice="Polly.Joanna">Sorry, I didn't catch that.</Say>
  <Redirect method="POST">/voice</Redirect>
</Response>"""
    return twiml(xml)


@app.route("/voice/finalize", methods=["GET", "POST"])
def voice_finalize():
    issue = (request.values.get("issue") or "").strip()
    phone_spoken = (request.values.get("phone") or "").strip()
    name = (request.values.get("SpeechResult") or "").strip()

    call_sid = request.values.get("CallSid", "")
    from_number = request.values.get("From", "")

    transcript = (
        f"CallerName: {name}\n"
        f"Callback: {phone_spoken}\n"
        f"Issue: {issue}\n"
        f"From: {from_number}\n"
        f"CallSid: {call_sid}"
    )

    extracted = openai_extract(transcript)
    urgent = bool(extracted.get("urgent"))

    lead_payload = {
        "ts": int(time.time()),
        "niche": SITE_NICHE,
        "city": SITE_CITY,
        "name": extracted.get("name") or name,
        "phone": extracted.get("callback_phone") or phone_spoken or from_number,
        "email": None,
        "address": extracted.get("address") or "",
        "message": extracted.get("issue") or issue,
        "summary": extracted.get("summary") or "",
        "urgent": urgent,
        "callSid": call_sid,
        "from": from_number,
        "source": "phone-ai",
    }

    post_lead(lead_payload)

    # If urgent, warm transfer
    if urgent and FORWARD_NUMBER:
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">Okay. This sounds urgent. I'm connecting you now.</Say>
  <Dial>{FORWARD_NUMBER}</Dial>
  <Say voice="Polly.Joanna">If the line is busy, a team member will call you back shortly.</Say>
</Response>"""
        return twiml(xml)

    # Otherwise, confirm and end
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">Thank you. A local team will call you back shortly.</Say>
  <Hangup/>
</Response>"""
    return twiml(xml)
