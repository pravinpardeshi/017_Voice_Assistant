# Amazon Connect Voice Agent — Caller Intake (SIN Name, Email, Callback, Reason)

Voice agent that answers an inbound call, collects:

1. **First Name** — exactly as on Canadian SIN card (name only, **never** the SIN number)
2. **Last Name** — as on SIN card
3. **Email**
4. **Best callback number** — asks if calling number is best, if No asks for alternate
5. **Reason for calling**
6. **Confirmation** — reads back all details, asks if changes needed
7. **Close** — plays `Thank you. Someone from iFirm will reach out to you. Have a nice day.` and disconnects

Stack: **Amazon Connect (voice flow) + Amazon Lex V2 (dialog) + AWS Lambda Python 3.12 (validation + S3 save) + S3 (storage) + Flask (local viewer)**.

---

## 0. Project Structure

```
027_AWS_iFirm_Opencode/
├── README.md                    # this guide (manual console path)
├── DEPLOY.md                    # automated deploy guide (`deploy.sh`)
├── deploy.sh                    # idempotent deploy: S3 + Lambdas + Lex V2 (preferred path)
├── contact-flow.json            # import-ready Connect flow (validated schema, all branches wired)
├── config.py                  # All configuration variables (S3, Lex, Connect, prompts)
├── pyproject.toml
├── lambda/
│   ├── lex_hook.py            # active Lambda: validation + name harvesting + S3 save + SES email
│   └── s3_saver.py            # standalone reference saver (not in the active path)
├── viewer/
│   ├── app.py                 # Flask viewer — search, view, download records
│   └── templates/
│       ├── index.html         # Record list table with search
│       ├── detail.html        # Single record detail + raw JSON
│       └── error.html         # Error page
└── src/
    └── 027_aws_ifirm_opencode/
        └── __init__.py
```

All environment-specific values live in `config.py`. Override via environment variables or edit directly before deploying.

---

## 1. Architecture

```text
PSTN Caller
  -> Amazon Connect Inbound Contact Flow
    -> Set Voice (Amazon Polly: {POLLY_VOICE_ID} {POLLY_ENGINE}) + Set contactNumber = $.CustomerEndpoint.Address
     -> Get Customer Input (Lex V2 Bot, Alias Prod, Locale en_US)
          Lex slots <-> Lambda DialogCodeHook (lex_hook.py: validation)
     -> On Fulfilled: same Lambda saves the confirmed record to S3 (one code hook per alias)
    -> Play Prompt: "Thank you. Someone from iFirm will reach out to you. Have a nice day."
    -> Disconnect
```

```text
S3 Bucket (calls/yy_mm_dd_hh_mm_ss-first-last-recordid.json)
  -> Local Viewer App (Flask, http://localhost:5000)
       -> Search / View / Download JSON
```

Lex does slot elicitation. `lex_hook.py` handles:

- name validation (letters only, blocks digits/SIN numbers),
- email regex validation,
- Yes/No normalization for best-number,
- phone normalization to E.164 `+1xxxxxxxxxx`,
- conditional skip of `CallbackNumber` when answer is Yes (auto-fill from ANI),
- restart at `FirstName` when confirmation is Denied,
- on confirmation: writing the record to S3 as JSON (`save_record`, same schema as below).

`s3_saver.py` is a standalone reference implementation of the same save (not in
the active path — Lex routes fulfillment to the hook Lambda):

- writing the confirmed record to S3 as JSON with searchable key structure,
- includes recordId, contactId, timestamp, all caller fields.

---

## 2. Prerequisites

1. AWS account with admin or equivalent for Connect, Lex V2, Lambda, IAM, S3.
2. Region: use `ca-central-1` (Canada) for data residency. Keep Connect, Lex, Lambda, S3 in the **same region**.
3. An existing Amazon Connect instance. If none:
   - Go to `AWS Console > Amazon Connect > Create instance` > Identity management: `Store users within Amazon Connect` > Admin user > Create.
   - Note the Instance ARN: `arn:aws:connect:ca-central-1:ACCOUNT:instance/INSTANCE-ID`.
4. A claimed phone number (or claim one in Step 6), and a test handset.
5. IAM permission to create roles/policies for Lex + Lambda + S3.
6. Python 3.12+ installed locally (for running the viewer app).
7. Optional: Terraform >= 1.5 if you want IaC deployment (Section 11). Manual console steps below work without it.

---

## 3. Step 1 — Create the Lambda Dialog Hook

### 3.1 Create function

1. Go to `Lambda > Create function > Author from scratch`.
2. Name: `connect-voice-agent-hook`, Runtime: `Python 3.12`, Architecture: `x86_64`.
3. Execution role: `Create a new role with basic Lambda permissions`.
4. Create, then open `Code > lambda_function.py`, replace with code in Section 3.2, `Deploy`.
5. Go to `Configuration > General configuration > Timeout`: set to `15 sec`.

### 3.2 Code (`lambda_function.py`)

Handler: `lambda_function.lambda_handler`.

