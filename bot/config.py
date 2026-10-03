import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _parse_user_ids(raw: str | None) -> frozenset[int]:
    if not raw:
        return frozenset()
    return frozenset(int(part.strip()) for part in raw.split(",") if part.strip())


def _parse_temperature(raw: str | None) -> float | None:
    # Unset/empty = don't send a temperature (gpt-5.x models reject it).
    raw = (raw or "").strip()
    return float(raw) if raw else None


def require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


# Not required at import, so the CLI scan and tests run without keys.
# main.py and llm.py call require() before using them.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Swappable without a code change, same as the sibling bots. gpt-5.5 because in
# testing gpt-4o summarised a 2,100-word script to ~870 words and gpt-4.1 left
# 14 of 15 repeats of a tic phrase; gpt-5.5 kept length and cleared them.
OPENAI_MODEL = os.getenv("OPENAI_MODEL") or "gpt-5.5"
# Only for models that accept it (e.g. 0.4 for gpt-4.1).
OPENAI_TEMPERATURE = _parse_temperature(os.getenv("OPENAI_TEMPERATURE"))
OPENAI_TIMEOUT_SECONDS = 300

# Comma-separated Telegram user IDs. Empty = anyone who finds the bot can use it
# (same as the sibling bots). When set, others are refused and told their ID.
ALLOWED_USER_IDS = _parse_user_ids(os.getenv("ALLOWED_USER_IDS"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Hardcoded by design.
MIN_INPUT_WORDS = 50
MAX_INPUT_CHARS = 120_000
MAX_UPLOAD_BYTES = 1_000_000
LENGTH_TOLERANCE = 0.10  # flag if the edit changes length by more than ±10%

# --- Google Doc mode ("go humanize") ------------------------------------------

# Doc mode is off unless a Doc ID is set.
GOOGLE_DOC_ID = os.getenv("GOOGLE_DOC_ID", "").strip()
# Reads and writes target this tab explicitly (the "tab=" value in the Doc URL).
GOOGLE_DOC_TAB_ID = (os.getenv("GOOGLE_DOC_TAB_ID") or "t.0").strip()
# Railway: paste the whole key JSON into GOOGLE_SERVICE_ACCOUNT_JSON.
# Local: point GOOGLE_SERVICE_ACCOUNT_FILE at the downloaded key.
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE") or "google-service-account.json"

# Dry run until explicitly switched on: the result is saved under STATE_DIR, the Doc is never written.
DOC_WRITE_ENABLED = os.getenv("DOC_WRITE_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}

# How emotion cues are written. "brackets" = [thoughtful]: ElevenLabs audio tags, used by
# Eleven v4 (the channel's model), v4 Turbo and v3. The emotion carries forward until the next tag.
# "parentheses" = (thoughtful), "none" = skip the emotion pass (genuine/genuinely still stripped).
CUE_STYLE = (os.getenv("CUE_STYLE") or "brackets").strip().lower()

# Tunable validation thresholds.
DOC_MIN_WORDS = int(os.getenv("DOC_MIN_WORDS") or 200)
DOC_LENGTH_TOLERANCE = float(os.getenv("DOC_LENGTH_TOLERANCE") or 0.15)  # final vs input word count
CUE_SIMILARITY_MIN = float(os.getenv("CUE_SIMILARITY_MIN") or 0.90)  # emotion output (cues removed) vs its input

# Where the last-input/last-output hashes and raw-script backups live. On Railway
# this must be a mounted volume, or they're lost on every restart/redeploy.
STATE_DIR = Path(os.getenv("STATE_DIR") or Path(__file__).resolve().parent.parent / "storage")
BACKUPS_KEEP = 3

# The old paste / .txt / /go / /cancel flow. Off = those reply with a pointer to the Doc.
LEGACY_TXT_FLOW = os.getenv("LEGACY_TXT_FLOW", "").strip().lower() in {"1", "true", "yes", "on"}
