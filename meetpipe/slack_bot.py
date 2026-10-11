"""Slack front-end: `@agent generate meeting prep` → run the pipeline on this machine → post the brief.

Runs over Socket Mode (no public URL needed), so it works from a laptop. Needs SLACK_BOT_TOKEN
(scopes: app_mentions:read, chat:write, files:write, channels:read) and SLACK_APP_TOKEN (connections:write).

Command examples (in any channel the bot is in):
  @agent generate meeting prep
  @agent generate meeting prep since 7d for "Weekly sync"
  @agent help
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
import traceback

from . import brief as brief_mod
from .coordinator import create_job, get_job
from .llm import LLM
from .runner import RunResult, make_scope, prepare

TRIGGER = re.compile(r"\b(prep|brief|prepare)\b", re.I)
HELP = ("*Meeting prep agent*\n"
        "• `@agent generate meeting prep`: brief for the last 14 days\n"
        "• `@agent generate meeting prep since 7d for \"Weekly sync\"`: custom window + meeting focus\n"
        "The coordinator asks each online teammate's local worker for repo-scoped Claude/Codex context, "
        "then combines it with shared GitHub, Gmail and Drive sources.")
SOURCE_LABEL = {"claude_code": "Claude Code", "codex": "Codex", "github": "GitHub", "gmail": "Gmail",
                "drive": "Drive", "local": "Local files", "workers": "Local workers",
                "fireflies": "Fireflies", "after": "After records"}
CHUNK = 3500  # stay well under Slack's per-message rendering limits


# ---------------------------- formatting ----------------------------

def to_mrkdwn(md: str) -> str:
    """Our brief markdown → Slack mrkdwn."""
    out = []
    for line in md.splitlines():
        if m := re.match(r"^#{1,6}\s+(.*)", line):
            line = f"*{m.group(1)}*"
        elif line.startswith("- "):
            line = "• " + line[2:]
        elif line.strip() == "---":
            line = ""
        line = re.sub(r"\*\*(.+?)\*\*", r"*\1*", line)
        line = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"<\2|\1>", line)
        out.append(line)
    return "\n".join(out).strip()


def chunks(text: str, size: int = CHUNK) -> list[str]:
    """Split on section boundaries (blank line before a *Header*), then on lines if still too long."""
    parts, cur = [], ""
    for block in re.split(r"\n(?=\*[^*\n]+\*\n)", text):
        if len(cur) + len(block) + 1 <= size:
            cur = f"{cur}\n{block}" if cur else block
            continue
        if cur:
            parts.append(cur)
        cur = ""
        for line in block.splitlines():
            if len(cur) + len(line) + 1 > size and cur:
                parts.append(cur)
                cur = ""
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


def source_summary(status: dict) -> str:
    bits = []
    for src, st in status.items():
        label = SOURCE_LABEL.get(src, src)
        extra = f" ({st['reason']})" if st.get("reason") else ""
        if st.get("status") == "ok":
            bits.append(f"{label} {st.get('items', 0)}{extra}")
        else:
            bits.append(f"{label} ⚠️ {st.get('status')}{extra}")
    return " · ".join(bits)


# ---------------------------- posting ----------------------------

def _client():
    from slack_sdk import WebClient
    return WebClient(token=os.environ["SLACK_BOT_TOKEN"])


def resolve_channel(name: str) -> str:
    from src.slack.messaging import _resolve_channel  # teammate's resolver (env mappings + API lookup)
    return _resolve_channel(name)


def post_brief(channel: str, result: RunResult, thread_ts: str | None = None, client=None) -> None:
    """Post the brief as mrkdwn (split into readable chunks) and attach brief.md."""
    client = client or _client()
    channel_id = channel if re.match(r"^[CGD][A-Z0-9]+$", channel) else resolve_channel(channel)
    parts = chunks(to_mrkdwn(result.markdown or ""))
    for i, part in enumerate(parts):
        client.chat_postMessage(channel=channel_id, text=part, thread_ts=thread_ts, unfurl_links=False,
                                unfurl_media=False, reply_broadcast=bool(thread_ts and i == 0))
    client.files_upload_v2(channel=channel_id, thread_ts=thread_ts, title=f"{result.run_dir.name}-brief.md",
                           file=str(result.run_dir / "brief.md"),
                           initial_comment=f"Sources: {source_summary(result.status)}")


# ---------------------------- command parsing ----------------------------

def parse_command(text: str) -> dict | None:
    """'<@U123> generate meeting prep since 7d for "Weekly sync"' → options, or None if not a prep request."""
    text = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
    if re.fullmatch(r"(help|\?)", text, re.I):
        return {"help": True}
    if not TRIGGER.search(text):
        return None
    opts: dict = {}
    if m := re.search(r"\bsince\s+(\d+[dwh]|\d{4}-\d{2}-\d{2})", text, re.I):
        opts["since"] = m.group(1)
    if m := re.search(r"[\"“]([^\"”]+)[\"”]", text):
        opts["meeting"] = m.group(1)
    elif m := re.search(r"\bfor\s+(?!the last)(.+?)(?:\s+since\b|$)", text, re.I):
        opts["meeting"] = m.group(1).strip()
    return opts


# ---------------------------- bot ----------------------------

def _collect_team_brief(scope, options: dict, llm_name: str = "claude", on_event=None) -> RunResult:
    emit = on_event or (lambda _e: None)
    coordinator_url = os.getenv("MEETPIPE_COORDINATOR_URL", "")
    coordinator_token = os.getenv("MEETPIPE_COORDINATOR_ADMIN_TOKEN", "")
    timeout_seconds = int(os.getenv("MEETPIPE_JOB_TIMEOUT_SECONDS", "300"))
    job = create_job(coordinator_url, coordinator_token, {
        "since": options.get("since", "14d"), "repo": scope.repo, "meeting": scope.meeting,
        "timeout_seconds": timeout_seconds,
    })
    print(f"Queued local context job {job['id']} for {len(job['workers'])} workers", flush=True)
    emit({"kind": "job", "job_id": job["id"], "workers": job["workers"], "deadline": job["deadline"]})

    # Gather common sources on the bot host while teammates retrieve their
    # private agent history locally in parallel.
    shared_sources = [s.strip() for s in os.getenv("MEETPIPE_SHARED_SOURCES", "github,gmail,drive,local").split(",")
                      if s.strip()]
    shared = prepare(scope, sources=shared_sources, llm_name=llm_name,
                     log=lambda line: print(line, flush=True), on_event=lambda e: emit({**e, "actor": "master"}))
    emit({"kind": "stage", "stage": "waiting_workers", "actor": "master"})
    deadline = float(job["deadline"])
    view = {"workers": []}
    while time.time() < deadline:
        view = get_job(coordinator_url, coordinator_token, job["id"])
        if view.get("status") == "complete":
            break
        time.sleep(2)
    if view.get("status") != "complete":
        view = get_job(coordinator_url, coordinator_token, job["id"])

    briefs, index = {}, {}
    if shared.brief:
        briefs["shared"] = shared.brief
        index.update({f"shared:{k}": v for k, v in shared.index.items()})
    worker_rows = view.get("workers", [])
    completed = []
    responded = []
    for row in worker_rows:
        worker_id, payload = row["worker_id"], row.get("result") or {}
        if row["status"] == "complete":
            responded.append(worker_id)
        if row["status"] == "complete" and payload.get("brief"):
            briefs[worker_id] = payload["brief"]
            index.update({f"{worker_id}:{k}": v for k, v in payload.get("index", {}).items()})
            completed.append(worker_id)

    source_status = dict(shared.status)
    worker_source_names = {s for r in worker_rows if r.get("status") == "complete"
                           for s in ((r.get("result") or {}).get("status") or {})}
    for source in sorted(worker_source_names | {"claude_code", "codex"}):
        counts = [int((r.get("result") or {}).get("status", {}).get(source, {}).get("items", 0))
                  for r in worker_rows if r.get("status") == "complete"]
        counts.append(int((shared.status.get(source) or {}).get("items", 0)))  # master's own fetch, if any
        source_status[source] = {"status": "ok" if any(counts) else "skipped", "items": sum(counts)}
    source_status["workers"] = {
        "status": "ok" if responded else "error",
        "items": len(responded),
        "reason": f"{len(responded)} of {len(worker_rows)} responded; "
                  f"{len(completed)} returned context"
                  + (f" ({', '.join(completed)})" if completed else ""),
    }
    if not briefs:
        shared.status = source_status
        return shared
    # Always merge (even a single brief) so the per-person view exists.
    emit({"kind": "stage", "stage": "merging", "actor": "master", "briefs": list(briefs)})
    final_brief = brief_mod.aggregate(briefs, LLM(llm_name))
    final_index = index

    title = f"Team pre-meeting brief: {scope.meeting or scope.repo or 'project sync'}"
    missing = [f"{r['worker_id']} ({r['status']})" for r in worker_rows if r['status'] != "complete"]
    missing += [f"{r['worker_id']} (no matching context)" for r in worker_rows
                if r['status'] == "complete" and not (r.get("result") or {}).get("brief")]
    coverage = ("\n\n_No result from " + ", ".join(missing) + "._") if missing else ""
    # Slack gets the short per-person view; the detailed brief is attached as brief.md.
    slack_md = (brief_mod.to_people_markdown(final_brief, final_index, title)
                if final_brief.get("people") else brief_mod.to_markdown(final_brief, final_index, title)) + coverage
    (shared.run_dir / "brief.json").write_text(json.dumps(final_brief, indent=2, ensure_ascii=False))
    (shared.run_dir / "brief.md").write_text(brief_mod.to_markdown(final_brief, final_index, title) + coverage)
    (shared.run_dir / "brief_people.md").write_text(slack_md)
    return RunResult(run_dir=shared.run_dir, status=source_status, index=final_index,
                     brief=final_brief, markdown=slack_md, title=title)


def serve(path: str = ".", llm_name: str = "claude") -> None:
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    app = App(token=os.environ["SLACK_BOT_TOKEN"])
    busy = threading.Lock()
    host = socket.gethostname().split(".")[0]
    coordinator_url = os.getenv("MEETPIPE_COORDINATOR_URL", "")
    coordinator_token = os.getenv("MEETPIPE_COORDINATOR_ADMIN_TOKEN", "")
    if not coordinator_url or not coordinator_token:
        raise RuntimeError("Set MEETPIPE_COORDINATOR_URL and MEETPIPE_COORDINATOR_ADMIN_TOKEN for the Slack bot")

    def run_job(channel: str, thread_ts: str, user: str, opts: dict, client) -> None:
        status_msg = client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f"On it <@{user}>! Asking online local workers for Claude/Codex context, while gathering "
                 f"shared sources since {opts.get('since', '14d')}…")
        try:
            scope = make_scope(path, opts.get("since", "14d"), meeting=opts.get("meeting"))
            result = _collect_team_brief(scope, opts, llm_name)
            if not result.markdown:
                client.chat_update(channel=channel, ts=status_msg["ts"],
                                   text=f"Couldn't build a brief: no context found. {source_summary(result.status)}")
                return
            client.chat_update(channel=channel, ts=status_msg["ts"],
                               text=f"✅ Brief ready for <@{user}>. Sources: {source_summary(result.status)}")
            post_brief(channel, result, thread_ts=thread_ts, client=client)
        except Exception as e:
            traceback.print_exc()
            client.chat_update(channel=channel, ts=status_msg["ts"], text=f"❌ Meeting prep failed: `{type(e).__name__}: {e}`")
        finally:
            busy.release()

    @app.event("app_mention")
    def on_mention(event, client, say):
        opts = parse_command(event.get("text", ""))
        thread_ts = event.get("thread_ts") or event["ts"]
        if opts is None:
            say(text="Try `@agent generate meeting prep` (or `@agent help`).", thread_ts=thread_ts)
            return
        if opts.get("help"):
            say(text=HELP, thread_ts=thread_ts)
            return
        if not busy.acquire(blocking=False):
            say(text="I'm already preparing a brief. I'll be free in a minute or two.", thread_ts=thread_ts)
            return
        threading.Thread(target=run_job, daemon=True,
                         args=(event["channel"], thread_ts, event.get("user", ""), opts, client)).start()

    @app.event("message")
    def ignore_messages():  # silence "unhandled event" warnings for plain channel messages
        pass

    print(f"Meeting prep bot connected as host '{host}' for {os.path.abspath(path)} (llm={llm_name}). Ctrl+C to stop.")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