> **Single source of truth:** copy the current contents of
> `lambda/lex_hook.py` from this repo (packaged as `lambda_function.py` with
> `config.py` beside it — exactly what `deploy.sh` builds into `lex-hook.zip`).
> The listing below is an older snapshot kept for reference; the repo file
> additionally handles S3 saving on fulfillment, transcript name-harvesting,
> and SES email notifications. Behavior summary:
>
> - **Dialog turns**: name validation (letters only, blocks digits/SIN numbers),
>   filler rejection ("ummm"/"aahh" and elongations are never valid answers —
>   patterns in `config.py: FILLER_ROOTS`),
>   email regex validation, Yes/No normalization, phone normalization to E.164,
>   conditional skip of `CallbackNumber` on Yes (auto-fill from ANI), restart at
>   `FirstName` on confirmation Deny, plus transcript harvesting (fills empty
>   name slots from "John"/"John Smith" answers so questions aren't repeated).
> - **Fulfillment (confirmed)**: writes the record to
>   `calls/yy_mm_dd_hh_mm_ss-first-last-recordid.json`, emails JSON to the
>   caller (if enabled) + admin addresses (see §13), then closes.
> - **Fulfillment (denied)**: clears slots and restarts at `FirstName`.

```python
import os, re, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config

EMAIL_RE = re.compile(config.EMAIL_PATTERN)
NAME_RE = re.compile(config.NAME_PATTERN)

def get_slot(intent, n):
    s = (intent.get("slots") or {}).get(n)
    return s["value"].get("interpretedValue") if s and s.get("value") else None

def set_slot(intent, n, v):
    intent["slots"][n] = None if v is None else {"value": {"originalValue": str(v), "interpretedValue": str(v)}}

def elicit(event, slot, msg):
    return {"sessionState": {"dialogAction": {"type": "ElicitSlot", "slotToElicit": slot},
        "intent": event["sessionState"]["intent"],
        "sessionAttributes": event["sessionState"].get("sessionAttributes", {})},
        "messages": [{"contentType": "PlainText", "content": msg}]}

def delegate(event):
    return {"sessionState": {"dialogAction": {"type": "Delegate"},
        "intent": event["sessionState"]["intent"],
        "sessionAttributes": event["sessionState"].get("sessionAttributes", {})}}

def close(event, ok, msg=""):
    return {"sessionState": {"dialogAction": {"type": "Close"},
        "intent": {**event["sessionState"]["intent"], "state": "Fulfilled" if ok else "Failed"},
        "sessionAttributes": event["sessionState"].get("sessionAttributes", {})},
        "messages": [{"contentType": "PlainText", "content": msg}] if msg else []}

def norm_phone(r):
    if not r: return None
    d = re.sub(r"\D", "", r)
    if len(d) == config.PHONE_DIGITS_10: return f"+1{d}"
    if len(d) == config.PHONE_DIGITS_11 and d.startswith("1"): return f"+{d}"
    return None

def valid_name(v):
    return bool(NAME_RE.match(v.strip())) and not re.search(r"\d", v)

def lambda_handler(event, ctx):
    ss = event["sessionState"]; intent = ss["intent"]
    attrs = ss.get("sessionAttributes", {}) or {}; src = event.get("invocationSource", "")
    ani = attrs.get("contactNumber", "")
    first = get_slot(intent, config.SLOT_FIRST_NAME)
    last = get_slot(intent, config.SLOT_LAST_NAME)
    email = get_slot(intent, config.SLOT_EMAIL)
    isbest = get_slot(intent, config.SLOT_IS_BEST_NUMBER)
    cb = get_slot(intent, config.SLOT_CALLBACK_NUMBER)
    reason = get_slot(intent, config.SLOT_REASON)

    if src == "FulfillmentCodeHook":
        if intent.get("confirmationState") == "Denied":
            for k in list(intent["slots"].keys()): intent["slots"][k] = None
            intent["confirmationState"] = "None"
            return elicit(event, config.SLOT_FIRST_NAME, config.PROMPT_DECLINE)
        return close(event, True)

    if first and not valid_name(first):
        set_slot(intent, config.SLOT_FIRST_NAME, None)
        return elicit(event, config.SLOT_FIRST_NAME, config.PROMPT_INVALID_NAME)
    if last and not valid_name(last):
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
            n = norm_phone(ani) or ani; set_slot(intent, config.SLOT_CALLBACK_NUMBER, n); attrs["finalCallback"] = n
            if not reason: return delegate(event)
        elif v in config.NO_ANSWERS:
            if not cb: return elicit(event, config.SLOT_CALLBACK_NUMBER, config.PROMPT_CALLBACK_NUMBER)
            n = norm_phone(cb)
            if not n:
                set_slot(intent, config.SLOT_CALLBACK_NUMBER, None)
                return elicit(event, config.SLOT_CALLBACK_NUMBER, config.PROMPT_CALLBACK_INVALID)
            set_slot(intent, config.SLOT_CALLBACK_NUMBER, n); attrs["finalCallback"] = n
        else:
            set_slot(intent, config.SLOT_IS_BEST_NUMBER, None)
            return elicit(event, config.SLOT_IS_BEST_NUMBER, f"Is {ani} the best number? Say yes or no.")
    if reason and len(reason.strip()) < config.MIN_REASON_LEN:
        set_slot(intent, config.SLOT_REASON, None)
        return elicit(event, config.SLOT_REASON, config.PROMPT_MIN_REASON)
    return delegate(event)
```

### 3.3 Allow Lex to invoke Lambda

After the Lex bot exists (Step 2) add a resource policy, or now pre-authorize via CLI:

```bash
aws lambda add-permission \
  --function-name connect-voice-agent-hook \
  --statement-id lex-invoke \
  --action lambda:InvokeFunction \
  --principal lexv2.amazonaws.com \
  --source-arn "arn:aws:lex:ca-central-1:ACCOUNT-ID:bot-alias/*/*"
```

Note the Lambda ARN for Step 2.6.

---

## 3.5 Step 1b — Create S3 Bucket for Records

1. Go to `S3 > Buckets > Create bucket`.
2. Bucket name: `connect-caller-intake-<ACCOUNT-ID>` (globally unique).
3. Region: `ca-central-1`.
4. Block Public Access: `ON` (default).
5. Bucket versioning: `Enable` (recommended for audit trail).
6. Default encryption: `SSE-S3` or `SSE-KMS` (your choice).
7. Create.

### S3 Key Naming Convention

```text
calls/yy_mm_dd_hh_mm_ss-first-last-recordid.json
```

Example: `calls/25_09_20_14_32_10-john-smith-a1b2c3d4.json`

Benefits:
- **Compact timestamp prefix** — `yy_mm_dd_hh_mm_ss` sorts chronologically with no subdirectories.
- **Name searchable** — `first-last` in filename allows `aws s3 ls` filtering.
- **Unique** — 8-char UUID suffix prevents collisions on same-second calls.
- **No PII in path** — names are lowercase, first/last only (no SIN number).

Search by name: `aws s3 ls s3://connect-caller-intake-XXXX/calls/ --recursive | grep "john-smith"`
Search by date: `aws s3 ls s3://connect-caller-intake-XXXX/calls/ --recursive | grep "25_09_20"`

---

## 3.6 Step 1c — Create S3 Saver Lambda (reference only — optional)

> The active path needs **no** separate saver: `connect-voice-agent-hook`
> saves on fulfillment (Lex allows one code-hook Lambda per alias), and
> `deploy.sh` deploys it that way. Create the saver below only if you want a
> standalone reference copy; do not attach it as the fulfillment Lambda.

### 3.6.1 Create function

1. Go to `Lambda > Create function > Author from scratch`.
2. Name: `connect-caller-intake-saver`, Runtime: `Python 3.12`, Architecture: `x86_64`.
3. Execution role: `Create a new role with basic Lambda permissions`.
4. Create, then go to `Configuration > Environment variables`:
   - Key: `S3_BUCKET`, Value: `connect-caller-intake-<ACCOUNT-ID>` (your bucket from 3.5).
5. Go to `Configuration > General configuration > Timeout`: set to `15 sec`.
6. Open `Code > lambda_function.py`, replace with code below, `Deploy`.

> Note: `config.py` reads `S3_BUCKET` from env var via `os.environ.get()`. The Lambda env var overrides the default in config.py. You can also edit `config.py` directly before packaging.

### 3.6.2 Code (`s3_saver.py`)

```python
import json, os, sys, uuid
from datetime import datetime, timezone
import boto3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config

s3 = boto3.client("s3", region_name=config.AWS_REGION)

def _get_slot_value(slots, name):
    s = (slots or {}).get(name)
    if not s: return None
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
    ani = session_attrs.get("contactNumber", "unknown")
    contact_id = event.get("requestAttributes", {}).get("contactId", "unknown")
    now = datetime.now(timezone.utc)
    record_id = str(uuid.uuid4())[:config.S3_RECORD_ID_LEN]
    ts = now.strftime(config.S3_KEY_TIMESTAMP_FMT)
    filename = f"{ts}-{first.lower()}-{last.lower()}-{record_id}.json"
    s3_key = f"{config.S3_PREFIX}{filename}"
    record = {
        "recordId": record_id, "contactId": contact_id, "timestamp": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M:%S"),
        "callingNumber": ani, "firstName": first, "lastName": last,
        "fullName": f"{first} {last}", "email": email,
        "callbackNumber": callback, "reasonForCalling": reason,
    }
    s3.put_object(Bucket=config.S3_BUCKET, Key=s3_key, Body=json.dumps(record, indent=2), ContentType="application/json")
    print(f"Saved {s3_key} to {config.S3_BUCKET}")
    return {
        "sessionState": {
            "dialogAction": {"type": "Close"},
            "intent": {**intent, "state": "Fulfilled"},
            "sessionAttributes": session_attrs,
        },
        "messages": [{"contentType": "PlainText", "content": config.PROMPT_CLOSING}],
    }
```

### 3.6.3 IAM — Allow saver Lambda to write to S3

The basic Lambda execution role already has CloudWatch Logs. Add S3 access:

1. Go to `IAM > Roles > connect-caller-intake-saver-role > Add permissions > Create inline policy`.
2. JSON tab, paste:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::connect-caller-intake-<ACCOUNT-ID>",
        "arn:aws:s3:::connect-caller-intake-<ACCOUNT-ID>/*"
      ]
    }
  ]
}
```

3. Name: `s3-caller-intake-access`, Create policy.

### 3.6.4 Allow Lex to invoke saver Lambda

```bash
aws lambda add-permission \
  --function-name connect-caller-intake-saver \
  --statement-id lex-invoke \
  --action lambda:InvokeFunction \
  --principal lexv2.amazonaws.com \
  --source-arn "arn:aws:lex:ca-central-1:ACCOUNT-ID:bot-alias/*/*"
