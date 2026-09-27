import json
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import boto3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config

EMAIL_RE = re.compile(config.EMAIL_PATTERN)
NAME_RE = re.compile(config.NAME_PATTERN)

s3 = boto3.client("s3", region_name=config.AWS_REGION)


def get_slot(intent, n):
    s = (intent.get("slots") or {}).get(n)
    return s["value"].get("interpretedValue") if s and s.get("value") else None


def set_slot(intent, n, v):
    intent["slots"][n] = None if v is None else {"value": {"originalValue": str(v), "interpretedValue": str(v)}}


def elicit(event, slot, msg):
    return {
        "sessionState": {
            "dialogAction": {"type": "ElicitSlot", "slotToElicit": slot},
            "intent": event["sessionState"]["intent"],
            "sessionAttributes": event["sessionState"].get("sessionAttributes", {})
        },
        "messages": [{"contentType": "PlainText", "content": msg}]
    }


def delegate(event):
    return {
        "sessionState": {
            "dialogAction": {"type": "Delegate"},
            "intent": event["sessionState"]["intent"],
            "sessionAttributes": event["sessionState"].get("sessionAttributes", {})
        }
    }


def close(event, ok, msg=""):
    return {
        "sessionState": {
            "dialogAction": {"type": "Close"},
            "intent": {**event["sessionState"]["intent"], "state": "Fulfilled" if ok else "Failed"},
            "sessionAttributes": event["sessionState"].get("sessionAttributes", {})
        },
        "messages": [{"contentType": "PlainText", "content": msg}] if msg else []
    }


def norm_phone(r):
    if not r:
        return None
    d = re.sub(r"\D", "", r)
    if len(d) == config.PHONE_DIGITS_10:
        return f"+1{d}"
    if len(d) == config.PHONE_DIGITS_11 and d.startswith("1"):
        return f"+{d}"
    return None


def valid_name(v):
    return bool(NAME_RE.match(v.strip())) and not re.search(r"\d", v)


def _collapse_repeats(word):
    return re.sub(r"(.)\1+", r"\1", word)


def _clean_tokens(text):
    return [w for w in re.sub(r"[^a-z' -]", " ", (text or "").strip().lower()).split() if w]


def is_filler(text):
    """True if the text is only filled pauses ("Ummm", "ahh", "hmm ...")."""
    toks = _clean_tokens(text)
    return bool(toks) and all(_collapse_repeats(w) in config.FILLER_ROOTS for w in toks)


# Words that can never be (part of) a name — if the transcript contains any of
# these, it is a greeting/request/answer, not a name; do not harvest.
NON_NAME_WORDS = frozenset({
    "hi", "hello", "hey", "good", "morning", "afternoon", "evening",
    "yes", "no", "yeah", "yep", "yup", "nope", "y", "n",
    "thanks", "thank", "please", "help", "need", "want", "call", "calling",
    "ok", "okay", "sure", "it", "is", "my", "name", "first", "last",
    "i", "am", "this", "a", "the", "here", "an", "to", "for",
})
# Leading words stripped before name detection ("Hi, this is John" -> "John").
NAME_PREFIX_WORDS = frozenset({
    "my", "name", "is", "first", "last", "this", "it",
    "i", "am", "i'm", "here", "hello", "hi", "hey",
})


def harvest_name_from_transcript(transcript):
    """Best-effort (first, last) from a raw transcript, or (None, None).

    Only fires on 1-2 clean alpha tokens with no greeting/request words, so
    greetings ("hi"), requests ("i need help") and answers ("yes") never
    become names. Callers answering the opening question with "John" or
    "John Smith" get their slots filled instead of being asked twice.
    """
    if not transcript:
        return (None, None)
    t = re.sub(r"[^a-z' -]", " ", transcript.strip().lower())
    toks = [w for w in t.split() if w]
    while toks and toks[0] in NAME_PREFIX_WORDS:
        toks.pop(0)
    # Drop filled pauses anywhere ("Ummm, John" -> ["john"]; "Ummm" -> []).
    toks = [w for w in toks if _collapse_repeats(w) not in config.FILLER_ROOTS]
    if not (1 <= len(toks) <= 2):
        return (None, None)
    if any(w in NON_NAME_WORDS or len(w) < 2 for w in toks):
        return (None, None)
    if not all(NAME_RE.match(w) for w in toks):
        return (None, None)
    first = toks[0].title()
    last = toks[1].title() if len(toks) == 2 else None
    return (first, last)


