"""Send a test message and attachment to the configured Slack test channel."""

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.slack.messaging import send_message


if __name__ == "__main__":
    test_file = Path(__file__).parent / "artifacts" / "hi.txt"
    send_message(
        recipients=["test-bot-features"],
        text="hi",
        files=[test_file],
    )