```

> **Note:** The Lex bot is created in Step 4 below. Lambda code hooks are attached there.

---

## 3.7 S3 JSON Record Schema

Each saved JSON file contains:

```json
{
  "recordId": "a1b2c3d4",
  "contactId": "abc123-def456",
  "timestamp": "2025-09-20T14:32:10.123456+00:00",
  "date": "2025-09-20",
  "time": "14:32:10",
  "callingNumber": "+14375550100",
  "firstName": "John",
  "lastName": "Smith",
  "fullName": "John Smith",
  "email": "john.smith@example.com",
  "callbackNumber": "+14375550100",
  "reasonForCalling": "Need help with tax filing"
}
```

---

## 4. Step 2 — Create the Lex V2 Bot

Go to `Amazon Lex > Bots > Create bot > Create blank bot`.

- Name: `CallerInfoVoiceAgent`
- IAM role: `Create a role with basic Amazon Lex permissions`
- COPPA: `No`
- Session timeout: `5 min`
- Create. Add language `English (US) en_US`, Voice: `Joanna (Neural)` (Amazon Polly), NLU threshold `0.40`, Save.

### 4.1 Create intent `CollectCallerInfo`

1. In the bot page, click `en_US` locale, then `Intents > Add intent > Create intent`, name `CollectCallerInfo`.
2. Sample utterances (one per line):
   - `I need help`
   - `My name is {FirstName} {LastName}`
   - `Hi`
   - `Hello`
3. Under `Fulfillment`: leave open (Lambda handles it). Do not close yet.

> **Note:** Do NOT add `{ReasonForCalling}` to sample utterances — `AMAZON.FreeFormInput` slot type doesn't support it. Lex will ask for the reason via the slot prompt.

### 4.2 Create slots (in this priority order)

In the intent page, click `Slots > Add slot`. Set `Prompt` exactly as below. Max retries `2`, Allow interrupt `checked`.

| # | Slot name | Slot type | Required? | Prompt |
|---|-----------|-----------|-----------|--------|
| 1 | `FirstName` | `AMAZON.FirstName` | Yes | `Thanks for calling iFirm. Please tell me your first name, exactly as it appears on your Canadian SIN card.` |
| 2 | `LastName` | `AMAZON.LastName` | Yes | `Thanks. Now, what is your last name, as on your SIN card?` |
| 3 | `Email` | `AMAZON.EmailAddress` | Yes | `Thanks. Please spell your email address slowly.` |
| 4 | `IsBestNumber` | custom `YesNoValues` (enumeration: yes/no + synonyms — Lex V2 has **no** `AMAZON.YesNo` built-in) | Yes | `Thanks. Is the number you are calling from the best number to reach you? Just say yes or no.` (overridden at runtime by Lambda to include ANI) |
| 5 | `CallbackNumber` | `AMAZON.PhoneNumber` | No (Optional — Lambda skips when Yes) | `No problem. What is the best phone number, including the area code, to reach you at?` |
| 6 | `ReasonForCalling` | `AMAZON.FreeFormInput` | Yes | `Got it. Briefly, what is the reason for your call today?` |
| 7 | `AppointmentDate` | `AMAZON.Date` | Yes | `What day works for you? You can say something like this Friday, or September 30th.` (past/vague dates re-asked; Mon–Fri only for booking) |
| 8 | `AppointmentTime` | `AMAZON.Time` | Yes | `What time works for you on that day?` (vague answers re-asked; busy times get a spoken alternative — say Yes to take it) |

> Note: these prompts are deployed automatically by `deploy.sh` — manual console edits get overwritten on the next `--only-lex` run.

> Privacy: do NOT create a slot for SIN number. Only names.

Set slot priority order 1→8 in `Slot priority` panel.

### 4.3 Enable confirmation

In the intent page, click `Confirmation`:

- Enable confirmation: `ON`
- Confirm prompt: `So to confirm: first name {FirstName}, last name {LastName}, email {Email}, best callback number {CallbackNumber}, calling about {ReasonForCalling}, on {AppointmentDate} at {AppointmentTime}. Is all of that correct? Say yes to confirm, or no to make changes.`
- Yes = confirmed (saves + closes). No = denied (restarts at FirstName). Never phrase it negatively ("need any changes?") — a human "No" then means "all correct" but Lex reads it as denial.
- Decline response: `Okay, let us update your details.`
- Max retries: `2`

This satisfies "confirm details and ask if changes are needed". On Deny, Lambda restarts at FirstName.

### 4.4 Attach Lambda code hooks

> Lex V2 allows **one** code-hook Lambda per bot alias locale, so both phases
> run in `connect-voice-agent-hook`: validation on `DialogCodeHook`, S3 saving
> on `FulfillmentCodeHook` (see `lambda/lex_hook.py: save_record`). The
> standalone `connect-caller-intake-saver` function is retained for reference
> but is not in the active path — do not attach a separate fulfillment Lambda.

1. In the intent page, click `Code hooks`:
   - Dialog code hook: `ON`, Lambda: `connect-voice-agent-hook`, version `$LATEST`.
   - Fulfillment code hook: `ON` (invokes the same Lambda; it saves + closes).
2. Save intent.

### 4.5 Build, version, alias

1. In the bot page, click `Build` (en_US) > wait for `Built`.
2. Test in Lex console (Test window) with session attribute `contactNumber: +14375550100`:
   - `Hi` → check each slot elicitation, Yes/No branch, confirmation Yes/No.
3. Click `Bot versions > Create version` (v1).
4. Click `Aliases > Create alias`: name `Prod`, associate version `v1`, language `en_US`.
5. Copy the **Bot alias ARN**, e.g. `arn:aws:lex:ca-central-1:…:bot-alias/XXXX/YYYY`. Needed for Connect.

---

## 5. Step 3 — Create the Connect Contact Flow

1. `Amazon Connect > Instance > Contact flows > Create contact flow > Import` (or create from scratch matching below).
2. Blocks in order:

   a. **Set logging behavior** — `Enable logging`.
    b. **Set voice** — `Tiffany, Generative, English US` (Amazon Polly, configured in `config.py`).
   c. **Set contact attributes** — `contactNumber = $.CustomerEndpoint.Address` (stores ANI for Lex).
    d. **Play prompt** — Welcome message (from `contact-flow.json`): `Hello, and thank you for calling iFirm. I am Monica and will collect a few quick details to get you started.`
    e. **Get customer input (Lex)**:
       - Lex V2 bot: `CallerInfoVoiceAgent`, Alias `Prod`, Language `en_US`, Timeout e.g. `15s`.
       - Session attributes: `contactNumber = $.Attributes.contactNumber`.
       - Prompt: Text-to-speech with the opening permission check (`I will ask some questions to gather basic information. Is that okay?`) — the caller's Yes/Okay triggers the intent (affirmation utterances are trained), then Lex asks for the name; the Lambda additionally harvests names from the raw transcript as a backstop against re-asking. Do NOT pick a library/music prompt (music loops forever). Leave the DTMF section untouched: this block is Lex-driven, and Lex + DTMF can't be combined in one block.
    f. **Check disconnect / error**: on Lex `Error/Timeout` → Play `Sorry, something went wrong. Please call us back in a few minutes so we can help you. Goodbye.` → Disconnect.
    g. **Play prompt** (on Success/Fulfilled): Text: `Thank you. Someone from iFirm will reach out to you. Have a nice day.`
    h. **Disconnect / hang up**.

3. `Save & Publish`. Copy Flow ARN.

### Importable flow JSON

Use `contact-flow.json` in the project root (validated against real exported
flows — correct `ConnectParticipantWithLexBot` action with `LexV2Bot.AliasArn`,
all branches wired). It ships with a placeholder alias ARN
(`arn:aws:lex:ca-central-1:123456789012:bot-alias/AAAAAAAAAA/BBBBBBBBBB`) —
`deploy.sh` prints your real Prod alias ARN at the end of every run; paste it
into the `AliasArn` before importing. After import, re-select the Lex
bot in the **Get customer input** block (grants invoke permission), then
**Save & Publish**. Importing creates a *new* flow — re-assign your phone
number to it afterwards.

> **Amazon Polly Configuration**: Voice ID, engine type, and language are configured in `config.py` (`POLLY_VOICE_ID`, `POLLY_ENGINE`, `POLLY_LANGUAGE_CODE`). Update these values before deploying the Connect flow.

> In console-built flows, `Get customer input` auto-stores `$.Lex.Slots.FirstName`, `LastName`, `Email`, `CallbackNumber`, `ReasonForCalling`, and `$.Lex.SessionAttributes.finalCallback`. Use these in a `Set contact attributes / Create Task / Invoke Lambda` block after Lex if you need to write to CRM.

### Required Lex + Connect IAM

Connect service role needs `lex:RecognizeText / RecognizeUtterance` on the bot alias. Console adds this automatically when you select the Lex bot in the `Get customer input` block. If using Terraform/CFN, attach:

```json
{"Effect":"Allow","Action":["lex:RecognizeText","lex:StartConversation"],"Resource":"<BOT_ALIAS_ARN>"}
```

---

## 6. Step 4 — Phone Number, Logging, Test

1. `Connect > Phone numbers > Claim a number` (or use existing) > Associate with the flow from Step 3.
2. `Connect > Contact flows > View flow logs`: enable CloudWatch logging; `Lex > Monitoring` for intent drop-off; `Lambda > Monitor` for errors.
3. Test calls:
   - Call 1: answer Yes to best-number → confirm `CallbackNumber == ANI`.
   - Call 2: answer No → give alternate 10-digit → confirm alternate is read back.
   - Call 3: give bad email → expect re-prompt; say No at confirmation → expect restart at FirstName; say Yes → expect closing prompt + disconnect.
4. Verify in `Connect > Contact search` that Lex slots are present.
5. Verify S3 save:
   - Go to `S3 > connect-caller-intake-XXXX > calls > YYYY > MM > DD/`.
   - Confirm JSON file exists with correct filename and contents.
   - Open the viewer app (Step 10) to see the record in the UI.

---

## 7. Call Script (what caller hears)

0. Flow: `Hello, and thank you for calling iFirm. I am Monica and will collect a few quick details to get you started.`
0. Flow → Lex handoff: `I will ask some questions to gather basic information. Is that okay?`
   - Yes / Okay / Sure (trained affirmations) → intake starts. Anything else may hit fallback → call ends, so answer affirmatively.
1. Bot: `Thanks for calling iFirm. Please tell me your first name, exactly as it appears on your Canadian SIN card.`
2. Bot: `Thanks. Now, what is your last name, as on your SIN card?`
3. Bot: `Thanks. Please spell your email address slowly.`
4. Bot: `I see you are calling from +1... Is this the best number to reach you? Say yes or no.` (ANI filled in at runtime)
   - Yes → skip. No → `No problem. What is the best phone number, including the area code, to reach you at?`
5. Bot: `Got it. Briefly, what is the reason for your call today?`
6. Bot: `What day works for you? You can say something like this Friday, or September 30th.`
7. Bot: `What time works for you on that day?`
   - Free → continue. Busy → `Sorry, <requested> is not available. The next opening is <suggestion>. Does that work for you? Say yes, or suggest another time.` → Yes books the suggestion.
8. Bot: `So to confirm: first name ..., last name ..., email ..., best callback number ..., calling about ..., on ... at .... Is all of that correct? Say yes to confirm, or no to make changes?`
   - Yes → book on Google Calendar + save + email + close. No → restart at 1.
9. Bot: `Thank you. Someone from iFirm will reach out to you. Have a nice day.` → hang up.

> Steps 0 use the flow's prompts (`contact-flow.json`); steps 1–9 use the Lex
> slot/confirmation/closing prompts. If the caller answers the opener with
> their name instead of Yes, the Lambda harvests it from the transcript and no
> question repeats.

### 7.1 Changing the questions

Every spoken line lives in exactly one place. Change it there, redeploy that
layer, re-test by phone. Console edits to Lex work for a quick try, but the
next `deploy.sh --only-lex` overwrites them — put lasting changes in the repo.

| # | What the caller hears | Where to change it | How to push it live |
|---|----------------------|--------------------|---------------------|
| 1 | Welcome line | `contact-flow.json` → welcome `MessageParticipant` `Text` (or the block in the designer) | Re-import flow (or edit block) → Save & Publish; re-point the number if it's a new flow |
| 2 | Opening permission question | `contact-flow.json` → Lex block `Text` (or the block's prompt in the designer) | Same as above |
| 3 | First / last name, email prompts | `deploy.sh` → the three `create_or_update_slot` prompt strings | `bash deploy.sh --only-lex` (rebuilds locale, republishes, moves `Prod`; alias ARN unchanged) |
| 4 | Best-number question | `lambda/lex_hook.py` (the `I see you are calling from …` f-string; ANI is runtime data) | `bash deploy.sh --only-lambdas` |
| 4 | Alternate-number prompt; all re-prompts (bad name/email/phone/reason, decline restart) | `config.py` `PROMPT_*` values | `bash deploy.sh --only-lambdas` (`config.py` ships inside the zip) |
| 5 | Reason prompt | `deploy.sh` → `ReasonForCalling` prompt string | `bash deploy.sh --only-lex` |
| 6 | Appointment day/time prompts + re-prompts | `config.py` `PROMPT_APPT_*` values | `bash deploy.sh --only-lambdas` (`config.py` ships inside the zip) |
| 7 | Confirmation + decline response | `deploy.sh` → `confirm_json` / decline text | `bash deploy.sh --only-lex` |
| 8 | Closing line | `config.py` `PROMPT_CLOSING` (Lambda fulfillment message) **and** `deploy.sh` `closing_json` (intent closing response) — keep them identical | `--only-lambdas` for the former, `--only-lex` for the latter (or full `bash deploy.sh`) |
| 9 | Business hours, slot length, calendar, timezone | `config.py` (`BUSINESS_HOURS_*`, `APPT_DURATION_MIN`, `CALENDAR_ID`, `TIMEZONE`) — `CALENDAR_ID` also as deploy env | `bash deploy.sh --only-lambdas` |

Rules of thumb:

- **Wording only?** Change the string, push that layer, test-call. No other file needs to touch.
- **New answer variants** (e.g. accept "yep" somewhere new)? Affirmation/negation sets live in `config.py` (`YES_ANSWERS`/`NO_ANSWERS`); intent-trigger phrases live in `deploy.sh` `--sample-utterances`. Add → push that layer.
- **A brand-new question (new slot)?** Bigger: add the slot type + `create_or_update_slot` call + priority entry in `deploy.sh`, read it in `lambda/lex_hook.py` (validation + include in `save_record` schema), confirm the viewer (`viewer/app.py`) displays it. Then full `bash deploy.sh`.
- **Never edit the live flow's Lex ARN by hand** to something untested — a stale ARN fails every call into the Error branch. Re-select bot/alias from the block dropdowns instead.

---

## 8. Optional: Terraform (IaC) Equivalent

Resources to create: `aws_lambda_function` (hook — validation + S3 save + SES email; saver retained as reference) + `aws_iam_role` (lex + lambda roles) + `aws_s3_bucket` + `aws_lexv2models_bot` + `aws_lexv2models_bot_locale` (voice `Tiffany`/`generative` by default) + `aws_lexv2models_intent` (`CollectCallerInfo` with `confirmation_setting`, `dialog_code_hook` + `fulfillment_code_hook` both invoking the hook Lambda — Lex allows one code hook per alias) + custom slot type `YesNoValues` + 8× `aws_lexv2models_slot` (contact + appointment) + `aws_lexv2models_bot_version` + `aws_lexv2models_bot_alias` (`Prod`) + `aws_connect_contact_flow` (import `contact-flow.json`). Always `Build` locale before versioning; alias must point at a numbered version, not `DRAFT`.

---

## 9. Troubleshooting

| Symptom | Fix |
|---------|-----|
| Lex never hears ANI | Check flow session attribute key is exactly `contactNumber` and Lambda reads `sessionAttributes.contactNumber`. |
| `CallbackNumber` always asked even on Yes | Lambda must `set_slot(CallbackNumber, ANI)` and `Delegate`; slot must be `Optional`. |
| Email keeps failing | Speak as `john dot smith at gmail dot com`; check Lambda regex; view CloudWatch. |
| Filler sounds ("ummm", "aahh") accepted as answers | Fixed in Lambda: fillers are rejected (name/reason re-prompted) and stripped before transcript harvesting. Tune via `config.py: FILLER_ROOTS`, then `bash deploy.sh --only-lambdas`. |
| Callers in background noise misheard | `deploy.sh` sets Lex VAD sensitivity `HighNoiseTolerance` (`MaximumNoiseTolerance` for very noisy sites via `LEX_VAD_SENSITIVITY`) + 3 elicitation retries per slot. Optional next step: Lex console → bot locale settings → Neural speech model (better in noise). Last line of defense is the confirmation gate — misheard records get rejected before saving. |
| Non-native accents misheard (Canadian/American/Indian English) | `deploy.sh` sets the Neural STT model (`LEX_SPEECH_MODEL`), which handles accents/natural speech far better than Standard. `en_US` covers US + Canadian English (no `en_CA` exists in Lex). For strong Indian accents add an `en_IN` locale as phase 2 — needs explicit routing (language menu or separate number), since accent can't be auto-detected. Sample utterances do NOT fix accents (acoustic, not phrasing). Confirmation gate + transcript harvesting backstop the rest. |
| Confirmation variables empty `{FirstName}` | Slot names case-sensitive; must match `FirstName/LastName/Email/CallbackNumber/ReasonForCalling`. |
| Lex access denied from Connect | Re-select bot in flow block to auto-grant, or add IAM above. |
| Lambda timeout | Increase to 15s; check VPC not attached unless needed. |
| No JSON in S3 after call | Check `connect-voice-agent-hook` Lambda CloudWatch logs (`Saved calls/...` line or traceback); verify S3 bucket env var; check IAM PutObject permission on the hook role. (Fulfillment is saved by the hook Lambda, not the saver.) |
| S3 key has wrong name | Lex sends lowercase slots; Lambda calls `.lower()` on first/last. If blank, Lex returned `null` for that slot — check slot resolution in Lex test console. |
| Viewer shows no records | Check AWS credentials in terminal (`aws sts get-caller-identity`); bucket name in `--bucket` flag; region matches S3 bucket region. |
| Viewer access denied | Ensure IAM user/role has `s3:GetObject`, `s3:ListBucket` on the bucket. |

---

## 10. Local Viewer Application

A Flask web app that reads all caller intake JSON records from S3 and lets you search, view, and download them locally. All settings (bucket, region, port) come from `config.py`.

### 10.1 Install dependencies

```bash
pip install flask boto3
```

Or if using the project's virtualenv:

```bash
pip install -e .
```

### 10.2 Configure

Edit `config.py` or set environment variables:

```bash
export S3_BUCKET="connect-caller-intake-YOUR-ACCOUNT-ID"
export AWS_REGION="ca-central-1"       # optional, defaults to ca-central-1
export VIEWER_PORT="5000"              # optional, defaults to 5000
```

The viewer reads from S3 using your local AWS credentials. Ensure one of:

- `~/.aws/credentials` configured via `aws configure`
- Environment variables: `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY`
- IAM role (if running on EC2/ECS)

The IAM user/role needs these S3 permissions on the bucket:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::connect-caller-intake-<ACCOUNT-ID>",
        "arn:aws:s3:::connect-caller-intake-<ACCOUNT-ID>/*"
      ]
    }
  ]
}
```