def slot_value(slots, name):
    s = (slots or {}).get(name)
    if not s:
        return None
    if isinstance(s, dict) and "value" in s and s["value"]:
        return s["value"].get("interpretedValue") or s["value"].get("originalValue")
    return None


# ─── Appointment booking (Google Calendar) ──────────────────────────

def _tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(config.TIMEZONE)
    except Exception as e:
        print(f"WARNING: timezone {config.TIMEZONE} unavailable ({e}) — booking unchecked")
        return None


def _google_calendar():
    """Lazy Calendar client from the service-account key JSON stored in S3."""
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as e:
        print(f"WARNING: google client libs missing ({e}) — booking unchecked")
        return None
    try:
        if not config.CALENDAR_ID:
            print("WARNING: CALENDAR_ID is empty — booking unchecked")
            return None
        obj = s3.get_object(Bucket=config.S3_BUCKET, Key=config.GOOGLE_CREDENTIALS_KEY)
        info = json.loads(obj["Body"].read())
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/calendar"])
        return build("calendar", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:
        print(f"WARNING: Google Calendar unavailable ({e}) — booking unchecked")
        return None


def _parse_appt_date(value, tz):
    """YYYY-MM-DD within [today, today+lookahead] or None."""
    if not value or not re.match(r"^\d{4}-\d{2}-\d{2}$", value.strip()):
        return None
    try:
        day = datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None
    today = datetime.now(tz).date()
    if day < today or (day - today).days > config.APPT_LOOKAHEAD_DAYS:
        return None
    return day


def _parse_appt_time(value):
    """HH:MM (24h) or None (vague MO/AF/EV/NI codes need a specific time)."""
    if not value:
        return None
    if value.strip().upper() in config.VAGUE_TIMES:
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})(?::\d{2})?$", value.strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return (h, mi)


def _in_business_hours(start, duration_min):
    if start.weekday() >= 5:
        return False
    end = start + timedelta(minutes=duration_min)
    if end.date() != start.date():
        return False
    return ((start.hour, start.minute) >= (config.BUSINESS_HOURS_START, 0)
            and (end.hour, end.minute) <= (config.BUSINESS_HOURS_END, 0))


def _busy_intervals(service, day_start, window_end):
    try:
        resp = service.freebusy().query(body={
            "timeMin": day_start.isoformat(), "timeMax": window_end.isoformat(),
            "timeZone": config.TIMEZONE,
            "items": [{"id": config.CALENDAR_ID}]}).execute()
        return [(b["start"], b["end"]) for b in
                resp.get("calendars", {}).get(config.CALENDAR_ID, {}).get("busy", [])]
    except Exception as e:
        print(f"WARNING: freebusy query failed ({e})")
        return None


def _overlaps(start, end, busy):
    for bs, be in busy:
        try:
            bs_dt = datetime.fromisoformat(bs)
            be_dt = datetime.fromisoformat(be)
        except ValueError:
            continue
        if start < be_dt and bs_dt < end:
            return True
    return False


def _ceil_step(dt, step_min):
    step = step_min * 60
    ts = dt.timestamp()
    return datetime.fromtimestamp(ts + (-ts % step), tz=dt.tzinfo)


