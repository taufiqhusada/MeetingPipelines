"""Gather → trim → write the run folder."""

from __future__ import annotations

import json
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from .config import settings
from .connectors import claude_code, codex, drive, github, gmail, local_transcripts
from .google_auth import GoogleNotConnected
from .models import ContextItem, RunScope
from .redact import redact

SOURCES = ["claude_code", "codex", "github", "gmail", "drive", "local"]
LABELS = {"claude_code": "Claude Code sessions", "codex": "Codex sessions", "github": "GitHub",
          "gmail": "Gmail (meeting transcripts & notes)", "drive": "Google Drive", "local": "Local transcripts"}


def _timed(name, fn):
    t = time.time()
    try:
        items = fn()
        return name, items, {"status": "ok", "items": len(items), "seconds": round(time.time() - t, 1)}
    except GoogleNotConnected as e:
        return name, [], {"status": "skipped", "reason": str(e)}
    except Exception as e:
        return name, [], {"status": "error", "reason": f"{type(e).__name__}: {e}",
                          "trace": traceback.format_exc(limit=3)}


def gather(scope: RunScope, sources: list[str], llm=None) -> tuple[dict[str, list[ContextItem]], dict]:
    fetchers = {
        "claude_code": lambda: claude_code.fetch(scope),
        "codex": lambda: codex.fetch(scope),
        "github": lambda: github.fetch(scope),
        "gmail": lambda: gmail.fetch(scope),
        "local": lambda: local_transcripts.fetch(scope),
    }
    results, status = {}, {}
    with ThreadPoolExecutor(max_workers=len(fetchers)) as pool:
        for name, items, st in pool.map(lambda kv: _timed(*kv), [(n, f) for n, f in fetchers.items() if n in sources]):
            results[name], status[name] = items, st
    if "drive" in sources:  # after gmail, so docs already pulled via email links aren't fetched twice
        seen = {d for it in results.get("gmail", []) for d in it.meta.get("drive_ids", [])}
        name, items, st = _timed("drive", lambda: drive.fetch(scope, llm=llm, skip_ids=seen))
        results[name], status[name] = items, st
    for items in results.values():
        for it in items:
            it.body, it.title = redact(it.body), redact(it.title)
    return results, status


def _fit(results: dict[str, list[ContextItem]], budget: int) -> dict[str, list[ContextItem]]:
    """Share the char budget fairly across sources (water-filling), newest items first."""
    sizes = {s: sum(len(i.body) for i in items) for s, items in results.items() if items}
    caps, remaining, open_ = {}, budget, set(sizes)
    while open_:
        share = remaining // len(open_)
        small = {s for s in open_ if sizes[s] <= share}
        if not small:
            caps.update({s: share for s in open_})
            break
        for s in small:
            caps[s] = sizes[s]
            remaining -= sizes[s]
        open_ -= small
    fitted = {}
    for s, items in results.items():
        left, kept = caps.get(s, 0), []
        for it in sorted(items, key=lambda i: i.created_at, reverse=True):
            if left <= 0 or (len(it.body) > left and left < 500):
                break
            if len(it.body) > left:
                it.body = it.body[:left] + "\n[… truncated to fit context budget …]"
            kept.append(it)
            left -= len(it.body)
        fitted[s] = kept
    return fitted


def render_context(results: dict[str, list[ContextItem]], scope: RunScope) -> str:
    lines = [
        "# Project context",
        f"- Project path: {scope.path or '-'}",
        f"- GitHub repo: {scope.repo or '-'}",
        f"- Upcoming meeting: {scope.meeting or '-'}",
        f"- Window: since {scope.since:%Y-%m-%d} until {datetime.now():%Y-%m-%d}",
        "",
    ]
    for src in SOURCES:
        items = results.get(src) or []
        if not items:
            continue
        lines.append(f"## Source: {LABELS[src]} ({len(items)} items)\n")
        for it in sorted(items, key=lambda i: i.created_at):
            meta = " | ".join(x for x in [it.created_at.strftime("%Y-%m-%d %H:%M"), it.author or "", it.url or ""] if x)
            body = re.sub(r"^(#+)", r"\\\1", it.body.strip(), flags=re.M)  # keep item headers unambiguous
            lines += [f"### [{it.id}] {it.title}", f"_{meta}_", "", body, ""]
    return "\n".join(lines)


def new_run_dir() -> Path:
    d = settings.runs_dir / datetime.now().strftime("%Y-%m-%d_%H%M%S")
    (d / "raw").mkdir(parents=True, exist_ok=True)
    return d


def write_run(run_dir: Path, scope: RunScope, results, status) -> tuple[Path, dict[str, dict]]:
    for src, items in results.items():
        (run_dir / "raw" / f"{src}.json").write_text(json.dumps([i.to_dict() for i in items], indent=2, default=str))
    fitted = _fit(results, settings.context_char_budget)
    context = render_context(fitted, scope)
    (run_dir / "context.md").write_text(context)
    index = {i.id: {"title": i.title, "url": i.url, "source": i.source, "kind": i.kind}
             for items in fitted.values() for i in items}
    (run_dir / "manifest.json").write_text(json.dumps({
        "scope": {"since": scope.since.isoformat(), "path": scope.path, "repo": scope.repo,
                  "meeting": scope.meeting, "keywords": scope.keywords},
        "sources": status, "context_chars": len(context), "items": index,
    }, indent=2))
    return run_dir / "context.md", index