### 10.3 Run the viewer

```bash
# defaults from config.py
python viewer/app.py

# override bucket
python viewer/app.py --bucket connect-caller-intake-YOUR-ACCOUNT-ID

# override port
python viewer/app.py --port 8080
```

Options:

```
--bucket   S3 bucket name (required)
--port     Port to listen on (default: 5000)
--region   AWS region (default: ca-central-1)
--host     Host to bind (default: 0.0.0.0)
--debug    Flask debug mode
```

Open in browser: `http://localhost:5000`

### 10.4 Viewer features

| Feature | How |
|---------|-----|
| **List all records** | Table shows date, time, name, email, callback, reason with View/Download buttons |
| **Search** | Type any text (name, email, reason, date, phone) in the search bar — searches across all fields |
| **View detail** | Click "View" to see full record with all fields + raw JSON |
| **Download single** | Click "Download" on any record to save its JSON file |
| **Download all** | Click "Download All" to get every record as a JSON array |
| **Clear search** | Click "Clear" button next to search bar |

### 10.5 Viewer file structure

```
viewer/
  app.py                  # Flask application
  templates/
    index.html            # Record list with search
    detail.html           # Single record view + raw JSON
    error.html            # Error page
```

---

## 11. Amazon Polly Voice Configuration

The voice agent uses **Amazon Polly** for text-to-speech. Configure in `config.py` or via environment variables.

