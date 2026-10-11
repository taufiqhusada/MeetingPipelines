"""Environment-driven settings. Every integration degrades to dry-run when its key is missing."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT.parent / ".env")
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
SEED_DIR = ROOT / "seed"
DB_PATH = Path(os.getenv("AFTER_DB_PATH", DATA_DIR / "after.db"))

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")

RECALL_API_KEY = os.getenv("RECALL_API_KEY", "")
RECALL_REGION = os.getenv("RECALL_REGION", "us-west-2")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "taufiqhusada/MeetingPipelines")
# Seed team handles are placeholders; only assign issues once they are real collaborators.
GITHUB_ASSIGN = os.getenv("GITHUB_ASSIGN", "0") == "1"

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "#meeting-agent")

GOOGLE_CREDENTIALS_FILE = Path(os.getenv("GOOGLE_CREDENTIALS_FILE", DATA_DIR / "google_credentials.json"))
GOOGLE_TOKEN_FILE = Path(os.getenv("GOOGLE_TOKEN_FILE", DATA_DIR / "google_token.json"))
TIMEZONE = os.getenv("TIMEZONE", "America/Los_Angeles")

DARWIN_API_KEY = os.getenv("DARWIN_API_KEY", "")  # optional: anonymous search works with a rate limit

BASE_URL = os.getenv("AFTER_BASE_URL", "http://localhost:8000")

# Global kill switch: when on, nothing leaves the machine (GitHub/Slack/Calendar are logged to the outbox).
DRY_RUN = os.getenv("DRY_RUN", "1") == "1"


def flags() -> list[dict]:
    """Missing setup the demo should surface instead of failing on."""
    out = []
    if not ANTHROPIC_API_KEY:
        out.append({"key": "ANTHROPIC_API_KEY", "impact": "Using canned demo analysis instead of Claude."})
    if not RECALL_API_KEY:
        out.append({"key": "RECALL_API_KEY", "impact": "Recall.ai bot disabled; use a pasted/uploaded transcript."})
    if not SLACK_BOT_TOKEN:
        out.append({"key": "SLACK_BOT_TOKEN", "impact": "Slack messages go to the outbox only (waiting on Slack teammate)."})
    if not GITHUB_TOKEN:
        out.append({"key": "GITHUB_TOKEN", "impact": "GitHub issues go to the outbox only."})
    if not GOOGLE_TOKEN_FILE.exists():
        out.append({"key": "Google Calendar", "impact": "Run `python -m after.google_setup`; until then meetings are .ics files."})
    if DRY_RUN:
        out.append({"key": "DRY_RUN=1", "impact": "Nothing is sent externally. Set DRY_RUN=0 to go live."})
    return out
