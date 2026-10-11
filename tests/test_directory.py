"""Print the Slack channel and user name-to-ID mappings."""

import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.slack.directory import get_workspace_mappings


if __name__ == "__main__":
    print(json.dumps(get_workspace_mappings(), indent=2, ensure_ascii=False))