### Available Voices (en-US)

| Voice ID | Gender | Style | Engine |
|----------|--------|-------|--------|
| `Tiffany` | Female | Expressive, most human-like | Generative (default; also Nova Sonic–compatible) |
| `Joanna` | Female | Friendly, natural | Neural |
| `Matthew` | Male | Professional, calm | Neural / Generative |
| `Justin` | Male | Young, energetic | Neural |
| `Ivy` | Female | Child-like | Neural |
| `Kendra` | Female | Professional | Neural |
| `Kimberly` | Female | Young adult | Neural |

> `Tiffany` is Generative-only and is supported in `ca-central-1`.

### Engine Types

| Engine | Quality | Cost | Use Case |
|--------|---------|------|----------|
| `generative` | Most human-like, expressive | Highest | Default (required for `Tiffany`) |
| `neural` | Higher quality, more natural | Higher | Fallback voice engines |
| `standard` | Good quality | Lower | Cost-sensitive deployments |

### Amazon Nova 2 Sonic (speech-to-speech, optional upgrade)

For fully expressive dialog (the bot converts speech directly to speech via
`amazon.nova-2-sonic-v1:0` instead of Polly TTS), enable it per bot locale —
console only, `deploy.sh` cannot do this step:

1. Admin website → **Conversational AI → Bots** → `CallerInfoVoiceAgent` → **Configuration** → locale `en_US` → **Speech model → Edit** → Model type **Speech-to-Speech**, Voice provider **Amazon Nova Sonic** → Confirm.
2. If the locale shows unbuilt changes → **Build language**.
3. In the contact flow's **Set voice** block → Other settings → **Override speaking style → Generative**, Voice provider **Amazon**, Language matching the locale, Voice **`Tiffany`** (Nova Sonic–compatible) → **Save & Publish**.
4. Re-test by phone. If Nova Sonic is missing from the Voice provider list, check model access/region support for `amazon.nova-2-sonic-v1:0` in your account.