def find_appt_slot(service, day, h, mi, tz):
    """(start, end, adjusted, checked) for the requested day/time.

    Returns the request itself when free and in business hours, else the next
    free in-hours slot (15-min grid). checked=False means the calendar could
    not be read — caller should proceed unchecked, not block.
    """
    dur = config.APPT_DURATION_MIN
    now = datetime.now(tz)
    req = _ceil_step(datetime(day.year, day.month, day.day, h, mi, tzinfo=tz),
                     config.APPT_STEP_MIN)
    if req < now + timedelta(minutes=5):
        req = _ceil_step(now + timedelta(minutes=5), config.APPT_STEP_MIN)
    window_end = now + timedelta(days=config.APPT_SEARCH_DAYS)
    day_start = datetime.combine(day, datetime.min.time()).replace(tzinfo=tz)
    busy = _busy_intervals(service, day_start, window_end)
    if busy is None:
        return (None, None, False, False)
    cand = req
    for _ in range(2000):
        if cand > window_end:
            return (None, None, False, True)
        end = cand + timedelta(minutes=dur)
        if _in_business_hours(cand, dur) and not _overlaps(cand, end, busy):
            return (cand, end, cand != req, True)
        cand += timedelta(minutes=config.APPT_STEP_MIN)
    return (None, None, False, True)


def pretty_appt(dt):
    h12 = int(dt.strftime("%I"))
    return f"{dt.strftime('%A')}, {dt.strftime('%B')} {dt.day} at {h12}:{dt.strftime('%M')} {dt.strftime('%p')}"


def _norm_affirm(text):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", (text or "").strip().lower()))


def is_affirmative(text):
    t = _norm_affirm(text)
    return t in config.YES_ANSWERS or t in config.APPT_ACCEPT_EXTRA


def book_appt(service, start, end, first, last, email, callback, reason, contact_id):
    body = {
        "summary": f"Call with {first} {last} — iFirm intake",
        "description": ("Booked by voice agent.\n"
                        f"Name: {first} {last}\nEmail: {email}\n"
                        f"Callback: {callback}\nReason: {reason}\n"
                        f"Contact: {contact_id}"),
        "start": {"dateTime": start.isoformat(), "timeZone": config.TIMEZONE},
        "end": {"dateTime": end.isoformat(), "timeZone": config.TIMEZONE},
    }
    ev = service.events().insert(calendarId=config.CALENDAR_ID, body=body).execute()
    return ev.get("id"), ev.get("htmlLink")


def _resolve_fulfillment_appt(event, intent, attrs):
    """Fresh availability check + booking. Never raises: returns status dict."""
    appt = {"requested": None, "start": None, "end": None,
            "status": "unchecked", "eventId": None, "eventLink": None}
    slots = intent.get("slots") or {}
    tz = _tz()
    day_s = slot_value(slots, config.SLOT_APPT_DATE)
    time_s = slot_value(slots, config.SLOT_APPT_TIME)
    if tz is None or not day_s:
        return appt
    day = _parse_appt_date(day_s, tz)
    parsed = _parse_appt_time(time_s) if time_s else None
    if day is None or parsed is None:
        return appt
    req = datetime(day.year, day.month, day.day, parsed[0], parsed[1], tzinfo=tz)
    requested_iso = attrs.get("apptRequested") or req.isoformat()
    appt["requested"] = requested_iso
    service = _google_calendar() if config.CALENDAR_ID else None
    if service is None:
        appt["start"] = req.isoformat()
        appt["end"] = (req + timedelta(minutes=config.APPT_DURATION_MIN)).isoformat()
        return appt
    start, end, adjusted, checked = find_appt_slot(service, day, parsed[0], parsed[1], tz)
    if not checked or start is None:
        appt["start"] = req.isoformat()
        appt["end"] = (req + timedelta(minutes=config.APPT_DURATION_MIN)).isoformat()
        appt["status"] = "no_availability" if checked else "unchecked"
        return appt
    try:
        eid, link = book_appt(
            service, start, end,
            slot_value(slots, config.SLOT_FIRST_NAME) or "unknown",
            slot_value(slots, config.SLOT_LAST_NAME) or "unknown",
            slot_value(slots, config.SLOT_EMAIL),
            slot_value(slots, config.SLOT_CALLBACK_NUMBER),
            slot_value(slots, config.SLOT_REASON),
            (event.get("requestAttributes") or {}).get("contactId", "unknown"),
        )
    except Exception as e:
        print(f"ERROR: booking failed ({e}) — saving flagged record")
        appt.update(start=start.isoformat(), end=end.isoformat(), status="booking_failed")
        return appt
    appt.update(start=start.isoformat(), end=end.isoformat(),
                status="adjusted" if requested_iso != start.isoformat() else "booked",
                eventId=eid, eventLink=link)
    return appt


