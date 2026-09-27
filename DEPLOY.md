# Deploy Guide — Caller Intake Voice Agent (`deploy.sh`)

This guide explains how to run `deploy.sh` to upload this project's code to AWS
and end up with a working solution:

```text
PSTN caller -> Amazon Connect (voice flow) -> Amazon Lex V2 (dialog)
  -> Lambda `connect-voice-agent-hook` (validation)
  -> Lambda `connect-voice-agent-hook` (validation + S3 save on fulfillment)
  -> S3 bucket `connect-caller-intake-<ACCOUNT_ID>/calls/...json`
```

The script automates **S3 + Lambda + Lex V2**. The **Connect contact flow +
phone number** remain manual console steps (the script prints the Lex alias ARN
you need for them).

Related docs: `README.md` (full manual console procedure), `config.py` (all
defaults), `build_lambdas.sh` (zip packaging only — `deploy.sh` includes this).

---

## 1. What `deploy.sh` does

The script is **idempotent** — safe to re-run. Existing resources are reused /
updated, not duplicated.

| Step | Resources | Details |
|------|-----------|---------|
| 1/3 S3 | `s3://connect-caller-intake-<ACCOUNT_ID>` | Create if missing (with correct `LocationConstraint`); then enforce block-public-access, versioning, SSE-S3 (`AES256`). |
| 2/3 Lambda | `connect-voice-agent-hook`, `connect-caller-intake-saver` | Build `lex-hook.zip` / `s3-saver.zip` from `lambda/*.py` + `config.py`; create roles `connect-voice-agent-hook-role` / `connect-caller-intake-saver-role` (with `AWSLambdaBasicExecutionRole` + inline `s3:PutObject/GetObject/ListBucket` on both — the hook saves records on fulfillment); create or update functions (Python 3.12, x86_64, timeout 15s, env `S3_BUCKET` — `AWS_REGION` is **not** set because Lambda reserves that key; the runtime provides it automatically); grant `lexv2.amazonaws.com` invoke on both. IAM calls use retry-with-backoff for eventual consistency. |
| 3/3 Lex V2 | Bot `CallerInfoVoiceAgent`, alias `Prod`, locale `en_US` | Create role `LexV2CallerIntakeRole` (Polly read + CloudWatch logs); create/reuse bot; create/reuse locale (NLU 0.40, voice Joanna/neural or `POLLY_*` env); create/update intent `CollectCallerInfo` (utterances, dialog + fulfillment hooks enabled, confirmation + closing prompts); custom slot type `YesNoValues` (Lex V2 has no `AMAZON.YesNo` built-in) + 8 slots (contact + `AppointmentDate/Time`) with priorities 1–8; `build-bot-locale` (waits for `Built`); publish version; create/update alias `Prod` wired to the dialog-hook Lambda. |

The script prints at the end:

```text
Lex Prod alias ARN: arn:aws:lex:ca-central-1:123456789012:bot-alias/XXXXXXXX/YYYYYYYY
```

You paste that ARN into the Connect flow (Section 6 below).

> Single-Lambda note: the Lex `Create/UpdateBotAlias` API supports **one**
> Lambda code hook per locale, so the dialog hook (`connect-voice-agent-hook`)
> handles both phases — slot validation on `DialogCodeHook` and S3 saving on
> `FulfillmentCodeHook`. No console attachment needed; the standalone
> `connect-caller-intake-saver` function is retained only as a reference.

---

## 2. Prerequisites

1. **Tools** (the script checks these and exits if missing):
   - `aws` CLI v2, `jq`, `zip`, `python3`
   - Install check: `aws --version && jq --version && zip -v | head -2 && python3 --version`

2. **AWS authentication** — one of:
   ```bash
   aws configure            # access key + secret + region
   # or
   aws sso login --profile my-profile
   ```
   Verify:
   ```bash
   aws sts get-caller-identity --region ca-central-1
   ```

