"""Compatibility entry point for the single MeetingPipelines Slack bot."""

import sys
from pathlib import Path

# Keep `python src/slack/app.py` working when launched from a checkout.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from meetpipe.slack_bot import serve


if __name__ == "__main__":
    serve()