### Configuration

```bash
# Set voice via environment variables
export POLLY_VOICE_ID="Matthew"
export POLLY_ENGINE="neural"
export POLLY_LANGUAGE_CODE="en-US"
```

Or edit `config.py` directly:

```python
POLLY_VOICE_ID = "Joanna"      # Change to Matthew, Justin, etc.
POLLY_ENGINE = "neural"        # or "standard"
POLLY_LANGUAGE_CODE = "en-US"
```

### SSML Support

Enable SSML for more natural speech with pauses, emphasis, and pronunciation:

```python
POLLY_SSML_ENABLED = True  # in config.py
```

Example SSML in prompts:
```xml
<speak>Hello <break time="500ms"/> please tell me your first name.</speak>
```

### Cost Optimization

- Neural voices cost ~$16 per 1M characters (vs $4 for standard)
- For high-volume deployments, consider `standard` engine
- Use `<break>` SSML tags to add natural pauses without extra characters

---

## 12. Security Notes

- Collect name only, never SIN number. Name validation rejects digits.
- Do not log `Email`/`CallbackNumber` in plain text beyond Connect Contact Lens defaults; set Lex `slot obfuscation` for `Email` if required.
- Store PII only in ca-central-1; enable Connect encryption at rest + CloudTrail.
- S3 bucket: Block Public Access ON, enable versioning, encrypt at rest (SSE-S3 or SSE-KMS).
- Local viewer: only runs on localhost; do not expose port 5000 to the internet without auth.
- Rotate AWS access keys periodically; use IAM roles over long-lived keys.

