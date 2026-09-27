#!/usr/bin/env bash
#
# deploy.sh — Deploy the Amazon Connect Caller Intake voice agent to AWS.
#
# What it does (idempotent, safe to re-run):
#   1. S3   — create + harden the records bucket (versioning, SSE-S3, block public access)
#   2. Lambda — package + create/update both functions:
#        - connect-voice-agent-hook   (Lex dialog code hook, validation)
#        - connect-caller-intake-saver (Lex fulfillment hook, S3 save)
#      Creates execution roles if missing, sets timeout/env, grants lexv2.amazonaws.com invoke.
#   3. Lex V2 — create (or reuse) bot `CallerInfoVoiceAgent`, locale en_US,
#      intent `CollectCallerInfo` + 8 slots, build locale, publish version,
#      create/update alias `Prod` wired to the dialog-hook Lambda.
#
# Connect contact flow + phone number remain manual (console) — the script
# prints the Lex Prod alias ARN you need for the flow (see README §5).
#
# Requirements: aws CLI v2, jq, zip, python3. Auth: `aws sts get-caller-identity` must work.
#
# Usage:
#   bash deploy.sh [--profile NAME] [--region ca-central-1] [--bucket NAME]
#                  [--skip-s3] [--skip-lambdas] [--skip-lex]
#                  [--only-s3] [--only-lambdas] [--only-lex]
#                  [--dry-run]
#
# Env overrides (same names as config.py):
#   AWS_REGION, S3_BUCKET, LEX_BOT_NAME, LEX_BOT_ALIAS, LEX_LOCALE,
#   LAMBDA_DIALOG_HOOK, LAMBDA_S3_SAVER, POLLY_VOICE_ID, POLLY_ENGINE
#
set -euo pipefail

# ─── Defaults (mirror config.py) ──────────────────────────────────────────
REGION="${AWS_REGION:-ca-central-1}"
BOT_NAME="${LEX_BOT_NAME:-CallerInfoVoiceAgent}"
BOT_ALIAS="${LEX_BOT_ALIAS:-Prod}"
LOCALE="${LEX_LOCALE:-en_US}"
HOOK_FN="${LAMBDA_DIALOG_HOOK:-connect-voice-agent-hook}"
SAVER_FN="${LAMBDA_S3_SAVER:-connect-caller-intake-saver}"
VOICE_ID="${POLLY_VOICE_ID:-Tiffany}"
VOICE_ENGINE="${POLLY_ENGINE:-generative}"
# VAD sensitivity for background noise: Default | HighNoiseTolerance | MaximumNoiseTolerance
LEX_VAD_SENSITIVITY="${LEX_VAD_SENSITIVITY:-HighNoiseTolerance}"
# STT model for accents/noise: Standard | Neural (Deepgram/Advanced need extra config — see README)
LEX_SPEECH_MODEL="${LEX_SPEECH_MODEL:-Neural}"
LAMBDA_RUNTIME="python3.12"
LAMBDA_TIMEOUT=15

PROFILE=""
BUCKET_OVERRIDE=""
DO_S3=1; DO_LAMBDAS=1; DO_LEX=1; DRY_RUN=0

usage() {
  echo "Usage: bash deploy.sh [--profile NAME] [--region ca-central-1] [--bucket NAME]"
  echo "         [--skip-s3] [--skip-lambdas] [--skip-lex]"
  echo "         [--only-s3] [--only-lambdas] [--only-lex]"
  echo "         [--dry-run] [-h/--help]"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --bucket) BUCKET_OVERRIDE="$2"; shift 2 ;;
    --skip-s3) DO_S3=0; shift ;;
    --skip-lambdas) DO_LAMBDAS=0; shift ;;
    --skip-lex) DO_LEX=0; shift ;;
    --only-s3) DO_S3=1; DO_LAMBDAS=0; DO_LEX=0; shift ;;
    --only-lambdas) DO_S3=0; DO_LAMBDAS=1; DO_LEX=0; shift ;;
    --only-lex) DO_S3=0; DO_LAMBDAS=0; DO_LEX=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown arg: $1 (see --help)" >&2; exit 2 ;;
  esac
done

# ─── Helpers ──────────────────────────────────────────────────────────────
# NOTE: log/ok/warn MUST go to stderr — ensure_* functions echo ARNs to
# stdout for command-substitution capture, and log noise would corrupt them.
log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*" >&2; }
ok()   { printf '\033[0;32m    ✓ %s\033[0m\n' "$*" >&2; }
warn() { printf '\033[0;33m    ! %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[0;31m    ✗ %s\033[0m\n' "$*" >&2; exit 1; }

retry() { # retry <tries> <delay-secs> <cmd...>: for IAM/Lambda eventual consistency
  local tries="$1" delay="$2"; shift 2
  local i
  for ((i = 1; i <= tries; i++)); do
    if "$@" >/dev/null; then return 0; fi
    if [[ "$i" -lt "$tries" ]]; then
      warn "attempt $i/$tries failed — retrying in ${delay}s"
      sleep "$delay"
    fi
  done
  return 1
}

AWS=(aws)
[[ -n "$PROFILE" ]] && AWS+=(--profile "$PROFILE")
AWS+=(--region "$REGION" --output json)
awsq() { # awsq <jmespath-query> <service-args...>: text output, empty on failure
  local q="$1"; shift
  "${AWS[@]}" "$@" --query "$q" --output text 2>/dev/null || true
}

need() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }
need aws; need jq; need zip; need python3

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -f "$PROJECT_ROOT/config.py" && -d "$PROJECT_ROOT/lambda" ]] \
  || die "run from project root (expected config.py + lambda/ beside deploy.sh)"

if [[ "$DRY_RUN" == "1" ]]; then
  warn "DRY-RUN mode: AWS mutating calls are echoed, not executed."
  run() { echo "  [dry-run] $*"; }
else
  run() { "$@"; }
fi

# ─── Identity ─────────────────────────────────────────────────────────────
log "Checking AWS identity ($REGION)"
ACCOUNT_ID="$(awsq Account sts get-caller-identity)"
if [[ -z "$ACCOUNT_ID" || "$ACCOUNT_ID" == "None" ]]; then
  warn "aws sts get-caller-identity failed; raw output:"
  "${AWS[@]}" sts get-caller-identity || true
  die "AWS auth failed — run 'aws configure' / 'aws sso login' first."
