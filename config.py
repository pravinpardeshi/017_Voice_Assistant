"""
Centralized configuration for Amazon Connect Caller Intake solution.
All environment-specific values in one place. Override via env vars or command line.
"""
import os

# ─── AWS ──────────────────────────────────────────────────────────────
AWS_REGION = os.environ.get("AWS_REGION", "ca-central-1")
AWS_ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID", "")  # never hardcode: resolved via sts or env

# ─── S3 ───────────────────────────────────────────────────────────────
S3_BUCKET = os.environ.get("S3_BUCKET", "connect-caller-intake")
S3_PREFIX = "calls/"
S3_KEY_TIMESTAMP_FMT = "%y_%m_%d_%H_%M_%S"   # yy_mm_dd_hh_mm_ss
S3_RECORD_ID_LEN = 8

# ─── Amazon Connect ───────────────────────────────────────────────────
CONNECT_INSTANCE_ARN = os.environ.get("CONNECT_INSTANCE_ARN", "")
CONNECT_FLOW_NAME = "CallerIntakeFlow"

# ─── Amazon Polly Voice ────────────────────────────────────────────────
# Voice ID: Tiffany (US English female, Generative — default),
#           Joanna (US English female, Neural), Matthew, Justin, Ivy, ...
# Engine: "generative" (most human-like; required for Tiffany),
#         "neural" (higher quality) or "standard" (cheapest)
POLLY_VOICE_ID = os.environ.get("POLLY_VOICE_ID", "Tiffany")
POLLY_ENGINE = os.environ.get("POLLY_ENGINE", "generative")
POLLY_LANGUAGE_CODE = os.environ.get("POLLY_LANGUAGE_CODE", "en-US")
POLLY_OUTPUT_FORMAT = os.environ.get("POLLY_OUTPUT_FORMAT", "mp3")  # mp3, pcm, ogg_vorbis
POLLY_SAMPLE_RATE = os.environ.get("POLLY_SAMPLE_RATE", "8000")    # 8000 for telephony
POLLY_SSML_ENABLED = os.environ.get("POLLY_SSML_ENABLED", "true").lower() == "true"

# ─── Amazon Lex V2 ────────────────────────────────────────────────────
LEX_BOT_NAME = "CallerInfoVoiceAgent"
LEX_BOT_ALIAS = "Prod"
LEX_LOCALE = "en_US"
LEX_NLU_CONFIDENCE = 0.40
LEX_SESSION_TIMEOUT = 300          # seconds

# ─── Lambda ───────────────────────────────────────────────────────────
LAMBDA_DIALOG_HOOK = "connect-voice-agent-hook"
LAMBDA_S3_SAVER   = "connect-caller-intake-saver"
LAMBDA_TIMEOUT    = 15             # seconds

# ─── Lex slot names (must match bot definition exactly) ───────────────
SLOT_FIRST_NAME      = "FirstName"
SLOT_LAST_NAME       = "LastName"
SLOT_EMAIL           = "Email"
SLOT_IS_BEST_NUMBER  = "IsBestNumber"
SLOT_CALLBACK_NUMBER = "CallbackNumber"
SLOT_REASON          = "ReasonForCalling"

# ─── Validation ───────────────────────────────────────────────────────
EMAIL_PATTERN    = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
NAME_PATTERN     = r"^[A-Za-z' -]{2,50}$"
# Filled pauses ("ummm", "aahh", "hmmm", ...) are never valid answers. Stored
# as collapsed roots: repeated letters are squeezed first ("ummmm" -> "um"),
# so any elongation matches.
FILLER_ROOTS     = ("um", "uh", "ah", "eh", "er", "oh", "hm", "mm", "mhm")
MIN_REASON_LEN   = 3
NAME_MAX_LEN     = 50
MAX_RETRIES      = 2
PHONE_DIGITS_10  = 10
PHONE_DIGITS_11  = 11
YES_ANSWERS      = ("yes", "y", "yeah", "yep")
NO_ANSWERS       = ("no", "n", "nope")

# ─── Prompts ──────────────────────────────────────────────────────────
PROMPT_WELCOME          = "Welcome. Thank you for calling. I am your virtual assistant. Help me with some basic information to get started."
PROMPT_FIRST_NAME       = "Please tell me your first name exactly as it appears on your Canadian SIN card."
PROMPT_LAST_NAME        = "Thank you. Now your last name as on your SIN card?"
PROMPT_EMAIL            = "Please spell your email address slowly."
PROMPT_IS_BEST          = "Is the number you are calling from the best number to reach you? Say yes or no."
PROMPT_CALLBACK_NUMBER  = "Okay. What is the best phone number with area code to reach you?"
PROMPT_CALLBACK_INVALID = "Please repeat a valid 10 digit Canadian number."
PROMPT_REASON           = "Briefly tell me the reason for your call?"
PROMPT_CONFIRM          = "So to confirm: first name {FirstName}, last name {LastName}, email {Email}, best callback number {CallbackNumber}, calling about {ReasonForCalling}. Is all of that correct? Say yes to confirm, or no to make changes."
PROMPT_DECLINE          = "No problem, let's update it. Please tell me your first name exactly as on your Canadian SIN card."
PROMPT_INVALID_NAME     = "Please share only your name, not your SIN number. What is your first name as on your SIN card?"
PROMPT_INVALID_LAST     = "Please share only your last name as on your SIN card. What is your last name?"
PROMPT_INVALID_EMAIL    = "That email looks invalid. Please spell your email slowly."
PROMPT_MIN_REASON       = "Briefly tell me the reason for your call?"
PROMPT_CLOSING          = "Thank you. Someone from iFirm will reach out to you. Have a nice day."
PROMPT_ERROR            = "Sorry, we had trouble. Goodbye."

# ─── Viewer (Flask) ──────────────────────────────────────────────────
VIEWER_HOST = os.environ.get("VIEWER_HOST", "0.0.0.0")
VIEWER_PORT = int(os.environ.get("VIEWER_PORT", 5000))
VIEWER_DEBUG = os.environ.get("VIEWER_DEBUG", "false").lower() == "true"

# ─── Email notifications (SES) ─────────────────────────────────────────
# Sent by the hook Lambda after the S3 save: a copy to each admin address,
# and optionally a receipt to the caller (from the captured Email slot).
# Fail-open: mail errors are only logged, the call and the S3 record are
# unaffected. Requires a verified SES sender identity; while the account is
# in SES sandbox, every recipient must be verified too (or request SES
# production access).
NOTIFICATION_ENABLED = os.environ.get("NOTIFICATION_ENABLED", "false").lower() == "true"
NOTIFICATION_EMAIL = os.environ.get("NOTIFICATION_EMAIL", "")      # comma-separated admin recipients
NOTIFICATION_CALLER_ENABLED = os.environ.get("NOTIFICATION_CALLER_ENABLED", "false").lower() == "true"
SES_SENDER_EMAIL = os.environ.get("SES_SENDER_EMAIL", "")          # verified sender (required when enabled)
