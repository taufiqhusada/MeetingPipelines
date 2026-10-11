"""CLI: doctor | connect | setup-github | run | slack-bot | aggregate."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from . import brief as brief_mod
from .config import settings
from .llm import LLM
from .models import RunScope
from .pipeline import SOURCES
from .runner import make_scope, parse_since, prepare


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
    scope = make_scope(args.path, args.since, args.repo, args.meeting,
                       [k.strip() for k in (args.keywords or "").split(",") if k.strip()])
    sources = [s.strip() for s in args.sources.split(",")] if args.sources else None
    result = prepare(scope, sources, None if args.no_llm else args.llm)
    if result.markdown is None:
        print(f"\nDone (no summary). See {result.run_dir}" if result.index else "No context gathered.")
        return 0 if result.index else 1
    print(f"\n{result.markdown}\n\n✔ Wrote {result.run_dir}/brief.md and brief.json")
    if args.post:
        from .slack_bot import post_brief
        post_brief(args.post, result)
        print(f"✔ Posted to Slack {args.post}")
    return 0


def cmd_slack_bot(args) -> int:
    from .slack_bot import serve
    serve(path=args.path, llm_name=args.llm)
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
    r.add_argument("--post", metavar="CHANNEL", help="also post the brief to a Slack channel, e.g. '#team'")
    r.set_defaults(fn=cmd_run)

    sb = sub.add_parser("slack-bot", help="listen for '@agent generate meeting prep' and reply with a brief")
    sb.add_argument("--path", default=".", help="local repo the bot prepares briefs for")
    sb.add_argument("--llm", default=settings.llm, choices=["claude", "codex"])
    sb.set_defaults(fn=cmd_slack_bot)

    g = sub.add_parser("aggregate", help="merge several briefs into one team brief")
    g.add_argument("briefs", nargs="+", help="run dirs or brief.json paths, optionally label=path")
    g.add_argument("--meeting")
    g.add_argument("--llm", default=settings.llm, choices=["claude", "codex"])
    g.set_defaults(fn=cmd_aggregate)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