fi
ok "account $ACCOUNT_ID"

# Bucket: explicit flag > $S3_BUCKET env > account-suffixed default (globally unique)
if [[ -n "$BUCKET_OVERRIDE" ]]; then BUCKET="$BUCKET_OVERRIDE";
elif [[ -n "${S3_BUCKET:-}" ]]; then BUCKET="$S3_BUCKET";
else BUCKET="connect-caller-intake-${ACCOUNT_ID}"; fi
[[ "$BUCKET" == "connect-caller-intake" ]] && BUCKET="connect-caller-intake-${ACCOUNT_ID}"
ok "bucket: $BUCKET   region: $REGION"

# ─── Step 1: S3 ───────────────────────────────────────────────────────────
ensure_s3() {
  log "Step 1/3 — S3 bucket: $BUCKET"
  if "${AWS[@]}" s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
    ok "bucket exists"
  else
    log "creating bucket"
    if [[ "$REGION" == "us-east-1" ]]; then
      run "${AWS[@]}" s3api create-bucket --bucket "$BUCKET" >/dev/null
    else
      run "${AWS[@]}" s3api create-bucket --bucket "$BUCKET" \
        --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
    fi
    ok "bucket created"
  fi
  if [[ "$DRY_RUN" == "1" ]]; then return; fi
  "${AWS[@]}" s3api put-public-access-block --bucket "$BUCKET" \
    --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true >/dev/null
  "${AWS[@]}" s3api put-bucket-versioning --bucket "$BUCKET" \
    --versioning-configuration Status=Enabled >/dev/null
  "${AWS[@]}" s3api put-bucket-encryption --bucket "$BUCKET" \
    --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' >/dev/null
  ok "hardened (block-public-access, versioning, SSE-S3)"
}

# ─── Step 2: Lambdas ──────────────────────────────────────────────────────
build_zips() {
  log "Packaging Lambda zips"
  local deploy_dir="/tmp/lambda-deploy-$$"
  rm -rf "$deploy_dir"; mkdir -p "$deploy_dir"
  rm -f "$PROJECT_ROOT/lex-hook.zip" "$PROJECT_ROOT/s3-saver.zip"

  # Vendor the Google Calendar client: the Lambda runtime never includes it,
  # so always bundle it (local installs don't transfer).
  log "vendoring google-api-python-client into lex-hook.zip"
  python3 -m pip install --quiet --no-cache-dir --target "$deploy_dir" \
    google-api-python-client \
    || die "pip install failed — need python3 -m pip + network to vendor google-api-python-client"

  cp "$PROJECT_ROOT/config.py" "$deploy_dir/config.py"
  cp "$PROJECT_ROOT/lambda/lex_hook.py" "$deploy_dir/lambda_function.py"
  (cd "$deploy_dir" && zip -qr "$PROJECT_ROOT/lex-hook.zip" .) && ok "lex-hook.zip"
  rm -rf "$deploy_dir"; mkdir -p "$deploy_dir"

  cp "$PROJECT_ROOT/config.py" "$deploy_dir/config.py"
  cp "$PROJECT_ROOT/lambda/s3_saver.py" "$deploy_dir/lambda_function.py"
  (cd "$deploy_dir" && zip -qr "$PROJECT_ROOT/s3-saver.zip" .) && ok "s3-saver.zip"
  rm -rf "$deploy_dir"
  ls -lh "$PROJECT_ROOT/lex-hook.zip" "$PROJECT_ROOT/s3-saver.zip"
}

ensure_lambda_role() { # ensure_lambda_role <role-name> [s3-bucket-for-inline-policy]
  local role="$1" bucket="${2:-}"
  # NOTE: must use the raw AWS call here — awsq ends with `|| true`
  # (always exit 0), which would wrongly report a missing role as existing
  # and skip creation entirely.
  if "${AWS[@]}" iam get-role --role-name "$role" >/dev/null 2>&1; then
    ok "role exists: $role"
  else
    log "creating role $role"
    local trust; trust="$(mktemp)"
    cat >"$trust" <<'EOF'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}