def save_record(event, intent, attrs, appt=None):
    """Persist the confirmed caller record to S3. Same schema as s3_saver.py,
    plus appointment fields (all None when booking is unchecked)."""
    slots = intent.get("slots") or {}
    first = slot_value(slots, config.SLOT_FIRST_NAME) or "unknown"
    last = slot_value(slots, config.SLOT_LAST_NAME) or "unknown"
    email = slot_value(slots, config.SLOT_EMAIL)
    callback = slot_value(slots, config.SLOT_CALLBACK_NUMBER)
    reason = slot_value(slots, config.SLOT_REASON)
    ani = (attrs or {}).get("contactNumber", "unknown")
    contact_id = (event.get("requestAttributes") or {}).get("contactId", "unknown")
    appt = appt or {}

    now = datetime.now(timezone.utc)
    record_id = str(uuid.uuid4())[:config.S3_RECORD_ID_LEN]
    ts = now.strftime(config.S3_KEY_TIMESTAMP_FMT)
    filename = f"{ts}-{first.lower()}-{last.lower()}-{record_id}.json"
    s3_key = f"{config.S3_PREFIX}{filename}"

    record = {
        "recordId": record_id,
        "contactId": contact_id,
        "timestamp": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "callingNumber": ani,
        "firstName": first,
        "lastName": last,
        "fullName": f"{first} {last}",
        "email": email,
        "callbackNumber": callback,
        "reasonForCalling": reason,
        "appointmentRequested": appt.get("requested"),
        "appointmentStart": appt.get("start"),
        "appointmentEnd": appt.get("end"),
        "appointmentStatus": appt.get("status", "unchecked"),
        "calendarEventId": appt.get("eventId"),
        "calendarEventLink": appt.get("eventLink"),
    }

    print(f"Saving record for {first} {last} to bucket {config.S3_BUCKET} (contact {contact_id})")
    s3.put_object(
        Bucket=config.S3_BUCKET,
        Key=s3_key,
        Body=json.dumps(record, indent=2),
        ContentType="application/json",
    )
    print(f"Saved {s3_key} to {config.S3_BUCKET}")
    return record, filename


def _valid_email(v):
    return bool(v and EMAIL_RE.match(v.strip()))


def _send_email(ses, from_addr, to_addrs, subject, body_text, record, filename):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg.set_content(body_text)
    resp = ses.send_raw_email(RawMessage={"Data": msg.as_bytes()})
    return resp.get("MessageId")


