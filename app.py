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

# Where to send calls if urgent (your buyer, or your own phone while testing)
FORWARD_NUMBER = os.environ.get("FORWARD_NUMBER", "").strip()  # e.g. +16025550123

# Your existing lead endpoint (optional). If blank, we'll just log.
LEAD_WEBHOOK_URL = os.environ.get("LEAD_WEBHOOK_URL", "").strip()

# Optional: Your site URL for fallback/records
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
        r = requests.post(LEAD_WEBHOOK_URL, json=payload, timeout=8)
        return 200 <= r.status_code < 300
    except Exception:
        return False

def openai_extract(transcript: str) -> dict:
    """
    Uses OpenAI to extract structured fields and urgency.
    You can swap model names later if you want.
    """
    if not OPENAI_API_KEY:
        # fallback heuristic
        urgent = bool(re.search(r"\b(flood|flooding|burst|gushing|overflow|standing water)\b", transcript, re.I))
        return {
            "urgent": urgent,
            "summary": transcript[:220],
            "name": "",
            "callback_phone": "",
            "address": "",
            "issue": transcript[:300]
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
            {"role": "user", "content": f"Transcript:\n{transcript}"}
        ],
        "temperature": 0.2
    }

    r = requests.post(url, headers=headers, json=body, timeout=12)
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"].strip()

    # Best effort JSON parsing
    try:
        return json.loads(text)
    except Exception:
        # fallback if model returned extra text
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        return {"urgent": False, "summary": transcript[:220], "name": "", "callback_phone": "", "address": "", "issue": transcript[:300]}

# =====================
# Voice Webhooks
# =====================

@app.post("/voice")
def voice_entry():
    """
    Twilio hits this when a call comes in.
    We ask for a short description (speech input).
    """
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
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

@app.post("/voice/issue")
def voice_issue():
    issue = (request.form.get("SpeechResult") or "").strip()

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">Got it.</Say>
  <Say voice="Polly.Joanna">What is the best callback phone number?</Say>
  <Gather input="speech" speechTimeout="auto" action="/voice/phone" method="POST">
    <Say voice="Polly.Joanna">Say the phone number slowly.</Say>
  </Gather>
  <Say voice="Polly.Joanna">Sorry, I didn't catch that.</Say>
  <Redirect method="POST">/voice</Redirect>

  <Parameter name="issue" value="{issue.replace('"', '').replace('<','').replace('>','')}" />
</Response>"""
    # Twilio doesn't carry custom params like this; we’ll store in a cookie-less way via query in next step
    # So instead, we’ll pass along issue via a hidden field in the next response:
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">Got it.</Say>
  <Say voice="Polly.Joanna">What is the best callback phone number?</Say>
  <Gather input="speech" speechTimeout="auto" action="/voice/phone" method="POST">
    <Say voice="Polly.Joanna">Say the phone number slowly.</Say>
  </Gather>
  <Say voice="Polly.Joanna">Sorry, I didn't catch that.</Say>
  <Redirect method="POST">/voice</Redirect>
  <Play digits="w"/>
  <Say voice="Polly.Joanna"></Say>
  <Redirect method="POST">/voice/phone?issue={requests.utils.quote(issue)}</Redirect>
</Response>"""
    # NOTE: The above uses a redirect to pass 'issue' if Gather fails; normal path uses query below.
    return twiml(xml)

@app.post("/voice/phone")
def voice_phone():
    # issue comes from querystring on this endpoint (we redirect here after /voice/issue)
    issue = (request.args.get("issue") or "").strip()
    phone_spoken = (request.form.get("SpeechResult") or "").strip()

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">Thanks.</Say>
  <Say voice="Polly.Joanna">And what is your name?</Say>
  <Gather input="speech" speechTimeout="auto" action="/voice/finalize?issue={requests.utils.quote(issue)}&phone={requests.utils.quote(phone_spoken)}" method="POST">
    <Say voice="Polly.Joanna">Go ahead.</Say>
  </Gather>
  <Say voice="Polly.Joanna">Sorry, I didn't catch that.</Say>
  <Redirect method="POST">/voice</Redirect>
</Response>"""
    return twiml(xml)

@app.post("/voice/finalize")
def voice_finalize():
    issue = (request.args.get("issue") or "").strip()
    phone_spoken = (request.args.get("phone") or "").strip()
    name = (request.form.get("SpeechResult") or "").strip()

    call_sid = request.form.get("CallSid", "")
    from_number = request.form.get("From", "")
    transcript = f"CallerName: {name}\nCallback: {phone_spoken}\nIssue: {issue}\nFrom: {from_number}\nCallSid: {call_sid}"

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
        "source": "phone-ai"
    }

    delivered = post_lead(lead_payload)

    if urgent and FORWARD_NUMBER:
        # Warm transfer
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">Okay. This sounds urgent. I'm connecting you to a local emergency team now.</Say>
  <Dial>{FORWARD_NUMBER}</Dial>
  <Say voice="Polly.Joanna">If the line is busy, a team member will call you back shortly.</Say>
</Response>"""
        return twiml(xml)

    # Non-urgent: confirm and end
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">Thank you. A local team will call you back shortly.</Say>
  <Hangup/>
</Response>"""
    return twiml(xml)

@app.get("/")
def ok():
    return "OK"
