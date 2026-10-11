"""One pre-meeting run (gather → trim → summarize), shared by the CLI and the Slack bot."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import brief as brief_mod
from .config import settings
from .connectors.github import detect_repo
from .llm import LLM
from .models import RunScope
from .pipeline import SOURCES, gather, new_run_dir, write_run


def parse_since(value: str) -> datetime:
    if m := re.fullmatch(r"(\d+)([dwh])", value):
        n, unit = int(m.group(1)), m.group(2)
        delta = {"d": timedelta(days=n), "w": timedelta(weeks=n), "h": timedelta(hours=n)}[unit]
        return datetime.now(timezone.utc) - delta
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def make_scope(path: str = ".", since: str = "14d", repo: str | None = None, meeting: str | None = None,
               keywords: list[str] | None = None) -> RunScope:
    path = os.path.abspath(os.path.expanduser(path))
    return RunScope(since=parse_since(since), path=path, repo=repo or detect_repo(path),
                    meeting=meeting, keywords=keywords or [])


@dataclass
class RunResult:
    run_dir: Path
    status: dict
    index: dict
    brief: dict | None = None
    markdown: str | None = None
    title: str = ""
    meta: dict = field(default_factory=dict)


def prepare(scope: RunScope, sources: list[str] | None = None, llm_name: str | None = settings.llm,
            log: Callable[[str], None] = print, on_event: Callable[[dict], None] | None = None) -> RunResult:
    emit = on_event or (lambda _e: None)
    sources = sources or SOURCES
    llm = LLM(llm_name) if llm_name else None
    run_dir = new_run_dir()
    log(f"▶ Run {run_dir.name}  path={scope.path}  repo={scope.repo}  since={scope.since:%Y-%m-%d}"
        f"  llm={llm_name or 'none'}")

    log("① Gathering …")
    emit({"kind": "stage", "stage": "gathering"})
    results, status = gather(scope, sources, llm=llm, on_event=emit)
    for src in sources:
        st = status.get(src, {})
        detail = (f"{st.get('items')} items in {st.get('seconds')}s" if st.get("status") == "ok"
                  else st.get("reason", ""))
        log(f"   {src:<12} {st.get('status', '-'):<8} {detail[:200]}")

    log("② Trimming + redacting …")
    context_path, index = write_run(run_dir, scope, results, status)
    log(f"   context.md: {context_path.stat().st_size:,} bytes, {len(index)} items")
    result = RunResult(run_dir=run_dir, status=status, index=index,
                       title=f"Pre-meeting brief: {scope.meeting or scope.repo or Path(scope.path).name}")
    if llm is None or not index:
        emit({"kind": "stage", "stage": "done", "items": len(index)})
        return result

    log(f"③ Summarizing with {llm_name} …")
    emit({"kind": "stage", "stage": "summarizing", "items": len(index)})
    result.brief = brief_mod.summarize(context_path.read_text(), llm)
    (run_dir / "brief.json").write_text(json.dumps(result.brief, indent=2, ensure_ascii=False))
    result.markdown = brief_mod.to_markdown(result.brief, index, result.title)
    (run_dir / "brief.md").write_text(result.markdown)
    emit({"kind": "stage", "stage": "done", "items": len(index)})
    return result