EOF
    run "${AWS[@]}" iam create-role --role-name "$role" \
      --assume-role-policy-document "file://$trust" \
      --description "Caller intake voice agent Lambda execution role" >/dev/null
    rm -f "$trust"
    # Retry: a freshly created role may not be visible to subsequent
    # IAM calls yet (eventual consistency → NoSuchEntity).
    retry 5 10 run "${AWS[@]}" iam attach-role-policy --role-name "$role" \
      --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole \
      || die "could not attach execution policy to role $role"
    ok "role created + AWSLambdaBasicExecutionRole attached"
  fi
  if [[ -n "$bucket" && "$DRY_RUN" == "0" ]]; then
    local pol; pol="$(mktemp)"
    cat >"$pol" <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:PutObject","s3:GetObject","s3:ListBucket"],"Resource":["arn:aws:s3:::${bucket}","arn:aws:s3:::${bucket}/*"]},{"Effect":"Allow","Action":["ses:SendEmail","ses:SendRawEmail"],"Resource":"*"}]}
EOF
    retry 6 10 "${AWS[@]}" iam put-role-policy --role-name "$role" \
      --policy-name caller-intake-access --policy-document "file://$pol" \
      || die "could not attach inline access policy to role $role after retries"
    # Cleanup: previous runs used policy name s3-caller-intake-access.
    "${AWS[@]}" iam delete-role-policy --role-name "$role" \
      --policy-name s3-caller-intake-access >/dev/null 2>&1 || true
    rm -f "$pol"
    ok "inline S3+SES policy on $role"
  fi
  if [[ "$DRY_RUN" == "0" ]]; then
    # Wait until the role is readable before returning its ARN (IAM propagation).
    retry 6 10 "${AWS[@]}" iam get-role --role-name "$role" \
      || die "role $role not visible after creation; re-run the script"
  fi
  awsq Role.Arn iam get-role --role-name "$role"
}

deploy_lambda() { # deploy_lambda <fn-name> <zip> <role-arn> [ENV k=v ...]
  local fn="$1" zip="$2" role="$3"; shift 3
  local env_json=""
  if [[ $# -gt 0 ]]; then
    # Build real JSON (not CLI shorthand): values may be empty or contain
    # commas (e.g. comma-separated admin emails) — both break shorthand
    # parsing with "Expected: ',', received: 'EOF'".
    env_json="$(printf '%s\n' "$@" | jq -Rn \
      '[inputs | capture("(?<k>[^=]*)=(?<v>.*)") | {(.k): .v}] | add | {Variables: .}')"
  fi
  if "${AWS[@]}" lambda get-function --function-name "$fn" >/dev/null 2>&1; then
    log "updating code: $fn"
    if [[ "$DRY_RUN" == "0" ]]; then
      run "${AWS[@]}" lambda update-function-code --function-name "$fn" \
        --zip-file "fileb://$zip" >/dev/null
      run "${AWS[@]}" lambda wait function-updated --function-name "$fn"
      if [[ -n "$env_json" ]]; then
        run "${AWS[@]}" lambda update-function-configuration --function-name "$fn" \
          --runtime "$LAMBDA_RUNTIME" --timeout "$LAMBDA_TIMEOUT" \
          --environment "$env_json" >/dev/null
        run "${AWS[@]}" lambda wait function-updated --function-name "$fn"
      else
        run "${AWS[@]}" lambda update-function-configuration --function-name "$fn" \
          --runtime "$LAMBDA_RUNTIME" --timeout "$LAMBDA_TIMEOUT" >/dev/null
        run "${AWS[@]}" lambda wait function-updated --function-name "$fn"
      fi
    fi
    ok "updated $fn"
  else
    log "creating function: $fn"
    # Fail fast: a malformed ARN (e.g. "None" from a failed lookup) will never
    # pass validation, so don't burn 5 retries on it.
    [[ "$role" == arn:aws:iam::*:role/* ]] \
      || die "cannot create $fn: invalid role ARN [$role] — role lookup failed, re-run"
    local args=(lambda create-function --function-name "$fn"
      --runtime "$LAMBDA_RUNTIME" --handler lambda_function.lambda_handler
      --role "$role" --architectures x86_64 --timeout "$LAMBDA_TIMEOUT"
      --zip-file "fileb://$zip")
    [[ -n "$env_json" ]] && args+=(--environment "$env_json")
    # Retry: Lambda may not be able to assume a freshly created role yet.
    retry 5 15 run "${AWS[@]}" "${args[@]}" \
      || die "could not create function $fn — re-run the script"
    ok "created $fn"
  fi
}

allow_lex_invoke() { # allow_lex_invoke <fn-name>
  local fn="$1"
  if [[ "$DRY_RUN" == "1" ]]; then run aws lambda add-permission --function-name "$fn" --statement-id lex-invoke; return; fi
  "${AWS[@]}" lambda add-permission --function-name "$fn" --statement-id lex-invoke \
    --action lambda:InvokeFunction --principal lexv2.amazonaws.com \
    --source-arn "arn:aws:lex:${REGION}:${ACCOUNT_ID}:bot-alias/*/*" >/dev/null 2>&1 \
    && ok "Lex invoke permission on $fn" \
    || ok "Lex invoke permission already present on $fn"
}