def send_record_email(record, filename):
    """Email the captured JSON via SES: receipt to the caller + copy to admins.

    Fail-open throughout: any mail failure is only logged, the call and the
    S3 record are unaffected.
    """
    if not config.NOTIFICATION_ENABLED:
        return
    admins = [
        a.strip()
        for a in (config.NOTIFICATION_EMAIL or "").replace(";", ",").split(",")
    ]
    admins = [a for a in admins if a]
    for bad in [a for a in admins if not _valid_email(a)]:
        print(f"WARNING: skipping invalid admin email address: {bad}")
    admins = [a for a in admins if _valid_email(a)]
    caller = (record.get("email") or "").strip()
    caller_ok = _valid_email(caller) and config.NOTIFICATION_CALLER_ENABLED
    if _valid_email(caller) and not config.NOTIFICATION_CALLER_ENABLED:
        print("Caller receipt disabled — skipping caller email")
    if not caller_ok and not admins:
        print("Email notifications enabled but no valid recipient "
              "(caller email invalid, no admin emails) — skipping")
        return
    from_addr = (config.SES_SENDER_EMAIL or "").strip()
    if not from_addr:
        print("Email notifications enabled but SES_SENDER_EMAIL is empty — skipping "
              "(SES requires a verified sender)")
        return
    try:
        ses = boto3.client("ses", region_name=config.AWS_REGION)
    except Exception as e:
        print(f"WARNING: could not create SES client: {e}")
        return
    if caller_ok:
        try:
            mid = _send_email(
                ses, from_addr, [caller],
                "Thank you — iFirm received your information",
                "Hi " + (record.get("firstName") or "there") + ",\n\n"
                "Thank you for calling iFirm. We have received your details and "
                "someone from our team will reach out to you soon.\n\n"
                f"Name: {record.get('fullName')}\n"
                f"Email: {record.get('email')}\n"
                f"Callback: {record.get('callbackNumber')}\n"
                f"Reason: {record.get('reasonForCalling')}\n"
                f"Appointment: {record.get('appointmentStart')} ({record.get('appointmentStatus')})\n",
                record, filename,
            )
            print(f"Sent caller receipt to {caller}: {mid}")
        except Exception as e:
            print(f"WARNING: could not send caller receipt to {caller}: {e}")
    if admins:
        try:
            mid = _send_email(
                ses, from_addr, admins,
                f"New caller intake: {record.get('fullName', 'unknown')} ({record.get('recordId', '')})",
                "A new caller intake record was saved.\n\n"
                f"Name: {record.get('fullName')}\n"
                f"Email: {record.get('email')}\n"
                f"Callback: {record.get('callbackNumber')} "
                f"(calling number: {record.get('callingNumber')})\n"
                f"Reason: {record.get('reasonForCalling')}\n"
                f"Appointment: {record.get('appointmentStart')} "
                f"({record.get('appointmentStatus')})"
                + (f"\nEvent: {record.get('calendarEventLink')}" if record.get("calendarEventLink") else "")
                + (f"\nNOTE: booking {record.get('appointmentStatus')} — follow up manually."
                   if record.get("appointmentStatus") in ("booking_failed", "unchecked", "no_availability") else "")
                + f"\nTimestamp (UTC): {record.get('timestamp')}\n"
                f"S3 key: {config.S3_PREFIX}{filename}\n",
                record, filename,
            )
            print(f"Sent intake email to {', '.join(admins)}: {mid}")
        except Exception as e:
            print(f"WARNING: could not send intake email to {', '.join(admins)}: {e}")


