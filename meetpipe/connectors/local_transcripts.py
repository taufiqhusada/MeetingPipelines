"""Transcript files dropped into ./transcripts (txt, md, vtt, srt)."""

from __future__ import annotations

from datetime import datetime, timezone

from ..config import settings
from ..models import ContextItem, RunScope
from .transcript_text import to_text

EXTS = {".txt", ".md", ".vtt", ".srt"}


def fetch(scope: RunScope) -> list[ContextItem]:
    root = settings.transcripts_dir
    if not root.exists():
        return []
    items = []
    for f in sorted(root.rglob("*")):
        if f.suffix.lower() not in EXTS or not f.is_file():
            continue
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        if mtime < scope.since:
            continue
        items.append(ContextItem(
            id=f"LT-{f.stem[:24]}", source="local", kind="meeting", title=f.name,
            body=to_text(f.name, f.read_text(encoding="utf-8", errors="replace")),
            created_at=mtime, url=None, meta={"path": str(f)},
        ))
    return items
