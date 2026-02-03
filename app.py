import os
import re
import time
import json
import requests
from flask import Flask, request, Response

app = Flask(__name__)

# =====================
# ENV VARS YOU SET
# =====================
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()

# Forward urgent calls to this number (E.164 format). Example: +15205551234
FORWARD_NUMBER = os.environ.get("FORWARD_NUMBER", "").strip()

# Optional: where to send lead JSON (Zapier/n8n/Make/your API). Leave blank if not ready.
LEAD_WEBHOOK_URL = os.environ.get("LEAD_WEBHOOK_URL", "").strip()

SITE_CITY = os.environ.get("SITE_CITY", "Phoenix, AZ").strip()
SITE_NICHE = os.environ.get("SITE_NICHE", "Water Damage").strip()


# =====================
# Helpers
# =====================
def twiml(xml: str) -> Response:
    return Response(xml, mimetype="text/xml")


def post_lead(payload: dict) -> bool:
    """Send lead to your pipeline. Safe if LEAD_WEBHOOK_URL is empty."""
    if not LEAD_WEBHOOK_URL:
        return False
    try:
        r = requests.post(LEAD_WEBHOOK_URL, json=payload, timeout=10)
        return 200 <= r.status_code < 300
    except Exception:
        return False


def heuristic_extract(transcript: str) -> dict:
    urgent = bool(
        re.search(
            r"\b(flood|flooding|burst|gushing|overflow|standing water|water is everywhere|still leaking|ceiling leak)\b",
            transcript,
            re.I,
        )
    )
    return {
        "urgent": urgent,
        "name": "",
        "callback_phone": "",
        "address": "",
        "issue": transcript[:400],
        "summary": transcript[:220],
    }


def openai_extract(transcript: str) -> dict:
    """
    Optional structured extraction + urgency.
    If OPENAI_API_KEY missing or fails, fallback to heuristic.
    """
    if not OPENAI_API_KEY:
        return heuristic_extract(transcript)

    try:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }

        system = (
            "You are an intake receptionist for emergency water damage services. "
            "Extract fields from the transcript. Return ONLY valid JSON with keys: "
            "urgent(boolean), name(string), callback_phone(string), address(string), "
            "issue(string), summary(string). "
            "urgent=true if active flooding, burst pipe, water still flowing, ceiling collapse, "
            "or anything needing immediate dispatch."
        )

        body = {
            "model": "gpt-4.1-mini",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": transcript},
            ],
            "temperature": 0.2,
        }

        r = requests.post(url, headers=headers, json=body, timeout=15)
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"].strip()

        # Parse JSON safely
        try:
            return json.loads(text)
        except Exception:
            m = re.search(r"\{.*\}", text, re.S)
            if m:
                return json.loads(m.group(0))

        return heuristic_extract(transcript)

    except Exception:
        return heuristic_extract(transcript)


# =====================
# Routes
# =====================
@app.get("/")
def ok():
    return "OK"


@app.route("/voice", methods=["GET", "POST"])
def voice_entry():
    """
    Entry point for inbound calls.
    Ask: what happened?
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
    """
    Receives issue (SpeechResult). Then asks for callback phone.
    We store 'issue' in Twilio Memory using <Parameter>.
    """
    issue = (request.values.get("SpeechResult") or "").strip()

    if not issue:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">Sorry, I didn't catch what happened.</Say>
  <Redirect method="POST">/voice</Redirect>
</Response>"""
        return twiml(xml)

    # Use <Parameter> to persist safely without query strings
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">Got it.</Say>
  <Say voice="Polly.Joanna">What is the best callback phone number?</Say>
  <Gather input="speech" speechTimeout="auto" action="/voice/phone" method="POST">
    <Parameter name="issue" value="{escape_xml(issue)}" />
    <Say voice="Polly.Joanna">Say the phone number slowly.</Say>
  </Gather>
  <Say voice="Polly.Joanna">Sorry, I didn't catch the phone number.</Say>
  <Redirect method="POST">/voice</Redirect>
</Response>"""
    return twiml(xml)


@app.route("/voice/phone", methods=["GET", "POST"])
def voice_phone():
    """
    Receives phone (SpeechResult) and also receives 'issue' via Twilio <Parameter>.
    Then asks for name. Store issue + phone via <Parameter>.
    """
    issue = (request.values.get("issue") or "").strip()
    phone_spoken = (request.values.get("SpeechResult") or "").strip()

    if not phone_spoken:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">Sorry, I didn't catch the callback number.</Say>
  <Redirect method="POST">/voice</Redirect>
</Response>"""
        return twiml(xml)

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">Thanks.</Say>
  <Say voice="Polly.Joanna">And what is your name?</Say>
  <Gather input="speech" speechTimeout="auto" action="/voice/finalize" method="POST">
    <Parameter name="issue" value="{escape_xml(issue)}" />
    <Parameter name="callback" value="{escape_xml(phone_spoken)}" />
    <Say voice="Polly.Joanna">Go ahead.</Say>
  </Gather>
  <Say voice="Polly.Joanna">Sorry, I didn't catch your name.</Say>
  <Redirect method="POST">/voice</Redirect>
</Response>"""
    return twiml(xml)


@app.route("/voice/finalize", methods=["GET", "POST"])
def voice_finalize():
    """
    Receives name (SpeechResult), and receives 'issue' + 'callback' via Twilio <Parameter>.
    Creates lead, posts it (optional), and forwards if urgent (optional).
    """
    issue = (request.values.get("issue") or "").strip()
    callback = (request.values.get("callback") or "").strip()
    name = (request.values.get("SpeechResult") or "").strip()

    call_sid = request.values.get("CallSid", "")
    from_number = request.values.get("From", "")

    transcript = (
        f"CallerName: {name}\n"
        f"Callback: {callback}\n"
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
        "phone": extracted.get("callback_phone") or callback or from_number,
        "email": None,
        "address": extracted.get("address") or "",
        "message": extracted.get("issue") or issue,
        "summary": extracted.get("summary") or "",
        "urgent": urgent,
        "callSid": call_sid,
        "from": from_number,
        "source": "phone-ai",
    }

    # Safe even if webhook not set
    post_lead(lead_payload)

    if urgent and FORWARD_NUMBER:
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">Okay. This sounds urgent. I'm connecting you now.</Say>
  <Dial>{escape_xml(FORWARD_NUMBER)}</Dial>
  <Say voice="Polly.Joanna">If the line is busy, a team member will call you back shortly.</Say>
</Response>"""
        return twiml(xml)

    xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">Thank you. A local team will call you back shortly.</Say>
  <Hangup/>
</Response>"""
    return twiml(xml)


# =====================
# XML safety
# =====================
def escape_xml(s: str) -> str:
    """
    Prevent TwiML from breaking if speech includes quotes, apostrophes, <, >, & etc.
    This is CRITICAL for Twilio stability.
    """
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
