"""Approve / reject After's agent tasks from Slack buttons (Socket Mode, no public URL needed).

Slack spreads Socket Mode events across every process connected with the same app token, so the handlers must
live in exactly one process:
  - normally the team bot: meetpipe/slack_bot.py calls register(app) at startup
  - or, when that bot isn't running, this module standalone:  python -m after.slack_actions

The handlers call After's REST API (AFTER_API_URL), so the bot can run on a different laptop than After.
Requires "Interactivity" to be switched on in the Slack app settings (no request URL needed with Socket Mode).
"""
import os

import httpx

AFTER_API_URL = os.getenv("AFTER_API_URL", "http://localhost:8000").rstrip("/")
APPROVE, REJECT = "after_task_approve", "after_task_reject"

KIND_LABEL = {"schedule_meeting": "🗓 Schedule meeting", "meeting_report": "📊 Recap + visuals", "other": "🔎 Research"}


def block_id(task_id: int) -> str:
    return f"after_task_{task_id}"


def approval_blocks(meeting_title: str, tasks: list[dict], base_url: str) -> list[dict]:
    """One section + Approve/Reject buttons per agent task."""
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": f"Agent tasks from {meeting_title}"[:150]}}]
    for t in tasks:
        p = t["payload"]
        when = f" · {p['start_datetime'].replace('T', ' ')[:16]} ({p.get('duration_minutes') or 30} min)" \
            if p.get("start_datetime") else ""
        route = ((t.get("darwin") or {}).get("agents") or [{}])[0].get("name")
        lines = [f"*{t['title']}*", f"{KIND_LABEL.get(t['kind'], t['kind'])}{when}", f"> {p['source_quote']}"]
        if route:
            lines.append(f"_Darwin route: {route}_")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:2900]}})
        blocks.append({"type": "actions", "block_id": block_id(t["id"]), "elements": [
            {"type": "button", "style": "primary", "action_id": APPROVE, "value": str(t["id"]),
             "text": {"type": "plain_text", "text": "Approve & run"}},
            {"type": "button", "style": "danger", "action_id": REJECT, "value": str(t["id"]),
             "text": {"type": "plain_text", "text": "Reject"}},
        ]})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"Details: {base_url}"}]})
    return blocks


def outcome_text(task: dict, user_id: str) -> str:
    """One line that replaces the buttons once a decision is made."""
    who = f"<@{user_id}>"
    if task["approval"] == "rejected":
        return f"❌ Rejected by {who}"
    r = task.get("result") or {}
    if task["kind"] == "schedule_meeting":
        cal = (r.get("calendar") or {})
        done = f"<{cal['detail']}|calendar event created>" if cal.get("sent") else "invite (.ics) ready, calendar in dry-run"
        return f"✅ Approved by {who}: {done}"
    if task["kind"] == "meeting_report":
        return f"✅ Approved by {who}: recap + visuals posted"
    summary = (r.get("summary") or r.get("detail") or "done").strip().splitlines()[0]
    return f"✅ Approved by {who}: {summary[:280]}"


def replace_actions(blocks: list[dict], task_id: int, text: str) -> list[dict]:
    target = block_id(task_id)
    return [{"type": "context", "block_id": target, "elements": [{"type": "mrkdwn", "text": text}]}
            if b.get("block_id") == target else b for b in blocks]


def handle_click(decision: str, body: dict, client, api_url: str = AFTER_API_URL) -> str:
    """Run one button click: show progress, call After, then swap the buttons for the outcome."""
    task_id = int(body["actions"][0]["value"])
    user = body["user"]
    channel, ts = body["channel"]["id"], body["message"]["ts"]
    blocks = body["message"]["blocks"]
    verb = "Approving and running" if decision == "approve" else "Rejecting"
    client.chat_update(channel=channel, ts=ts, text=f"{verb} task {task_id}…",
                       blocks=replace_actions(blocks, task_id, f"⏳ {verb} for <@{user['id']}>…"))
    try:
        r = httpx.post(f"{api_url}/api/agent-tasks/{task_id}/{decision}",
                       params={"approver": user.get("username") or user.get("name") or ""}, timeout=300)
        data = r.json()
        if r.status_code >= 400:
            text = f"⚠️ {data.get('detail', r.status_code)}"
        else:
            task = next(t for t in data["agent_tasks"] if t["id"] == task_id)
            text = outcome_text(task, user["id"])
    except Exception as e:  # After unreachable: put the buttons back so someone can retry
        client.chat_update(channel=channel, ts=ts, text="After is unreachable", blocks=blocks)
        client.chat_postEphemeral(channel=channel, user=user["id"], text=f"Couldn't reach After at {api_url}: {e}")
        return "error"
    client.chat_update(channel=channel, ts=ts, text=text, blocks=replace_actions(blocks, task_id, text))
    return text


def register(app) -> None:
    """Attach the button handlers to a slack_bolt App."""
    @app.action(APPROVE)
    def on_approve(ack, body, client):
        ack()
        handle_click("approve", body, client)

    @app.action(REJECT)
    def on_reject(ack, body, client):
        ack()
        handle_click("reject", body, client)


def main() -> None:
    from dotenv import load_dotenv
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    load_dotenv()
    app = App(token=os.environ["SLACK_BOT_TOKEN"])
    register(app)
    print(f"After approval buttons listening (After API: {AFTER_API_URL}). Don't run alongside the team bot.")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()


if __name__ == "__main__":
    main()
