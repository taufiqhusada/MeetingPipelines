"""End-to-end demo: Prep → Meeting → Transcript → After → (next) Prep.

  ① Prep      meetpipe brief from your real sources (Claude Code, GitHub, Gmail, Drive, …)
  ② Meeting   test transcript of the weekly sync (scripts/seed_data/transcript_weekly_sync.json),
              standing in for a Fireflies recording
  ③ After     the real after/ pipeline (analysis → tasks → GitHub/Slack routing → agent tasks → report),
              in DRY_RUN so nothing is sent; analysis runs on the local Claude Code/Codex CLI instead of an API key
  ④ Loop      the next prep reads After's tasks + goals as commitments (source "after")

  uv run --group after scripts/demo_e2e.py                     # ①–④ (④ gather only)
  uv run --group after scripts/demo_e2e.py --loop              # ④ also writes the next brief
  uv run --group after scripts/demo_e2e.py --slack "#test-bot-features"   # post the brief + After recap
  uv run --group after scripts/demo_e2e.py --skip-prep         # reuse the latest brief in runs/

Then open the After UI on the same data:
  AFTER_DB_PATH=runs/demo_e2e/after.db uv run --group after uvicorn after.app:app
  → http://localhost:8000/?meeting=mp-weekly-sync-2026-10-11
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DEMO_DIR = ROOT / "runs" / "demo_e2e"
TRANSCRIPT = ROOT / "scripts" / "seed_data" / "transcript_weekly_sync.json"

# After reads its settings at import time: point it at an isolated demo DB and force dry-run.
os.environ["AFTER_DB_PATH"] = str(DEMO_DIR / "after.db")
os.environ["DRY_RUN"] = "1"

TEAM = [
    ("taufiq", "Taufiq Husada Daryanto", "Lead / Before stage (meetpipe)", "taufiqhusada"),
    ("michael", "Michael", "Integrations (Slack)", None),
    ("george", "George", "After stage", None),
]
DESIGN_REVIEW = {  # the previous meeting: its goals are what this sync is checked against
    "id": "mp-design-review-2026-10-09", "title": "MeetingPipelines design review", "date": "2026-10-09",
    "goals": [
        {"goal": "Connectors (Claude Code, Codex, GitHub, Gmail, Drive) working end to end on real data", "owner": "taufiq"},
        {"goal": "Click-to-connect consent page and one-click GitHub App setup", "owner": "taufiq"},
        {"goal": "Slack bot first version that posts the brief to the team channel", "owner": "michael"},
        {"goal": "After-meeting pipeline spec in Drive", "owner": "george"},
    ],
}


def banner(n: str, title: str) -> None:
    print(f"\n{'═' * 78}\n {n}  {title}\n{'═' * 78}")


def latest_brief_run() -> Path | None:
    runs = sorted((p for p in (ROOT / "runs").glob("20*") if (p / "brief.md").exists()), reverse=True)
    return runs[0] if runs else None


# ---------------------------------------------------------------- ① prep
def stage_prep(args):
    from meetpipe.runner import RunResult, make_scope, prepare

    banner("①", "PREP: pre-meeting brief from real sources (meetpipe)")
    if args.skip_prep and (run := latest_brief_run()):
        print(f"(reusing {run.relative_to(ROOT)})")
        manifest = json.loads((run / "manifest.json").read_text())
        result = RunResult(run_dir=run, status=manifest["sources"], index=manifest["items"],
                           brief=json.loads((run / "brief.json").read_text()), markdown=(run / "brief.md").read_text())
    else:
        scope = make_scope(str(ROOT), args.since, meeting="MeetingPipelines weekly sync")
        result = prepare(scope, sources=[s for s in ["claude_code", "codex", "github", "gmail", "fireflies", "drive"]],
                         llm_name=args.llm)
    b = result.brief or {}
    print(f"\nHeadline: {b.get('headline', '')}\n")
    for c in b.get("commitments", []):
        print(f"  [{c.get('status'):<11}] {c.get('owner') or '-':<8} {c['what'][:90]}")
    print(f"\n→ {result.run_dir.relative_to(ROOT)}/brief.md")
    return result


# ---------------------------------------------------------------- ② meeting
def stage_meeting():
    banner("②", "MEETING: weekly sync transcript (test data standing in for Fireflies)")
    data = json.loads(TRANSCRIPT.read_text())
    print(f"{data['title']} · {data['date']} · {', '.join(data['attendees'])} · {len(data['lines'])} lines\n")
    for line in data["lines"][:6]:
        print(f"  {line['speaker']}: {line['text'][:110]}")
    print("  …")
    return data


# ---------------------------------------------------------------- ③ after
def stage_after(transcript_json: dict, llm_name: str):
    banner("③", "AFTER: transcript → summary, tasks, routing, agent actions (after/, DRY_RUN)")
    db_path = DEMO_DIR / "after.db"
    db_path.unlink(missing_ok=True)

    from after import db, pipeline
    from after.analyze import SYSTEM, _prompt
    from after.schemas import MeetingAnalysis, Transcript
    from meetpipe.llm import LLM

    # Seed the demo team + the previous meeting before After's own seeding runs.
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as c:
        c.executescript(db.SCHEMA)
        c.executemany("INSERT INTO users VALUES (?,?,?,?,?,?)",
                      [(h, n, r, None, None, gh) for h, n, r, gh in TEAM])
        c.execute("INSERT INTO meetings (id, title, date, source, goals_json) VALUES (?,?,?,?,?)",
                  (DESIGN_REVIEW["id"], DESIGN_REVIEW["title"], DESIGN_REVIEW["date"], "seed",
                   json.dumps(DESIGN_REVIEW["goals"])))

    llm = LLM(llm_name)

    def local_analyze(t: Transcript):
        """Same prompt + schema as after/analyze.py, run on the local CLI instead of the Anthropic API."""
        prompt = (f"{SYSTEM}\n\n{_prompt(t)}\n\nReturn ONLY a JSON object matching this JSON schema:\n"
                  f"{json.dumps(MeetingAnalysis.model_json_schema())}")
        return MeetingAnalysis.model_validate(llm.json(prompt)), f"local-{llm_name}"

    pipeline.analyze = local_analyze
    print(f"Analyzing with local {llm_name} …")
    result = pipeline.process(Transcript.model_validate(transcript_json))
    a = result["analysis"]

    # Approve the safe agent tasks so the demo shows execution; leave the rest waiting for a human.
    for t in result["agent_tasks"]:
        if t["kind"] in ("meeting_report", "schedule_meeting"):
            result = pipeline.decide(t["id"], approve=True)

    print(f"\nSummary: {a['summary']}\n")
    print("Last meeting's goals → progress:")
    for g in a["goal_progress"]:
        print(f"  [{g['status']:<11}] {g.get('owner') or '-':<8} {g['goal'][:80]}")
    print("\nDecisions:")
    for d in a["decisions"]:
        print(f"  • {d['decision']}")
    print("\nHuman tasks (routed):")
    for t in result["human_tasks"]:
        flags = f"  ⚑ {', '.join(t['flags'])}" if t["flags"] else ""
        print(f"  → {t['channel']:<6} {t['owner'] or 'UNASSIGNED':<10} due {t['due_date'] or '—':<10} {t['title'][:70]}{flags}")
    print("\nAgent tasks:")
    for t in result["agent_tasks"]:
        res = t.get("result") or {}
        detail = res.get("report") or res.get("ics") or res.get("detail") or ""
        print(f"  [{t['approval']:<8}] {t['kind']:<16} {t['title'][:60]}  {detail}")
    print(f"\nOutbox (would be sent; DRY_RUN): {len(result['outbox'])} messages")
    for m in result["outbox"][:4]:
        print(f"  {m['channel']} → {m['target'] or 'team channel'}: {m['body'].splitlines()[0][:90]}")
    print("\nNext meeting goals (saved for the next prep):")
    for g in a["next_meeting_goals"]:
        print(f"  • {g}")
    (DEMO_DIR / "after_result.json").write_text(json.dumps(result, indent=2, default=str))
    return result


# ---------------------------------------------------------------- ④ loop
def stage_loop(args):
    from meetpipe.config import settings
    from meetpipe.connectors import after_db
    from meetpipe.runner import make_scope, prepare

    banner("④", "LOOP: the next prep reads After's tasks + goals as commitments")
    settings.after_db_path = DEMO_DIR / "after.db"
    scope = make_scope(str(ROOT), "7d", meeting="MeetingPipelines weekly sync (next week)")
    items = after_db.fetch(scope)
    for it in items:
        print(f"[{it.id}] {it.title}\n" + "\n".join("  " + l for l in it.body.splitlines()[:14]))
    if args.loop:
        print("\nWriting next week's brief (claude_code + github + after) …")
        result = prepare(scope, sources=["claude_code", "github", "after"], llm_name=args.llm)
        print(f"→ {result.run_dir.relative_to(ROOT)}/brief.md")


def post_to_slack(channel: str, prep, after_result) -> None:
    from slack_sdk import WebClient

    from meetpipe.slack_bot import post_brief, resolve_channel, to_mrkdwn

    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    cid = resolve_channel(channel)
    root = client.chat_postMessage(channel=cid, text="*① Prep → ② Meeting → ③ After: end-to-end demo*")
    post_brief(cid, prep, thread_ts=root["ts"], client=client)
    recap = next((m["body"] for m in after_result["outbox"] if "recap" in m["body"].lower()), None)
    if recap:
        client.chat_postMessage(channel=cid, thread_ts=root["ts"], text=to_mrkdwn("*③ After-meeting recap*\n" + recap))
    print(f"\n✔ Posted demo thread to {channel}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--since", default="14d")
    p.add_argument("--llm", default="claude", choices=["claude", "codex"])
    p.add_argument("--skip-prep", action="store_true", help="reuse the latest brief instead of generating one")
    p.add_argument("--loop", action="store_true", help="also generate the next brief from After's records")
    p.add_argument("--slack", metavar="CHANNEL", help="post the brief + After recap to this Slack channel")
    args = p.parse_args()

    from meetpipe import config  # noqa: F401  (loads .env before After's config)

    prep = stage_prep(args)
    transcript = stage_meeting()
    after_result = stage_after(transcript, args.llm)
    stage_loop(args)
    if args.slack:
        post_to_slack(args.slack, prep, after_result)
    print(f"\nDone. After UI: AFTER_DB_PATH={DEMO_DIR.relative_to(ROOT)}/after.db uv run --group after "
          f"uvicorn after.app:app → http://localhost:8000/?meeting={transcript['meeting_id']}")


if __name__ == "__main__":
    main()