3. **IAM permissions** for the deploying principal (admin is simplest; scoped
   minimum below):
   - `sts:GetCallerIdentity`
   - S3: `CreateBucket`, `PutPublicAccessBlock`, `PutBucketVersioning`, `PutBucketEncryption`, `HeadBucket`
   - IAM: `CreateRole`, `GetRole`, `AttachRolePolicy`, `PutRolePolicy`
   - Lambda: `CreateFunction`, `UpdateFunctionCode`, `UpdateFunctionConfiguration`, `GetFunction`, `AddPermission`
   - Lex V2 (`lexv2-models`): `ListBots`, `CreateBot`, `DescribeBot`, `CreateBotLocale`, `DescribeBotLocale`, `BuildBotLocale`, `ListIntents`, `DescribeIntent`, `CreateIntent`, `UpdateIntent`, `DeleteIntent`, `ListSlots`, `CreateSlot`, `UpdateSlot`, `DeleteSlot`, `ListSlotTypes`, `CreateSlotType`, `UpdateSlotType`, `CreateBotVersion`, `ListBotAliases`, `CreateBotAlias`, `UpdateBotAlias`, `DescribeBotAlias`, `CreateResourcePolicyStatement` (grants Connect invoke access on the alias)

4. **Region**: default `ca-central-1` (Canada, for data residency). Keep
   Connect, Lex, Lambda, and S3 in the **same region**.

5. **Amazon Connect instance**: must already exist (the script does not create
   it). Note its instance ARN if you need it later.

---

## 3. Configure (optional)

All defaults mirror `config.py`. You can override with env vars or flags:

| Setting | Env var | Flag | Default |
|---------|---------|------|---------|
| Region | `AWS_REGION` | `--region` | `ca-central-1` |
| Bucket | `S3_BUCKET` | `--bucket` | `connect-caller-intake-<ACCOUNT_ID>` |
| Bot name | `LEX_BOT_NAME` | — | `CallerInfoVoiceAgent` |
| Bot alias | `LEX_BOT_ALIAS` | — | `Prod` |
| Locale | `LEX_LOCALE` | — | `en_US` |
| Hook function | `LAMBDA_DIALOG_HOOK` | — | `connect-voice-agent-hook` |
| Saver function | `LAMBDA_S3_SAVER` | — | `connect-caller-intake-saver` |
| Polly voice | `POLLY_VOICE_ID` | — | `Tiffany` |
| Polly engine | `POLLY_ENGINE` | — | `generative` |
| VAD sensitivity (background noise) | `LEX_VAD_SENSITIVITY` | — | `HighNoiseTolerance` (`Default` \| `MaximumNoiseTolerance`) |
| STT model (accents/noise) | `LEX_SPEECH_MODEL` | — | `Neural` (`Standard`; `Deepgram`/`Advanced` need extra config) |
| Email notifications | `NOTIFICATION_ENABLED` | — | `false` (set `true` to email each record) |
| Admin recipients | `NOTIFICATION_EMAIL` | — | _(empty; comma-separated)_ |
| Caller receipt | `NOTIFICATION_CALLER_ENABLED` | — | `false` (callers never emailed unless `true`) |
| Email sender (verified) | `SES_SENDER_EMAIL` | — | _(empty = notifications skipped)_ |
| Booking calendar | `CALENDAR_ID` | — | _(empty = date/time collected, booking `unchecked`)_ |
| Booking timezone | `TIMEZONE` | — | `America/Toronto` |
| Slot length (min) | `APPOINTMENT_DURATION_MIN` | — | `30` |
| Booking window (days) | `APPOINTMENT_LOOKAHEAD_DAYS` | — | `60` |
| AWS profile | — | `--profile` | default profile |

Examples:

```bash
# Canada region (default), default bucket
export AWS_REGION="ca-central-1"

# Custom bucket + male voice
export S3_BUCKET="connect-caller-intake-123456789012"
export POLLY_VOICE_ID="Matthew"
export POLLY_ENGINE="neural"
```

---

## 4. Run the deployment

From the project root (the folder containing `deploy.sh`, `config.py`, `lambda/`):

```bash
cd /home/pravin/opencode_projects/027_AWS_iFirm_Opencode

# Full deploy: S3 + Lambdas + Lex
bash deploy.sh
```

Expected runtime: **3–8 minutes** (Lex locale build takes 1–3 min).

### Common variants

```bash
bash deploy.sh --help            # usage
bash deploy.sh --profile my-sso  # use a named AWS profile
bash deploy.sh --region ca-central-1 --bucket connect-caller-intake-123456789012

# Partial / iterative runs (script is idempotent)
bash deploy.sh --only-s3         # bucket only
bash deploy.sh --only-lambdas    # code update only (fastest iteration loop)
bash deploy.sh --only-lex        # bot only (requires Lambdas already deployed,
                                 # otherwise alias is created without code hook)
bash deploy.sh --skip-lex        # infra + code, no bot changes
```

### What success looks like

