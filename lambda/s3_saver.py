import json
import os
import sys
import uuid
from datetime import datetime, timezone

import boto3

# Add project root to path so config.py is importable in Lambda
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config

s3 = boto3.client("s3", region_name=config.AWS_REGION)


def _get_slot_value(slots, name):
    s = (slots or {}).get(name)
    if not s:
        return None
    if isinstance(s, dict) and "value" in s and s["value"]:
        return s["value"].get("interpretedValue") or s["value"].get("originalValue")
    return None


def lambda_handler(event, context):
    intent = event["sessionState"]["intent"]
    slots = intent.get("slots") or {}
    session_attrs = event["sessionState"].get("sessionAttributes") or {}

    first = _get_slot_value(slots, config.SLOT_FIRST_NAME)
    last = _get_slot_value(slots, config.SLOT_LAST_NAME)
    email = _get_slot_value(slots, config.SLOT_EMAIL)
    callback = _get_slot_value(slots, config.SLOT_CALLBACK_NUMBER)
    reason = _get_slot_value(slots, config.SLOT_REASON)
    ani = (session_attrs or {}).get("contactNumber", "unknown")
    contact_id = (event.get("requestAttributes") or {}).get("contactId", "unknown")

    now = datetime.now(timezone.utc)
    record_id = str(uuid.uuid4())[:config.S3_RECORD_ID_LEN]
    timestamp = now.isoformat()

    ts = now.strftime(config.S3_KEY_TIMESTAMP_FMT)
    filename = f"{ts}-{first.lower()}-{last.lower()}-{record_id}.json"
    s3_key = f"{config.S3_PREFIX}{filename}"

    record = {
        "recordId": record_id,
        "contactId": contact_id,
        "timestamp": timestamp,
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

    s3.put_object(
        Bucket=config.S3_BUCKET,
        Key=s3_key,
        Body=json.dumps(record, indent=2),
        ContentType="application/json",
    )

    print(f"Saved {s3_key} to {config.S3_BUCKET}")
    return {
        "sessionState": {
            "dialogAction": {"type": "Close"},
            "intent": {**intent, "state": "Fulfilled"},
            "sessionAttributes": session_attrs,
        },
        "messages": [
            {
                "contentType": "PlainText",
                "content": config.PROMPT_CLOSING,
            }
        ],
    }
