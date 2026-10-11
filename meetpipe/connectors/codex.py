"""Codex sessions from ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings
from ..models import ContextItem, RunScope, parse_ts
from ._agentlog import SessionDigest, in_scope

_PATCH_FILE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.M)


def _text(content: list | None, kind: str) -> str:
    return "\n".join(c.get("text", "") for c in content or [] if c.get("type") == kind)


def _tool_input(payload: dict) -> str:
    raw = payload.get("input") or payload.get("arguments") or ""
    if isinstance(raw, str):
        try:  # function_call arguments are JSON: {"command": [...]} / {"cmd": "..."}
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return raw
    else:
        obj = raw
    if isinstance(obj, dict):
        cmd = obj.get("command") or obj.get("cmd") or obj.get("input") or ""
        return " ".join(cmd) if isinstance(cmd, list) else str(cmd)
    return str(obj)


def _parse(file: Path, scope: RunScope) -> tuple[str, SessionDigest] | None:
    s, sid = SessionDigest(), file.stem
    with file.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            p = rec.get("payload") or {}
            if rec.get("type") == "session_meta":
                sid = p.get("id") or sid
                s.cwd = p.get("cwd")
                if not in_scope(s.cwd, scope.path):
                    return None
                s.branch = (p.get("git") or {}).get("branch")
                continue
            if rec.get("type") != "response_item":
                continue
            ts = parse_ts(rec.get("timestamp"))
            if not ts or ts < scope.since:
                continue
            ptype = p.get("type")
            if ptype == "message" and p.get("role") == "user":
                s.touch(ts)
                text = _text(p.get("content"), "input_text")
                if not text.lstrip().startswith("<"):  # skip injected context blocks
                    s.user(text)
            elif ptype == "message" and p.get("role") == "assistant":
                s.touch(ts)
                s.assistant(_text(p.get("content"), "output_text"),
                            limit=300 if p.get("phase") == "commentary" else 1500)
            elif ptype in ("custom_tool_call", "function_call", "local_shell_call"):
                cmd = _tool_input(p)
                for path in _PATCH_FILE.findall(cmd):
                    s.file(path.strip())
                if "*** Begin Patch" not in cmd:
                    s.command(f"[{p.get('name', 'shell')}] {cmd}")
    return (sid, s) if s.turns else None


def fetch(scope: RunScope) -> list[ContextItem]:
    root = settings.codex_sessions_dir
    if not root.exists():
        return []
    cutoff = scope.since.timestamp()
    items = []
    for f in root.rglob("*.jsonl"):
        if f.stat().st_mtime < cutoff:
            continue
        parsed = _parse(f, scope)
        if not parsed:
            continue
        sid, s = parsed
        items.append(ContextItem(
            id=f"CX-{sid.replace('-', '')[-6:]}",
            source="codex",
            kind="session",
            title=s.title or "Codex session",
            body=s.render(),
            created_at=s.last or datetime.now(timezone.utc),
            meta={"session_id": sid, "cwd": s.cwd, "branch": s.branch,
                  "started": s.first.isoformat() if s.first else None, "files_edited": list(s.files)},
        ))
    return items
