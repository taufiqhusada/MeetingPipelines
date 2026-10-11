"""Shared helpers for turning coding-agent transcripts into compact session digests."""

from __future__ import annotations

import os
import re
from datetime import datetime

_TAG_BLOCK = re.compile(r"<([a-zA-Z_][\w\-]*)(?:\s[^>]*)?>.*?</\1>", re.S)


def clean_user_text(text: str) -> str:
    """Drop harness-injected blocks (<system-reminder>, <environment_context>, ...)."""
    return _TAG_BLOCK.sub("", text or "").strip()


def in_scope(cwd: str | None, path: str | None) -> bool:
    if not path:
        return True
    if not cwd:
        return False
    cwd, path = os.path.realpath(cwd), os.path.realpath(path)
    return cwd == path or cwd.startswith(path.rstrip("/") + "/")


def clip(text: str, n: int) -> str:
    text = text.strip()
    return text if len(text) <= n else text[: n - 1] + "…"


class SessionDigest:
    """Accumulates the useful parts of one agent session."""

    def __init__(self) -> None:
        self.turns: list[str] = []
        self.files: dict[str, None] = {}  # ordered set
        self.commands: list[str] = []
        self.first: datetime | None = None
        self.last: datetime | None = None
        self.title: str | None = None
        self.cwd: str | None = None
        self.branch: str | None = None

    def touch(self, ts: datetime | None) -> None:
        if ts:
            self.first = min(self.first, ts) if self.first else ts
            self.last = max(self.last, ts) if self.last else ts

    def user(self, text: str) -> None:
        text = clean_user_text(text)
        if text:
            self.turns.append(f"USER: {clip(text, 1500)}")
            if not self.title:
                self.title = clip(text.splitlines()[0], 90)

    def assistant(self, text: str, limit: int = 1200) -> None:
        if text and text.strip():
            self.turns.append(f"AGENT: {clip(text, limit)}")

    def file(self, path: str) -> None:
        if path:
            self.files[path] = None

    def command(self, cmd: str) -> None:
        if cmd and len(self.commands) < 40:
            self.commands.append(clip(" ".join(cmd.split()), 160))

    def render(self, max_chars: int = 9000) -> str:
        parts = list(self.turns)
        if self.files:
            parts.append("FILES EDITED: " + ", ".join(list(self.files)[:40]))
        if self.commands:
            parts.append("COMMANDS RUN:\n" + "\n".join(f"  $ {c}" for c in self.commands))
        body = "\n\n".join(parts)
        if len(body) > max_chars:  # keep the opening ask and the latest state
            head = max_chars // 3
            body = body[:head] + "\n\n[… middle of session trimmed …]\n\n" + body[-(max_chars - head):]
        return body
