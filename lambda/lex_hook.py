import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
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


def save_record(event, intent, attrs):
    """Persist the confirmed caller record to S3. Same schema as s3_saver.py."""
    slots = intent.get("slots") or {}
    first = slot_value(slots, config.SLOT_FIRST_NAME) or "unknown"
    last = slot_value(slots, config.SLOT_LAST_NAME) or "unknown"
    email = slot_value(slots, config.SLOT_EMAIL)
    callback = slot_value(slots, config.SLOT_CALLBACK_NUMBER)
    reason = slot_value(slots, config.SLOT_REASON)
    ani = (attrs or {}).get("contactNumber", "unknown")
    contact_id = (event.get("requestAttributes") or {}).get("contactId", "unknown")

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
        print("Caller receipt disabled by NOTIFICATION_CALLER_ENABLED — skipping caller email") and config.NOTIFICATION_CALLER_ENABLED
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
                f"Reason: {record.get('reasonForCalling')}\n",
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
                f"Timestamp (UTC): {record.get('timestamp')}\n"
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
            return elicit(event, config.SLOT_FIRST_NAME, config.PROMPT_DECLINE)
        # Confirmed: Lex routes fulfillment to this same Lambda (one code hook
        # per alias), so persist the record here before closing.
        try:
            record, filename = save_record(event, intent, attrs)
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
    return delegate(event)