def lambda_handler(event, ctx):
    ss = event["sessionState"]
    intent = ss["intent"]
    attrs = ss.get("sessionAttributes", {}) or {}
    src = event.get("invocationSource", "")
    ani = attrs.get("contactNumber", "")
    first = get_slot(intent, config.SLOT_FIRST_NAME)
    last = get_slot(intent, config.SLOT_LAST_NAME)
    email = get_slot(intent, config.SLOT_EMAIL)
    isbest = get_slot(intent, config.SLOT_IS_BEST_NUMBER)
    cb = get_slot(intent, config.SLOT_CALLBACK_NUMBER)
    reason = get_slot(intent, config.SLOT_REASON)

    if src == "FulfillmentCodeHook":
        if intent.get("confirmationState") == "Denied":
            for k in list(intent["slots"].keys()):
                intent["slots"][k] = None
            intent["confirmationState"] = "None"
            for k in ("apptStart", "apptEnd", "apptRequested", "pendingSuggestion"):
                attrs.pop(k, None)
            return elicit(event, config.SLOT_FIRST_NAME, config.PROMPT_DECLINE)
        # Confirmed: re-resolve the appointment fresh, book it, persist, email.
        appt = _resolve_fulfillment_appt(event, intent, attrs)
        try:
            record, filename = save_record(event, intent, attrs, appt)
        except Exception:
            print("ERROR: failed to save record to S3")
            raise
        send_record_email(record, filename)
        return close(event, True)

    # Backstop against double-asking: if Lex matched the intent but left name
    # slots empty, harvest them from this turn's raw transcript ("John" or
    # "John Smith" answering the opening question). Runs only while a name
    # slot is empty, so later turns are unaffected.
    if not first or not last:
        h_first, h_last = harvest_name_from_transcript(event.get("inputTranscript", ""))
        if h_first and not first:
            set_slot(intent, config.SLOT_FIRST_NAME, h_first)
            first = h_first
        if h_last and not last:
            set_slot(intent, config.SLOT_LAST_NAME, h_last)
            last = h_last

    if first and (not valid_name(first) or is_filler(first)):
        set_slot(intent, config.SLOT_FIRST_NAME, None)
        return elicit(event, config.SLOT_FIRST_NAME, config.PROMPT_INVALID_NAME)
    if last and (not valid_name(last) or is_filler(last)):
        set_slot(intent, config.SLOT_LAST_NAME, None)
        return elicit(event, config.SLOT_LAST_NAME, config.PROMPT_INVALID_LAST)
    if email and not EMAIL_RE.match(email.strip()):
        set_slot(intent, config.SLOT_EMAIL, None)
        return elicit(event, config.SLOT_EMAIL, config.PROMPT_INVALID_EMAIL)
    if email and not isbest:
        return elicit(event, config.SLOT_IS_BEST_NUMBER, f"I see you are calling from {ani}. Is this the best number to reach you? Say yes or no.")
    if isbest:
        v = isbest.lower()
        if v in config.YES_ANSWERS:
            n = norm_phone(ani) or ani
            set_slot(intent, config.SLOT_CALLBACK_NUMBER, n)
            attrs["finalCallback"] = n
            if not reason:
                return delegate(event)
        elif v in config.NO_ANSWERS:
            if not cb:
                return elicit(event, config.SLOT_CALLBACK_NUMBER, config.PROMPT_CALLBACK_NUMBER)
            n = norm_phone(cb)
            if not n:
                set_slot(intent, config.SLOT_CALLBACK_NUMBER, None)
                return elicit(event, config.SLOT_CALLBACK_NUMBER, config.PROMPT_CALLBACK_INVALID)
            set_slot(intent, config.SLOT_CALLBACK_NUMBER, n)
            attrs["finalCallback"] = n
        else:
            set_slot(intent, config.SLOT_IS_BEST_NUMBER, None)
            return elicit(event, config.SLOT_IS_BEST_NUMBER, f"Is {ani} the best number? Say yes or no.")
    if reason and (len(reason.strip()) < config.MIN_REASON_LEN or is_filler(reason)):
        set_slot(intent, config.SLOT_REASON, None)
        return elicit(event, config.SLOT_REASON, config.PROMPT_MIN_REASON)

    # Appointment scheduling runs only once contact details + reason are in.
    if not all([first, last, email, isbest, cb, reason]):
        return delegate(event)
    tz = _tz()
    if tz is None:
        return delegate(event)
    appt_date = get_slot(intent, config.SLOT_APPT_DATE)
    appt_time = get_slot(intent, config.SLOT_APPT_TIME)
    transcript = event.get("inputTranscript", "") or ""

    if not appt_date:
        return elicit(event, config.SLOT_APPT_DATE, config.PROMPT_APPT_DATE)
    day = _parse_appt_date(appt_date, tz)
    if day is None:
        set_slot(intent, config.SLOT_APPT_DATE, None)
        return elicit(event, config.SLOT_APPT_DATE, config.PROMPT_APPT_DATE_INVALID)

    pending_iso = attrs.get("pendingSuggestion")
    if pending_iso and appt_time:
        attrs.pop("pendingSuggestion", None)
        pending_iso = None
    if pending_iso and not appt_time:
        # Caller answering the suggestion offer.
        if is_affirmative(transcript):
            try:
                sug = datetime.fromisoformat(pending_iso)
            except ValueError:
                sug = None
            if sug is not None:
                set_slot(intent, config.SLOT_APPT_TIME, f"{sug.hour:02d}:{sug.minute:02d}")
                attrs["apptStart"] = sug.isoformat()
                attrs["apptEnd"] = (sug + timedelta(minutes=config.APPT_DURATION_MIN)).isoformat()
                attrs.pop("pendingSuggestion", None)
                return delegate(event)
        elif _norm_affirm(transcript) in config.NO_ANSWERS:
            attrs.pop("pendingSuggestion", None)
            return elicit(event, config.SLOT_APPT_TIME, config.PROMPT_APPT_TIME)
        # Anything else: fall through and re-offer below.

    if not appt_time:
        if pending_iso:
            try:
                sug = datetime.fromisoformat(pending_iso)
            except ValueError:
                sug = None
            if sug is not None:
                return elicit(event, config.SLOT_APPT_TIME,
                    f"The next opening is {pretty_appt(sug)}. Does that work for you? Say yes, or suggest another time.")
        return elicit(event, config.SLOT_APPT_TIME, config.PROMPT_APPT_TIME)

    parsed = _parse_appt_time(appt_time)
    if parsed is None:
        set_slot(intent, config.SLOT_APPT_TIME, None)
        attrs.pop("pendingSuggestion", None)
        return elicit(event, config.SLOT_APPT_TIME, config.PROMPT_APPT_TIME_INVALID)

    h, mi = parsed
    if "apptRequested" not in attrs:
        attrs["apptRequested"] = datetime(day.year, day.month, day.day, h, mi, tzinfo=tz).isoformat()
    service = _google_calendar() if config.CALENDAR_ID else None
    if service is None:
        req = datetime(day.year, day.month, day.day, h, mi, tzinfo=tz)
        attrs["apptStart"] = req.isoformat()
        attrs["apptEnd"] = (req + timedelta(minutes=config.APPT_DURATION_MIN)).isoformat()
        return delegate(event)
    start, end, adjusted, checked = find_appt_slot(service, day, h, mi, tz)
    if not checked or start is None:
        if not checked:
            req = datetime(day.year, day.month, day.day, h, mi, tzinfo=tz)
            attrs["apptStart"] = req.isoformat()
            attrs["apptEnd"] = (req + timedelta(minutes=config.APPT_DURATION_MIN)).isoformat()
            return delegate(event)
        return elicit(event, config.SLOT_APPT_TIME,
            "I could not find any opening in the next 7 days. Please suggest a later date, or call us directly and we will arrange it.")
    if not adjusted:
        attrs["apptStart"] = start.isoformat()
        attrs["apptEnd"] = end.isoformat()
        attrs.pop("pendingSuggestion", None)
        return delegate(event)
    req_pretty = pretty_appt(datetime(day.year, day.month, day.day, h, mi, tzinfo=tz))
    if "apptRequested" not in attrs:
        attrs["apptRequested"] = datetime(day.year, day.month, day.day, h, mi, tzinfo=tz).isoformat()
    attrs["pendingSuggestion"] = start.isoformat()
    return elicit(event, config.SLOT_APPT_TIME,
        f"Sorry, {req_pretty} is not available. The next opening is {pretty_appt(start)}. Does that work for you? Say yes, or suggest another time.")
