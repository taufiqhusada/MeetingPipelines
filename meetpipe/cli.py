"""CLI: doctor | connect | setup-github | run | aggregate."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import brief as brief_mod
from .config import settings
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


def _line(ok: bool | None, name: str, detail: str) -> None:
    mark = {True: "✅", False: "❌", None: "⚪"}[ok]
    print(f"{mark} {name:<16} {detail}")


def cmd_doctor(args) -> int:
    from .connectors import claude_code, codex, github
    from .google_auth import GoogleNotConnected, client_config, service

    scope = RunScope(since=parse_since(args.since), path=os.path.abspath(args.path), repo=args.repo)
    print(f"Checking sources for path={scope.path}, since={scope.since:%Y-%m-%d}\n")

    n = len(claude_code.fetch(scope))
    _line(n > 0 or None, "claude_code", f"{n} sessions in {settings.claude_projects_dir}")
    n = len(codex.fetch(scope))
    _line(n > 0 or None, "codex", f"{n} sessions in {settings.codex_sessions_dir} (matching path)")

    s = github.session()
    authed = "Authorization" in s.headers
    try:
        if authed:
            who = s.get("https://api.github.com/user", timeout=15).json().get("login")
            detail = f"authenticated as @{who}"
        else:
            detail = "not connected (public repos only, 60 req/h) — run: uv run run.py connect"
        repo = scope.repo or github.detect_repo(scope.path)
        if repo:
            r = s.get(f"https://api.github.com/repos/{repo}", timeout=15)
            detail += f"; repo {repo}: HTTP {r.status_code}"
            ok = r.status_code == 200 and (authed or None)
        else:
            ok, detail = None, detail + "; no repo detected (pass --repo)"
        _line(ok, "github", detail)
    except Exception as e:
        _line(False, "github", str(e))

    if not client_config():
        _line(False, "google", "team Google app not configured (GOOGLE_CLIENT_ID/SECRET) — see README")
    else:
        try:
            email = service("gmail", "v1").users().getProfile(userId="me").execute()["emailAddress"]
            _line(True, "gmail", f"connected as {email}")
            user = service("drive", "v3").about().get(fields="user").execute()["user"]["emailAddress"]
            _line(True, "drive", f"connected as {user}")
        except GoogleNotConnected as e:
            _line(False, "google", str(e))
        except Exception as e:
            _line(False, "google", f"{type(e).__name__}: {e}")

    tdir = settings.transcripts_dir
    count = len([p for p in tdir.rglob("*") if p.is_file()]) if tdir.exists() else 0
    _line(count > 0 or None, "local", f"{count} files in {tdir}")

    for backend in ("claude", "codex"):
        try:
            llm = LLM(backend, timeout=180)
            if args.ping:
                reply = llm.run("Reply with exactly the word: PONG")
                _line("PONG" in reply.upper(), f"llm:{backend}", f"{llm.bin} → {reply[:40]!r}")
            else:
                _line(True, f"llm:{backend}", f"{llm.bin} (use --ping to test a call)")
        except Exception as e:
            _line(False, f"llm:{backend}", str(e)[:300])
    return 0


def cmd_connect(args) -> int:
    from .connect import serve
    serve(port=args.port, open_browser=not args.no_browser,
          start_path="/setup/github" if args.cmd == "setup-github" else "/")
    return 0


def cmd_run(args) -> int:
    from .connectors.github import detect_repo

    path = os.path.abspath(args.path)
    scope = RunScope(since=parse_since(args.since), path=path, repo=args.repo or detect_repo(path),
                     meeting=args.meeting, keywords=[k.strip() for k in (args.keywords or "").split(",") if k.strip()])
    sources = [s.strip() for s in args.sources.split(",")] if args.sources else SOURCES
    llm = None if args.no_llm else LLM(args.llm)
    run_dir = new_run_dir()
    print(f"▶ Run {run_dir.name}  path={scope.path}  repo={scope.repo}  since={scope.since:%Y-%m-%d}"
          f"  llm={args.llm if llm else 'none'}")

    print("① Gathering …")
    results, status = gather(scope, sources, llm=llm)
    for src in sources:
        st = status.get(src, {})
        detail = (f"{st.get('items')} items in {st.get('seconds')}s" if st.get("status") == "ok"
                  else st.get("reason", ""))
        print(f"   {src:<12} {st.get('status', '-'):<8} {detail[:200]}")

    print("② Trimming + redacting …")
    context_path, index = write_run(run_dir, scope, results, status)
    print(f"   context.md: {context_path.stat().st_size:,} bytes, {len(index)} items")

    if llm is None:
        print(f"\nDone (no summary). See {run_dir}")
        return 0
    if not index:
        print("No context gathered; skipping summary.")
        return 1

    print(f"③ Summarizing with {args.llm} …")
    result = brief_mod.summarize(context_path.read_text(), llm)
    (run_dir / "brief.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    title = f"Pre-meeting brief: {scope.meeting or scope.repo or Path(scope.path).name}"
    md = brief_mod.to_markdown(result, index, title)
    (run_dir / "brief.md").write_text(md)
    print(f"\n{md}\n\n✔ Wrote {run_dir}/brief.md and brief.json")
    return 0


def cmd_aggregate(args) -> int:
    briefs, index = {}, {}
    for spec in args.briefs:
        label, _, p = spec.rpartition("=")
        path = Path(p)
        run_dir = path if path.is_dir() else path.parent
        label = label or run_dir.name
        briefs[label] = json.loads((run_dir / "brief.json").read_text())
        manifest = run_dir / "manifest.json"
        if manifest.exists():
            for k, v in json.loads(manifest.read_text()).get("items", {}).items():
                index[f"{label}:{k}"] = v
    result = brief_mod.aggregate(briefs, LLM(args.llm))
    out = settings.runs_dir / f"aggregate_{datetime.now():%Y-%m-%d_%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "team_brief.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    md = brief_mod.to_markdown(result, index, f"Team brief: {args.meeting or ', '.join(briefs)}")
    (out / "team_brief.md").write_text(md)
    print(f"{md}\n\n✔ Wrote {out}/team_brief.md")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="run.py", description="Meeting Pipelines: pre-meeting context brief")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="check every source + LLM backend")
    d.add_argument("--path", default=".")
    d.add_argument("--repo")
    d.add_argument("--since", default="30d")
    d.add_argument("--ping", action="store_true", help="also make a tiny test call to each LLM CLI")
    d.set_defaults(fn=cmd_doctor)

    c = sub.add_parser("connect", help="open the local consent page to connect Google + GitHub")
    c.add_argument("--port", type=int, default=8765)
    c.add_argument("--no-browser", action="store_true")
    c.set_defaults(fn=cmd_connect)

    sg = sub.add_parser("setup-github", help="one-time: create the team's read-only GitHub App (writes .env)")
    sg.add_argument("--port", type=int, default=8765)
    sg.add_argument("--no-browser", action="store_true")
    sg.set_defaults(fn=cmd_connect)

    r = sub.add_parser("run", help="gather → trim → summarize")
    r.add_argument("--path", default=".", help="local repo path (matches coding-agent sessions)")
    r.add_argument("--repo", help="GitHub owner/name (default: detected from git remote)")
    r.add_argument("--since", default="14d", help="e.g. 7d, 2w, 2026-10-01")
    r.add_argument("--meeting", help="upcoming meeting title / topic")
    r.add_argument("--keywords", help="comma-separated hints for Drive triage")
    r.add_argument("--sources", help=f"comma list, default all: {','.join(SOURCES)}")
    r.add_argument("--llm", default=settings.llm, choices=["claude", "codex"])
    r.add_argument("--no-llm", action="store_true", help="gather only (keyword Drive triage, no summary)")
    r.set_defaults(fn=cmd_run)

    g = sub.add_parser("aggregate", help="merge several briefs into one team brief")
    g.add_argument("briefs", nargs="+", help="run dirs or brief.json paths, optionally label=path")
    g.add_argument("--meeting")
    g.add_argument("--llm", default=settings.llm, choices=["claude", "codex"])
    g.set_defaults(fn=cmd_aggregate)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