ensure_function_s3_access() { # ensure_function_s3_access <fn-name> <bucket>
  local fn="$1" bucket="$2" role_arn role_name
  [[ "$DRY_RUN" == "1" ]] && return
  role_arn="$(awsq Role lambda get-function --function-name "$fn")"
  role_name="${role_arn##*/}"
  if [[ -z "$role_name" || "$role_name" == "None" ]]; then
    warn "could not resolve execution role for $fn — skipping S3 policy"
    return
  fi
  local pol; pol="$(mktemp)"
  cat >"$pol" <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:PutObject","s3:GetObject","s3:ListBucket"],"Resource":["arn:aws:s3:::${bucket}","arn:aws:s3:::${bucket}/*"]},{"Effect":"Allow","Action":["ses:SendEmail","ses:SendRawEmail"],"Resource":"*"}]}
EOF
  retry 6 10 "${AWS[@]}" iam put-role-policy --role-name "$role_name" \
    --policy-name caller-intake-access --policy-document "file://$pol" \
    || die "could not attach inline access policy to $role_name (used by $fn)"
  # Cleanup: previous runs used policy name s3-caller-intake-access.
  "${AWS[@]}" iam delete-role-policy --role-name "$role_name" \
    --policy-name s3-caller-intake-access >/dev/null 2>&1 || true
  rm -f "$pol"
  ok "inline S3+SES policy on $role_name (execution role of $fn)"
}

deploy_lambdas() {
  log "Step 2/3 — Lambda functions"
  build_zips
  local hook_role saver_role hook_arn saver_arn
  hook_role="connect-voice-agent-hook-role"
  saver_role="connect-caller-intake-saver-role"
  hook_arn="$(ensure_lambda_role "$hook_role" "$BUCKET")"
  saver_arn="$(ensure_lambda_role "$saver_role" "$BUCKET")"
  [[ "$DRY_RUN" == "0" && -z "$hook_arn" ]] && die "could not resolve role ARN for $hook_role"
  # NOTE: never set AWS_REGION on the function — Lambda reserves that key
  # (InvalidParameterValueException). The runtime provides AWS_REGION
  # automatically and config.py reads it via os.environ.get().
  deploy_lambda "$HOOK_FN" "$PROJECT_ROOT/lex-hook.zip" "$hook_arn" "S3_BUCKET=$BUCKET" \
    "NOTIFICATION_ENABLED=${NOTIFICATION_ENABLED:-false}" \
    "NOTIFICATION_EMAIL=${NOTIFICATION_EMAIL:-}" \
    "NOTIFICATION_CALLER_ENABLED=${NOTIFICATION_CALLER_ENABLED:-false}" \
    "SES_SENDER_EMAIL=${SES_SENDER_EMAIL:-}" \
    "CALENDAR_ID=${CALENDAR_ID:-}"
  deploy_lambda "$SAVER_FN" "$PROJECT_ROOT/s3-saver.zip" "$saver_arn" "S3_BUCKET=$BUCKET"
  # Functions created in the console use auto-generated roles, which the update
  # path never changes — so attach the S3 policy to each function's ACTUAL
  # configured role, not just the script-managed one. Without this, PutObject
  # fails with AccessDenied and no record is ever saved.
  ensure_function_s3_access "$HOOK_FN" "$BUCKET"
  ensure_function_s3_access "$SAVER_FN" "$BUCKET"
  allow_lex_invoke "$HOOK_FN"
  allow_lex_invoke "$SAVER_FN"
  HOOK_FN_ARN="$(awsq Configuration.FunctionArn lambda get-function --function-name "$HOOK_FN")"
  SAVER_FN_ARN="$(awsq Configuration.FunctionArn lambda get-function --function-name "$SAVER_FN")"
  ok "hook:  ${HOOK_FN_ARN:-$HOOK_FN}"
  ok "saver: ${SAVER_FN_ARN:-$SAVER_FN}"
}

# ─── Step 3: Lex V2 ───────────────────────────────────────────────────────
ensure_lex_role() {
  local role="LexV2CallerIntakeRole"
  # NOTE: must use the raw AWS call here — awsq ends with `|| true`
  # (always exit 0), which would wrongly report a missing role as existing
  # and skip creation entirely.
  if "${AWS[@]}" iam get-role --role-name "$role" >/dev/null 2>&1; then
    ok "lex role exists: $role"
  else
    log "creating Lex service role: $role"
    local trust; trust="$(mktemp)"
    cat >"$trust" <<'EOF'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lexv2.amazonaws.com"},"Action":"sts:AssumeRole"}]}
EOF
    run "${AWS[@]}" iam create-role --role-name "$role" \
      --assume-role-policy-document "file://$trust" \
      --description "Lex V2 bot runtime role for caller intake" >/dev/null
    rm -f "$trust"
    # Lex needs Polly for voice + CloudWatch for logs.
    run "${AWS[@]}" iam attach-role-policy --role-name "$role" \
      --policy-arn arn:aws:iam::aws:policy/AmazonPollyReadOnlyAccess >/dev/null
    run "${AWS[@]}" iam attach-role-policy --role-name "$role" \
      --policy-arn arn:aws:iam::aws:policy/CloudWatchLogsFullAccess >/dev/null
    [[ "$DRY_RUN" == "0" ]] && sleep 10
    ok "lex role created"
  fi
  awsq Role.Arn iam get-role --role-name "$role"
}

deploy_lex() {
  log "Step 3/3 — Lex V2 bot: $BOT_NAME (locale $LOCALE)"
  [[ "$DO_LAMBDAS" == "0" && -z "${HOOK_FN_ARN:-}" ]] && warn "lambdas skipped — alias will be wired without code-hook; attach manually later."
  local lex_role_arn; lex_role_arn="$(ensure_lex_role)"

  # — Bot (reuse by name if present) —
  local bot_id
  # NOTE: filter client-side via JMESPath, not --filters (the BotFilterName
  # enum is `BotName`, and a misspelled server-side filter fails silently
  # through awsq and makes an existing bot look absent).
  bot_id="$(awsq "botSummaries[?botName=='$BOT_NAME'].botId | [0]" lexv2-models list-bots)"
  if [[ -z "$bot_id" || "$bot_id" == "None" ]]; then
    log "creating bot $BOT_NAME"
    if [[ "$DRY_RUN" == "1" ]]; then bot_id="DRYBOTID01"; run aws lexv2-models create-bot --bot-name "$BOT_NAME";
    else
      bot_id="$("${AWS[@]}" lexv2-models create-bot --bot-name "$BOT_NAME" \
        --role-arn "$lex_role_arn" --data-privacy childDirected=false \
        --idle-session-ttl-in-seconds 300 \
        --description "Caller intake voice agent (SIN name, email, callback, reason)" \
        --query botId --output text)"
    fi
    ok "bot created: $bot_id"
  else
    ok "bot exists: $BOT_NAME ($bot_id)"
  fi
  [[ "$DRY_RUN" == "1" ]] && { ok "dry-run: stopping before locale/intent creation"; return; }

  # wait until bot Available
  for _ in $(seq 1 30); do
    s="$(awsq botStatus lexv2-models describe-bot --bot-id "$bot_id")"
    [[ "$s" == "Available" ]] && break
    sleep 5
  done

  # — Locale (create or refresh voice settings) —
  if "${AWS[@]}" lexv2-models describe-bot-locale --bot-id "$bot_id" \
      --bot-version DRAFT --locale-id "$LOCALE" >/dev/null 2>&1; then
    log "refreshing locale $LOCALE (voice $VOICE_ID/$VOICE_ENGINE)"
    "${AWS[@]}" lexv2-models update-bot-locale --bot-id "$bot_id" --bot-version DRAFT \
      --locale-id "$LOCALE" --nlu-intent-confidence-threshold 0.40 \
      --voice-settings "{\"voiceId\":\"$VOICE_ID\",\"engine\":\"$VOICE_ENGINE\"}" \
      --description "English US voice locale" >/dev/null \
      || die "could not update locale $LOCALE (voice $VOICE_ID/$VOICE_ENGINE) — see AWS error above"
    ok "locale voice refreshed: $VOICE_ID/$VOICE_ENGINE"
  else
    log "creating locale $LOCALE (voice $VOICE_ID/$VOICE_ENGINE)"
    "${AWS[@]}" lexv2-models create-bot-locale --bot-id "$bot_id" --bot-version DRAFT \
      --locale-id "$LOCALE" --nlu-intent-confidence-threshold 0.40 \
      --voice-settings "{\"voiceId\":\"$VOICE_ID\",\"engine\":\"$VOICE_ENGINE\"}" \
      --description "English US voice locale" >/dev/null
    ok "locale created"
  fi

  # — VAD sensitivity + Neural STT model (best-effort) —
  # HighNoiseTolerance suits consistent moderate noise (traffic, offices);
  # MaximumNoiseTolerance is for very noisy sites. The Neural speech model
  # handles accents/natural speech far better than Standard. Both ride one
  # update call; newer API than the voice settings above, so never fail the
  # deploy over them: older CLIs/APIs just land in the warn branch
  # (Lex console > bot > locale settings covers both).
  if "${AWS[@]}" lexv2-models update-bot-locale --bot-id "$bot_id" --bot-version DRAFT \
      --locale-id "$LOCALE" --nlu-intent-confidence-threshold 0.40 \
      --speech-detection-sensitivity "$LEX_VAD_SENSITIVITY" \
      --speech-recognition-settings "speechModelPreference=$LEX_SPEECH_MODEL" >/dev/null 2>&1; then
    ok "VAD sensitivity: $LEX_VAD_SENSITIVITY; STT model: $LEX_SPEECH_MODEL"
  else
    warn "VAD/STT model not applied (unsupported CLI/API?) — Lex console > bot > locale settings if needed"
  fi

  # — Intent CollectCallerInfo —
  local intent_id
  intent_id="$(awsq "intentSummaries[?intentName=='CollectCallerInfo'].intentId | [0]" \
    lexv2-models list-intents --bot-id "$bot_id" --bot-version DRAFT --locale-id "$LOCALE")"
  local confirm_json closing_json
  confirm_json="$(mktemp)"; closing_json="$(mktemp)"
  cat >"$confirm_json" <<'EOF'
{"active":true,"promptSpecification":{"messageGroups":[{"message":{"plainTextMessage":{"value":"So to confirm: first name {FirstName}, last name {LastName}, email {Email}, best callback number {CallbackNumber}, calling about {ReasonForCalling}, on {AppointmentDate} at {AppointmentTime}. Is all of that correct? Say yes to confirm, or no to make changes."}}}],"maxRetries":2,"allowInterrupt":true},"declinationResponse":{"messageGroups":[{"message":{"plainTextMessage":{"value":"Okay, let us update your details."}}}],"allowInterrupt":true}}
EOF
  cat >"$closing_json" <<'EOF'
{"active":true,"closingResponse":{"messageGroups":[{"message":{"plainTextMessage":{"value":"Thank you. Someone from iFirm will reach out to you. Have a nice day."}}}],"allowInterrupt":true}}
EOF
  if [[ -z "$intent_id" || "$intent_id" == "None" ]]; then
    log "creating intent CollectCallerInfo"
    intent_id="$("${AWS[@]}" lexv2-models create-intent --intent-name CollectCallerInfo \
      --bot-id "$bot_id" --bot-version DRAFT --locale-id "$LOCALE" \
      --description "Collect caller name, email, callback number and reason" \
      --sample-utterances utterance="I need help" utterance="Hi" utterance="Hello" utterance="Yes" utterance="Okay" utterance="Sure" utterance="Go ahead" 'utterance="That is fine"' 'utterance="My name is {FirstName} {LastName}"' 'utterance="My first name is {FirstName}"' 'utterance="I would like to book an appointment"' \
      --dialog-code-hook enabled=true \
      --fulfillment-code-hook enabled=true \
      --intent-confirmation-setting "file://$confirm_json" \
      --intent-closing-setting "file://$closing_json" \
      --query intentId --output text)"
    ok "intent created: $intent_id"
  else
    log "updating intent CollectCallerInfo ($intent_id)"
    "${AWS[@]}" lexv2-models update-intent --intent-id "$intent_id" --intent-name CollectCallerInfo \
      --bot-id "$bot_id" --bot-version DRAFT --locale-id "$LOCALE" \
      --sample-utterances utterance="I need help" utterance="Hi" utterance="Hello" utterance="Yes" utterance="Okay" utterance="Sure" utterance="Go ahead" 'utterance="That is fine"' 'utterance="My name is {FirstName} {LastName}"' 'utterance="My first name is {FirstName}"' 'utterance="I would like to book an appointment"' \
      --dialog-code-hook enabled=true \
      --fulfillment-code-hook enabled=true \
      --intent-confirmation-setting "file://$confirm_json" \
      --intent-closing-setting "file://$closing_json" >/dev/null
    ok "intent updated"
  fi
  # NOTE: confirm/closing files are kept — the final intent write below
  # re-sends the FULL spec (UpdateIntent has replacement semantics).

  # — Remediation: placeholder 'NewIntent' with colliding utterances —
  # Console-created bots often contain a 'NewIntent' holding the same sample
  # utterances as ours, which fails the build ("utterance must be unique").
  # Remove it ONLY if it is fully redundant (no slots, no unique utterances).
  local newintent_id
  newintent_id="$(awsq "intentSummaries[?intentName=='NewIntent'].intentId | [0]" \
    lexv2-models list-intents --bot-id "$bot_id" --bot-version DRAFT --locale-id "$LOCALE")"
  if [[ -n "$newintent_id" && "$newintent_id" != "None" ]]; then
    log "found intent NewIntent ($newintent_id) — checking for utterance collision"
    local ni_desc ni_nslots
    ni_desc="$("${AWS[@]}" lexv2-models describe-intent --intent-id "$newintent_id" \
      --bot-id "$bot_id" --bot-version DRAFT --locale-id "$LOCALE")"
    ni_nslots="$(awsq 'length(slotSummaries)' lexv2-models list-slots \
      --bot-id "$bot_id" --bot-version DRAFT --locale-id "$LOCALE" --intent-id "$newintent_id")"
    # Show exactly what NewIntent holds, so a refusal is self-diagnosing.
    local ni_utter_list
    ni_utter_list="$(printf '%s' "$ni_desc" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print('(unreadable intent description)'); raise SystemExit
us = sorted({u.get('utterance', '') for u in (d.get('sampleUtterances') or []) if u.get('utterance')})
print('; '.join(us) if us else '(none)')")"
    warn "NewIntent state: slots=${ni_nslots:-unknown}; utterances: $ni_utter_list"
    if [[ "${ni_nslots:-}" == "0" ]] && printf '%s' "$ni_desc" | python3 -c "
import json, sys
ours = {'I need help', 'Hi', 'Hello', 'My name is {FirstName} {LastName}'}
d = json.load(sys.stdin)
theirs = {u.get('utterance', '') for u in (d.get('sampleUtterances') or [])}
sys.exit(0 if theirs and theirs <= ours else 1)"; then
      "${AWS[@]}" lexv2-models delete-intent --intent-id "$newintent_id" \
        --bot-id "$bot_id" --bot-version DRAFT --locale-id "$LOCALE" >/dev/null \
        || die "could not delete placeholder intent NewIntent"
      ok "removed placeholder intent NewIntent (duplicate utterances, no slots)"
    elif [[ "${ni_nslots:-}" == "0" ]]; then
      ok "NewIntent kept (no colliding utterances)"
    else
      die "intent NewIntent has its own slots/utterances colliding with CollectCallerInfo — rename or delete it in Lex console > $BOT_NAME, then re-run"
    fi
  fi

  # — Custom Yes/No slot type —
  # Lex V2 has NO AMAZON.YesNo built-in: using it fails CreateSlot with
  # PreconditionFailed "parent resource does not exist". Canonical values
  # yes/no match the Lambda's YES_ANSWERS/NO_ANSWERS normalization.
  local yesno_values; yesno_values="$(mktemp)"
  # NOTE: single-line JSON — like every other file:// payload in this script.
  cat >"$yesno_values" <<'EOF'
[{"sampleValue": {"value": "yes"}, "synonyms": [{"value": "yeah"}, {"value": "yep"}, {"value": "yup"}, {"value": "sure"}, {"value": "y"}]}, {"sampleValue": {"value": "no"}, "synonyms": [{"value": "nope"}, {"value": "nah"}, {"value": "n"}]}]
EOF
  local yesno_type_id
  yesno_type_id="$(awsq "slotTypeSummaries[?slotTypeName=='YesNoValues'].slotTypeId | [0]" \
    lexv2-models list-slot-types --bot-id "$bot_id" --bot-version DRAFT --locale-id "$LOCALE")"
  if [[ -z "$yesno_type_id" || "$yesno_type_id" == "None" ]]; then
    yesno_type_id="$("${AWS[@]}" lexv2-models create-slot-type --slot-type-name YesNoValues \
      --bot-id "$bot_id" --bot-version DRAFT --locale-id "$LOCALE" \
      --description "Yes/no answers (Lex V2 has no AMAZON.YesNo built-in)" \
      --slot-type-values "file://$yesno_values" \
      --value-selection-setting resolutionStrategy=TopResolution \
      --query slotTypeId --output text)" \
      || die "could not create custom slot type YesNoValues — see AWS error above"
    ok "slot type created: YesNoValues"
  else
    "${AWS[@]}" lexv2-models update-slot-type --slot-type-id "$yesno_type_id" \
      --slot-type-name YesNoValues \
      --bot-id "$bot_id" --bot-version DRAFT --locale-id "$LOCALE" \
      --description "Yes/no answers (Lex V2 has no AMAZON.YesNo built-in)" \
      --slot-type-values "file://$yesno_values" \
      --value-selection-setting resolutionStrategy=TopResolution >/dev/null \
      || die "could not update custom slot type YesNoValues — see AWS error above"
    ok "slot type updated: YesNoValues"
  fi
  rm -f "$yesno_values"
  [[ -n "$yesno_type_id" && "$yesno_type_id" != "None" ]] \
    || die "empty slot type ID for YesNoValues — aborting"

  # — Slots (create or update, in README §4.2 order) —
  # NOTE: explicit `|| die` checks are required here — this function runs
  # inside command substitution, where set -e is ineffective, so a failed
  # AWS call would otherwise print a false "✓ created" and continue with
  # an empty slot ID (which later breaks the slot-priority update).
  # format: name|type|constraint|prompt
  create_or_update_slot() { # $1=name $2=type $3=Required|Optional $4=prompt
    local name="$1" type="$2" constraint="$3" prompt="$4"
    local sid elic prompt_json; elic="$(mktemp)"
    prompt_json="$(printf '%s' "$prompt" | jq -Rs .)"
    cat >"$elic" <<EOF
{"slotConstraint":"$constraint","promptSpecification":{"messageGroups":[{"message":{"plainTextMessage":{"value":$prompt_json}}}],"maxRetries":3,"allowInterrupt":true}}
EOF
    sid="$(awsq "slotSummaries[?slotName=='$name'].slotId | [0]" \
      lexv2-models list-slots --bot-id "$bot_id" --bot-version DRAFT \
      --locale-id "$LOCALE" --intent-id "$intent_id")"
    if [[ -z "$sid" || "$sid" == "None" ]]; then
      sid="$("${AWS[@]}" lexv2-models create-slot --slot-name "$name" \
        --bot-id "$bot_id" --bot-version DRAFT --locale-id "$LOCALE" --intent-id "$intent_id" \
        --slot-type-id "$type" --value-elicitation-setting "file://$elic" \
        --query slotId --output text)" \
        || die "could not create slot $name ($type) — see AWS error above"
      ok "slot created: $name ($type)"
    else
      "${AWS[@]}" lexv2-models update-slot --slot-id "$sid" --slot-name "$name" \
        --bot-id "$bot_id" --bot-version DRAFT --locale-id "$LOCALE" --intent-id "$intent_id" \
        --slot-type-id "$type" --value-elicitation-setting "file://$elic" >/dev/null \
        || die "could not update slot $name — see AWS error above"
      ok "slot updated: $name"
    fi
    rm -f "$elic"
    [[ -n "$sid" && "$sid" != "None" ]] || die "empty slot ID for $name — aborting"
    printf '%s' "$sid"
  }

  # — Remediation: stale IsBestNumber slot with the old invalid type —
  # An early run may have left a phantom IsBestNumber slot bound to the
  # non-existent AMAZON.YesNo type next to the good one, which fails the
  # build ("same slot name with different slot types"). Delete every slot
  # with that name so exactly one clean definition remains.
  local _dup
  for _dup in $("${AWS[@]}" lexv2-models list-slots --bot-id "$bot_id" --bot-version DRAFT \
      --locale-id "$LOCALE" --intent-id "$intent_id" \
      --query "slotSummaries[?slotName=='IsBestNumber'].slotId" --output text 2>/dev/null || true); do
    [[ -n "$_dup" && "$_dup" != "None" ]] || continue
    "${AWS[@]}" lexv2-models delete-slot --slot-id "$_dup" --bot-id "$bot_id" \
      --bot-version DRAFT --locale-id "$LOCALE" --intent-id "$intent_id" >/dev/null 2>&1 \
      && ok "removed stale IsBestNumber slot ($_dup)" \
      || warn "could not delete stale slot $_dup (continuing)"
  done

  # (capture slot IDs in priority order for the later update-intent call)
  SID_FIRST="$(create_or_update_slot FirstName AMAZON.FirstName Required \
    'Thanks for calling iFirm. Please tell me your first name, exactly as it appears on your Canadian SIN card.')"
  SID_LAST="$(create_or_update_slot LastName AMAZON.LastName Required \
    'Thanks. Now, what is your last name, as on your SIN card?')"
  SID_EMAIL="$(create_or_update_slot Email AMAZON.EmailAddress Required \
    'Thanks. Please spell your email address slowly.')"
  SID_ISBEST="$(create_or_update_slot IsBestNumber "$yesno_type_id" Required \
    'Thanks. Is the number you are calling from the best number to reach you? Just say yes or no.')"
  SID_CB="$(create_or_update_slot CallbackNumber AMAZON.PhoneNumber Optional \
    'No problem. What is the best phone number, including the area code, to reach you at?')"
  SID_REASON="$(create_or_update_slot ReasonForCalling AMAZON.FreeFormInput Required \
    'Got it. Briefly, what is the reason for your call today?')"
  SID_APPT_DATE="$(create_or_update_slot AppointmentDate AMAZON.Date Required \
    'What day works for you? You can say something like this Friday, or September 30th.')"
  SID_APPT_TIME="$(create_or_update_slot AppointmentTime AMAZON.Time Required \
    'What time works for you on that day?')"

  # Fail fast with a clear message instead of a cryptic length error below.
  for _s in "$SID_FIRST" "$SID_LAST" "$SID_EMAIL" "$SID_ISBEST" "$SID_CB" "$SID_REASON" "$SID_APPT_DATE" "$SID_APPT_TIME"; do
    [[ -n "$_s" && "$_s" != "None" ]] \
      || die "a slot ID is missing — aborting before slot-priority update"
  done

  # Final intent write: FULL spec (utterances, hooks, confirmation, closing
  # AND priorities 1→8). UpdateIntent has replacement semantics, so a
  # priorities-only update would wipe the other settings and can break the
  # locale build — this single write leaves the intent complete.
  "${AWS[@]}" lexv2-models update-intent --intent-id "$intent_id" --intent-name CollectCallerInfo \
    --bot-id "$bot_id" --bot-version DRAFT --locale-id "$LOCALE" \
    --description "Collect caller name, email, callback number, reason and appointment" \
    --sample-utterances utterance="I need help" utterance="Hi" utterance="Hello" utterance="Yes" utterance="Okay" utterance="Sure" utterance="Go ahead" 'utterance="That is fine"' 'utterance="My name is {FirstName} {LastName}"' 'utterance="My first name is {FirstName}"' 'utterance="I would like to book an appointment"' \
    --dialog-code-hook enabled=true \
    --fulfillment-code-hook enabled=true \
    --intent-confirmation-setting "file://$confirm_json" \
    --intent-closing-setting "file://$closing_json" \
    --slot-priorities priority=1,slotId="$SID_FIRST" priority=2,slotId="$SID_LAST" \
      priority=3,slotId="$SID_EMAIL" priority=4,slotId="$SID_ISBEST" \
      priority=5,slotId="$SID_CB" priority=6,slotId="$SID_REASON" \
      priority=7,slotId="$SID_APPT_DATE" priority=8,slotId="$SID_APPT_TIME" >/dev/null \
    || die "could not finalize intent CollectCallerInfo — see AWS error above"
  ok "intent finalized (hooks, confirmation, slot priorities 1-8)"
  rm -f "$confirm_json" "$closing_json"

  # — Build locale (one automatic retry: a fresh slot type can need time to propagate) —
  log "building bot locale (takes ~1-3 min)"
  local build_status="Failed" build_attempt
  for build_attempt in 1 2; do
    [[ "$build_attempt" == "2" ]] && log "retrying bot locale build (attempt 2/2 after 30s)"
    [[ "$build_attempt" == "2" ]] && sleep 30
    "${AWS[@]}" lexv2-models build-bot-locale --bot-id "$bot_id" \
      --bot-version DRAFT --locale-id "$LOCALE" >/dev/null \
      || die "could not start bot locale build — see AWS error above"
    for _ in $(seq 1 60); do
      build_status="$(awsq botLocaleStatus lexv2-models describe-bot-locale \
        --bot-id "$bot_id" --bot-version DRAFT --locale-id "$LOCALE")"
      [[ "$build_status" == "Built" || "$build_status" == "Failed" ]] && break
      sleep 10
    done
    [[ "$build_status" == "Built" ]] && break
    warn "bot locale build attempt $build_attempt/2 failed"
  done
  if [[ "$build_status" != "Built" ]]; then
    warn "latest locale status:"
    "${AWS[@]}" lexv2-models describe-bot-locale --bot-id "$bot_id" \
      --bot-version DRAFT --locale-id "$LOCALE" || true
    die "bot locale build failed — open Lex console > $BOT_NAME > $LOCALE for build details, fix, and re-run (resumes automatically)"
  fi
  ok "locale built"

  # — Version + alias —
  log "publishing version + alias $BOT_ALIAS"
  local version
  version="$("${AWS[@]}" lexv2-models create-bot-version --bot-id "$bot_id" \
    --bot-version-locale-specification "{\"$LOCALE\":{\"sourceBotVersion\":\"DRAFT\"}}" \
    --description "deploy.sh $(date -u +%F)" --query botVersion --output text)"
  ok "version published: $version"

  # A new version leaves the bot in Creating/Versioning state, during which
  # the alias call is rejected — wait until it returns to Available.
  local bot_state
  for _ in $(seq 1 30); do
    bot_state="$(awsq botStatus lexv2-models describe-bot --bot-id "$bot_id")"
    [[ "$bot_state" == "Available" ]] && break
    sleep 5
  done
  [[ "$bot_state" == "Available" ]] \
    || die "bot still in state [$bot_state] after versioning — wait a minute and re-run (resumes at the alias step)"

  # Resolve Lambda ARNs (deploy_lambdas sets these on full runs; look them
  # up when running --only-lex, where that step is skipped).
  if [[ -z "${HOOK_FN_ARN:-}" ]]; then
    HOOK_FN_ARN="$(awsq Configuration.FunctionArn lambda get-function --function-name "$HOOK_FN")"
  fi
  if [[ -z "${SAVER_FN_ARN:-}" ]]; then
    SAVER_FN_ARN="$(awsq Configuration.FunctionArn lambda get-function --function-name "$SAVER_FN")"
  fi
  # Alias code hook: always the dialog hook — it validates turns AND saves the
  # confirmed record (Lex allows one code-hook Lambda per alias).
  local alias_lambda="${HOOK_FN_ARN:-}"
  if [[ -z "${alias_lambda:-}" || "$alias_lambda" == "None" ]]; then
    warn "Lambda ARN unknown (lambdas skipped?) — creating alias without code hook."
    alias_lambda=""
  fi
  local locale_settings; locale_settings="$(mktemp)"
  if [[ -n "$alias_lambda" ]]; then
    cat >"$locale_settings" <<EOF
{"$LOCALE":{"enabled":true,"codeHookSpecification":{"lambdaCodeHook":{"lambdaARN":"$alias_lambda","codeHookInterfaceVersion":"1.0"}}}}
EOF
  else
    cat >"$locale_settings" <<EOF
{"$LOCALE":{"enabled":true}}
EOF
  fi
  local alias_id alias_arn
  alias_id="$(awsq "botAliasSummaries[?botAliasName=='$BOT_ALIAS'].botAliasId | [0]" \
    lexv2-models list-bot-aliases --bot-id "$bot_id")"
  if [[ -z "$alias_id" || "$alias_id" == "None" ]]; then
    "${AWS[@]}" lexv2-models create-bot-alias --bot-alias-name "$BOT_ALIAS" \
      --bot-id "$bot_id" --bot-version "$version" \
      --bot-alias-locale-settings "file://$locale_settings" \
      --description "Production alias (deploy.sh)" >/dev/null \
      || die "could not create alias $BOT_ALIAS — see AWS error above"
    ok "alias created: $BOT_ALIAS"
  else
    "${AWS[@]}" lexv2-models update-bot-alias --bot-alias-id "$alias_id" \
      --bot-alias-name "$BOT_ALIAS" --bot-id "$bot_id" --bot-version "$version" \
      --bot-alias-locale-settings "file://$locale_settings" \
      --description "Production alias (deploy.sh)" >/dev/null \
      || die "could not update alias $BOT_ALIAS — see AWS error above"
    ok "alias updated: $BOT_ALIAS -> v$version"
  fi
  # Wire the same code hook onto TestBotAlias (fixed ID TSTALIASID, always on
  # DRAFT): the Lex console Test window uses TestBotAlias, which otherwise has
  # no Lambda attached and every test fails with "Cannot call DialogCodeHook".
  "${AWS[@]}" lexv2-models update-bot-alias --bot-alias-id TSTALIASID \
    --bot-alias-name TestBotAlias --bot-id "$bot_id" --bot-version DRAFT \
    --bot-alias-locale-settings "file://$locale_settings" \
    --description "Test alias (deploy.sh)" >/dev/null \
    && ok "TestBotAlias wired (console Test window works)" \
    || warn "could not wire TestBotAlias (console tests will fail; Prod unaffected)"
  rm -f "$locale_settings"
  # NOTE: create/update responses don't reliably include IDs/ARNs — re-resolve
  # both by name so the printed ARN is never "None".
  alias_id="$(awsq "botAliasSummaries[?botAliasName=='$BOT_ALIAS'].botAliasId | [0]" \
    lexv2-models list-bot-aliases --bot-id "$bot_id")"
  [[ -n "$alias_id" && "$alias_id" != "None" ]] \
    || die "could not resolve alias ID for $BOT_ALIAS"
  # NOTE: describe-bot-alias returns no ARN field — construct it deterministically
  # (arn:aws:lex:<region>:<account>:bot-alias/<botId>/<aliasId)). The describe
  # call just verifies the alias is readable.
  "${AWS[@]}" lexv2-models describe-bot-alias --bot-id "$bot_id" \
    --bot-alias-id "$alias_id" >/dev/null \
    || die "could not read alias $BOT_ALIAS"
  alias_arn="arn:aws:lex:${REGION}:${ACCOUNT_ID}:bot-alias/${bot_id}/${alias_id}"

  # Allow Amazon Connect to invoke this alias. The console does this when you
  # pick the bot from the flow block's dropdown — but a pasted ARN never
  # triggers it, and without this grant every call fails into the Error
  # branch ("Sorry, something went wrong"). Service principals require
  # SourceAccount + SourceArn conditions, scoped to this Connect instance.
  # Verify first: a blind create masks real failures behind "already present".
  local connect_iid connect_cond
  connect_iid="${CONNECT_INSTANCE_ID:-$(awsq 'InstanceSummaryList[0].Id' connect list-instances)}"
  if [[ -z "$connect_iid" || "$connect_iid" == "None" ]]; then
    warn "no Connect instance found — skipping Connect invoke grant (set CONNECT_INSTANCE_ID or grant in the Lex console)"
  elif "${AWS[@]}" lexv2-models describe-resource-policy --resource-arn "$alias_arn" 2>/dev/null \
      | grep -q ConnectVoiceAccess; then
    ok "Connect invoke permission already present on alias $BOT_ALIAS"
  else
    connect_cond="$(jq -n --arg acct "$ACCOUNT_ID" --arg region "$REGION" --arg iid "$connect_iid" \
      '{StringEquals: {"aws:SourceAccount": $acct}, ArnLike: {"aws:SourceArn": ("arn:aws:connect:" + $region + ":" + $acct + ":instance/" + $iid)}}')"
    "${AWS[@]}" lexv2-models create-resource-policy-statement \
      --resource-arn "$alias_arn" --statement-id ConnectVoiceAccess --effect Allow \
      --principal service=connect.amazonaws.com \
      --action lex:RecognizeText lex:RecognizeUtterance lex:StartConversation \
        lex:GetSession lex:PutSession lex:DeleteSession \
      --condition "$connect_cond" >/dev/null \
      || die "could not grant Connect invoke permission on alias $BOT_ALIAS"
    ok "Connect invoke permission granted on alias $BOT_ALIAS"
  fi

  echo ""
  echo "Lex Prod alias ARN: $alias_arn"
  echo "(paste into the Connect 'Get customer input' block, README §5)"
  echo "Fulfillment note: Lex allows one code-hook Lambda per alias, so the"
  echo "dialog hook ($HOOK_FN) also saves the confirmed record to S3."
}

# ─── Main ─────────────────────────────────────────────────────────────────
[[ "$DO_S3" == "1" ]] && ensure_s3
[[ "$DO_LAMBDAS" == "1" ]] && deploy_lambdas
[[ "$DO_LEX" == "1" ]] && deploy_lex

log "Done."
echo "  bucket : $BUCKET"
[[ -n "${HOOK_FN_ARN:-}" ]] && echo "  hook   : $HOOK_FN_ARN"
[[ -n "${SAVER_FN_ARN:-}" ]] && echo "  saver  : $SAVER_FN_ARN"
echo ""
echo "Next (manual, README §5-6):"
echo "  1. Connect > Contact flows > import flow JSON, set BotAliasArn to the Lex Prod ARN above"
echo "  2. Claim/assign phone number to the flow, test Yes/No + bad-email + deny-confirm paths"
echo "  3. Viewer: python viewer/app.py --bucket $BUCKET"