---

## 13. Email notifications (SES)

After each confirmed call, the hook Lambda emails the captured details (all fields
in the message body; the full JSON stays in S3) to each configured **admin**
address — and, only if explicitly enabled, a friendly receipt to the
**caller** (callers are never emailed by default).
Sending is **fail-open**: mail errors are only logged, the call and the S3
record are unaffected.

1. **Verify the sender**: SES console (`ca-central-1`) → Verified identities →
   Create identity (email address) → click the verification link. While the
   account is in SES **sandbox**, every recipient must be verified too — for
   real use, request SES production access.
2. **Deploy with notifications on**:
   ```bash
   export NOTIFICATION_ENABLED="true"
   export NOTIFICATION_EMAIL="admin1@example.com,admin2@example.com"  # admins
   export SES_SENDER_EMAIL="sender@example.com"     # verified sender (required)
   # optional: export NOTIFICATION_CALLER_ENABLED="true"  # also email the caller
   bash deploy.sh --only-lambdas
   ```
   (`deploy.sh` grants `ses:SendEmail`/`SendRawEmail` and passes the env vars;
   without them, notifications stay silently disabled.)
3. **Verify**: test-call through Yes → S3 file appears → caller receipt +
   admin email arrive with all details in the body. If not, check hook CloudWatch
   logs for `Sent caller receipt` / `Sent intake email` vs `WARNING`.