```text
==> Checking AWS identity (ca-central-1)
    ✓ account 123456789012
    ✓ bucket: connect-caller-intake-123456789012   region: ca-central-1
==> Step 1/3 — S3 bucket ...
    ✓ hardened (block-public-access, versioning, SSE-S3)
==> Step 2/3 — Lambda functions
    ✓ lex-hook.zip
    ✓ s3-saver.zip
    ✓ hook:  arn:aws:lambda:ca-central-1:123456789012:function:connect-voice-agent-hook
    ✓ saver: arn:aws:lambda:ca-central-1:123456789012:function:connect-caller-intake-saver
==> Step 3/3 — Lex V2 bot ...
    ✓ locale built
    ✓ version published: 3
    ✓ alias updated: Prod -> v3

Lex Prod alias ARN: arn:aws:lex:ca-central-1:123456789012:bot-alias/XXXXXXXX/YYYYYYYY
```

**Save the Lex Prod alias ARN** — you need it for the Connect flow next.

---

## 5. Verify the automated resources

```bash
REGION=ca-central-1
BUCKET=connect-caller-intake-$(aws sts get-caller-identity --query Account --output text)

# S3
aws s3api head-bucket --bucket "$BUCKET" --region "$REGION"
aws s3api get-bucket-versioning --bucket "$BUCKET"
aws s3api get-public-access-block --bucket "$BUCKET"

# Lambda
aws lambda get-function --function-name connect-voice-agent-hook --region "$REGION" \
  --query 'Configuration.[FunctionName,Runtime,Timeout,LastUpdateStatus]'
aws lambda get-function --function-name connect-caller-intake-saver --region "$REGION" \
  --query 'Configuration.[FunctionName,Runtime,Timeout,LastUpdateStatus]'

# Lex (BOT_ID from console or list-bots)
BOT_ID=$(aws lexv2-models list-bots --region "$REGION" \
  --filters name=BOT_NAME,values=CallerInfoVoiceAgent,operator=EQ \
  --query 'bots[0].botId' --output text)
aws lexv2-models describe-bot-locale --bot-id "$BOT_ID" \
  --bot-version DRAFT --locale-id en_US --region "$REGION" \
  --query 'botLocaleStatus'
aws lexv2-models list-bot-aliases --bot-id "$BOT_ID" --region "$REGION" \
  --query 'botAliasSummaries[].botAliasName'
```

Quick Lex dialog test (console): Lex → `CallerInfoVoiceAgent` → Test with
`Prod` alias → type `Hi` → walk through FirstName → LastName → Email →
IsBestNumber (Yes/No) → CallbackNumber → ReasonForCalling → confirm Yes.

---

## 6. Finish the working solution (manual: Connect flow + phone)

`deploy.sh` stops at the Lex alias. These console steps wire voice telephony
(full detail in `README.md` §5–6):

1. **No fulfillment wiring needed**: the dialog-hook Lambda saves the confirmed
   record to S3 itself on `FulfillmentCodeHook` (Lex allows one code-hook
   Lambda per alias). Skip any console fulfillment-Lambda attachment.
2. **Create the contact flow**: Connect → Contact flows → Create (or import):
   - Set logging: enabled
   - Set voice: Joanna / neural / en-US
   - Set contact attribute: `contactNumber = $.CustomerEndpoint.Address`
   - Play prompt (welcome): `Welcome. Thank you for calling. I will ask you a
     few questions to get started.`
   - Get customer input: Lex V2 bot `CallerInfoVoiceAgent`, alias `Prod`,
     locale `en_US`, session attribute `contactNumber = $.Attributes.contactNumber`
   - On error/timeout → Play `Sorry, we had trouble. Goodbye.` → Disconnect
   - On success → Play `Thank you calling. Someone will reach out to you.
     Have a nice day.` → Disconnect
   - Save & Publish. (A minimal importable JSON template is in `README.md` §5 —
     replace `REPLACE_WITH_LEX_PROD_ALIAS_ARN` with the ARN from Section 4.)
3. **Phone number**: Connect → Phone numbers → claim (or reuse) → associate
   with the flow.
4. **Test calls**:
   - Call 1: answer **Yes** to best-number → confirm callback == calling number.
   - Call 2: answer **No** → give alternate 10-digit number → confirm read-back.
   - Call 3: give a bad email (expect re-prompt); at confirmation say **No**
     (expect restart at FirstName), then **Yes** (expect closing prompt +
     disconnect).
5. **Verify S3 records**:
   ```bash
   aws s3 ls "s3://$BUCKET/calls/" --recursive --region "$REGION" | tail
   ```
   Or run the local viewer:
   ```bash
   pip install -e .            # or: pip install flask boto3
   python viewer/app.py --bucket "$BUCKET"
   # open http://localhost:5000
   ```

