"""Claude Code sessions from ~/.claude/projects/<slug>/<session>.jsonl."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings
from ..models import ContextItem, RunScope, parse_ts
from ._agentlog import SessionDigest, in_scope

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def _slug(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(path))


def _candidate_files(scope: RunScope) -> list[Path]:
    root = settings.claude_projects_dir
    if not root.exists():
        return []
    dirs = [d for d in root.iterdir() if d.is_dir()]
    if scope.path:  # cheap pre-filter on the slugged directory name; cwd is re-checked per record
        slug = _slug(scope.path)
        dirs = [d for d in dirs if d.name == slug or d.name.startswith(slug + "-")]
    cutoff = scope.since.timestamp()
    return [f for d in dirs for f in d.glob("*.jsonl") if f.stat().st_mtime >= cutoff]


def _parse(file: Path, scope: RunScope) -> SessionDigest | None:
    s = SessionDigest()
    for line in file.open(encoding="utf-8", errors="replace"):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        rtype = rec.get("type")
        if rtype == "ai-title":
            s.title = rec.get("title") or rec.get("aiTitle") or s.title
            continue
        if rtype not in ("user", "assistant"):
            continue
        ts = parse_ts(rec.get("timestamp"))
        if not ts or ts < scope.since or not in_scope(rec.get("cwd"), scope.path):
            continue
        s.touch(ts)
        s.cwd = s.cwd or rec.get("cwd")
        s.branch = rec.get("gitBranch") or s.branch
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, str):
            (s.user if rtype == "user" else s.assistant)(content)
            continue
        for block in content or []:
            bt = block.get("type")
            if bt == "text":
                (s.user if rtype == "user" else s.assistant)(block.get("text", ""))
            elif bt == "tool_use":
                name, inp = block.get("name"), block.get("input") or {}
                if name in EDIT_TOOLS:
                    s.file(inp.get("file_path") or inp.get("notebook_path"))
                elif name == "Bash":
                    s.command(inp.get("command", ""))
    return s if s.turns else None


def fetch(scope: RunScope) -> list[ContextItem]:
    items = []
    for f in _candidate_files(scope):
        s = _parse(f, scope)
        if not s:
            continue
        sid = f.stem
        items.append(ContextItem(
            id=f"CC-{sid[:6]}",
            source="claude_code",
            kind="session",
            title=s.title or "Claude Code session",
            body=s.render(),
            created_at=s.last or datetime.now(timezone.utc),
            meta={"session_id": sid, "cwd": s.cwd, "branch": s.branch,
                  "started": s.first.isoformat() if s.first else None, "files_edited": list(s.files)},
        ))
    return items