---

## 14. Appointment booking (Google Calendar)

After contact details + reason, the bot asks for a preferred day and time,
checks a Google Calendar, and books on confirmation:

- **Day** (`AppointmentDate`, `AMAZON.Date`): specific dates within 60 days
  (`APPT_LOOKAHEAD_DAYS`); past/vague answers are re-asked.
- **Time** (`AppointmentTime`, `AMAZON.Time`): specific times (`MO/AF/EV/NI`
  answers are re-asked for a specific time).
- **Availability**: requested slot must be free and inside business hours
  (Mon–Fri 9:00–17:00 Toronto, `BUSINESS_HOURS_*`, 30-min `APPT_DURATION_MIN`).
  Busy times get a spoken next-opening offer — the caller says Yes to take it.
- **Confirmation** includes the appointment; on Yes the Lambda re-checks,
  inserts the Calendar event, saves the record (`appointmentStart/End/Status`,
  `calendarEventId`), then emails. Statuses: `booked`, `adjusted` (took the
  suggestion), `unchecked` (calendar unreachable/misconfigured — still saves),
  `booking_failed` (saved + flagged for manual follow-up), `no_availability`.
- **Timezone**: `America/Toronto` (`TIMEZONE`).
- The viewer lists appointments and links the Calendar event; search covers
  appointment fields automatically.

### 14.1 Google Calendar setup (console + CLI, one time)

1. **Google Cloud**: create/select a project → IAM → Service Accounts → create
   one (e.g. `ifirm-intake-booker`) → **Keys → Add key → JSON** → download.
   **Never commit this file** (it's git-ignored).
2. **Share the calendar**: in Google Calendar, share the target calendar with
   the service-account email → **Make changes to events**. Note the calendar ID
   (usually the calendar's email address).
3. **Upload the key to S3** (same private records bucket):
   ```bash
   aws s3 cp ~/Downloads/ifirm-intake-booker-*.json \
     s3://connect-caller-intake-<ACCOUNT-ID>/config/google-credentials.json \
     --region ca-central-1
   ```
4. **Deploy with the calendar ID**:
   ```bash
   export CALENDAR_ID="your-calendar-id@group.calendar.google.com"
   bash deploy.sh --only-lambdas   # env + google client vendored into the zip
   bash deploy.sh --only-lex       # 2 new slots, priorities, confirmation text
   ```
   Without `CALENDAR_ID`, dialog still collects date/time but booking stays
   `unchecked` (nothing breaks).
5. **Verify**: Lex console test → pick a free time → confirm Yes → event appears
   on the calendar + S3 record shows `"appointmentStatus": "booked"`. Then test
   a busy time → expect the spoken alternative → Yes → `adjusted`.