---

## 7. Iterate (code updates)

The normal dev loop after the first deploy is Lambda-only (30–60s):

```bash
# edit lambda/lex_hook.py or lambda/s3_saver.py (or config.py prompts)
bash deploy.sh --only-lambdas
```

If you changed prompts/slots/confirmations, also re-run the bot:

```bash
bash deploy.sh --only-lex   # rebuilds locale + publishes new version + moves Prod
```

---

## 8. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `AWS auth failed` | Run `aws configure` or `aws sso login`; verify `aws sts get-caller-identity`. With SSO use `--profile`. |
| `missing required tool: jq/zip` | `sudo apt install jq zip` (Debian/Ubuntu). |
| `BucketAlreadyExists` | Bucket name is global — pass a unique `--bucket` (default already suffixes account ID). |
| `Could not resolve role ARN` / IAM eventual consistency | Re-run the script — IAM calls now retry with backoff (5–6 attempts); a second run always converges. |
| `NoSuchEntity` on `PutRolePolicy` / role "cannot be found" | IAM propagation delay right after `CreateRole`. Fixed in-script with retries; just re-run. |
| `Reserved keys ... AWS_REGION` on `UpdateFunctionConfiguration` | Lambda forbids setting `AWS_REGION` (runtime provides it). Fixed — update `deploy.sh` and re-run; only `S3_BUCKET` is set now. |
| `bot locale build Failed` | Open Lex console → bot → locale → check intent/slot errors; fix and re-run `--only-lex`. The script now prints `failureReasons` automatically. Two common causes are auto-remediated: a stale `IsBestNumber` slot bound to the old invalid type (deleted + recreated) and a placeholder `NewIntent` with duplicate utterances (deleted only if it has no slots and no unique utterances). |
| `utterance must be unique across intents` (non-placeholder) | Some other intent genuinely uses the same utterances — rename or delete it in the Lex console, then re-run. |
| `locale build timed out` | Build can exceed 10 min in busy regions — re-run; the script resumes from the current DRAFT state. |
| Lex never hears ANI (`contactNumber`) | Flow session attribute key must be exactly `contactNumber`; Lambda reads `sessionAttributes.contactNumber` (README §9). |
| `CallbackNumber` always asked even on Yes | Slot must be **Optional** (script sets this); Lambda fills it from ANI and delegates. |
| No JSON in S3 after call | Check **hook** Lambda CloudWatch logs (`Saving record for…` / `Saved calls/…` lines or traceback); verify `S3_BUCKET` env on `connect-voice-agent-hook`. The script attaches the S3+SES policy to each function's **actual** execution role (console-created functions use auto-generated roles) — look for `inline S3+SES policy on … (execution role of …)` in deploy output. The hook saves on fulfillment — the saver function is not in the active path. |
| No notification email received | Email is fail-open by design — check hook logs for `Sent intake email` vs `WARNING: could not send`. Common causes: sender identity not verified in SES (`ca-central-1`); account in SES sandbox sending to an unverified recipient (verify it or request production access); `NOTIFICATION_EMAIL` empty. |
| Viewer shows no records | Check local creds (`aws sts get-caller-identity`), `--bucket` value, and region match; IAM needs `s3:GetObject` + `s3:ListBucket`. |
| `Unknown arg` | See `bash deploy.sh --help` for the exact flag list. |

---

## 9. Cleanup (optional)

To remove everything the script created (careful — deletes call records):

```bash
REGION=ca-central-1
BUCKET=connect-caller-intake-$(aws sts get-caller-identity --query Account --output text)
BOT_ID=$(aws lexv2-models list-bots --region "$REGION" \
  --filters name=BOT_NAME,values=CallerInfoVoiceAgent,operator=EQ \
  --query 'bots[0].botId' --output text)

aws lambda delete-function --function-name connect-voice-agent-hook --region "$REGION"
aws lambda delete-function --function-name connect-caller-intake-saver --region "$REGION"
aws lexv2-models delete-bot --bot-id "$BOT_ID" --region "$REGION"
aws s3 rm "s3://$BUCKET" --recursive --region "$REGION"
aws s3api delete-bucket --bucket "$BUCKET" --region "$REGION"
# Optional: delete roles connect-voice-agent-hook-role,
# connect-caller-intake-saver-role, LexV2CallerIntakeRole in IAM
# after detaching their policies.
```
