"""The one record type every connector produces."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


class SourceNotConfigured(RuntimeError):
    """Raised by a connector whose credentials aren't set up; the run marks it skipped, not failed."""


@dataclass
class ContextItem:
    id: str  # short citable id, e.g. "GH-PR-92", "CC-3f2a1b"
    source: str  # claude_code | codex | github | gmail | drive | local
    kind: str  # session | pr | issue | commit | doc | meeting
    title: str
    body: str
    created_at: datetime
    url: str | None = None
    author: str | None = None
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        return d


@dataclass
class RunScope:
    since: datetime
    path: str | None = None  # local repo path, used to match coding-agent sessions
    repo: str | None = None  # owner/name on GitHub
    meeting: str | None = None  # meeting title / topic, guides Gmail + Drive triage
    keywords: list[str] = field(default_factory=list)


def parse_ts(value: str | float | int | None) -> datetime | None:
    """Parse ISO strings / epoch seconds / epoch millis into aware UTC datetimes."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        secs = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(secs, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
