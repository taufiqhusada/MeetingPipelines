"""After-meeting pipeline: analyze -> store -> deliver human tasks -> queue agent tasks for approval."""
import json
from collections import defaultdict

from . import config, db, executors, report
from .analyze import analyze
from .schemas import Transcript


def _due_date_followups(tasks: list[dict]) -> list[dict]:
    """No deadline said in the meeting -> a human action item to set one (grouped per owner)."""
    missing = defaultdict(list)
    for t in tasks:
        if not t["due_date"] and t["status"] != "done":
            missing[t["owner"]].append(t["title"])
    return [
        {
            "title": ("Set due dates for: " if owner else "Assign an owner and due date for: ") + "; ".join(titles),
            "owner": owner,
            "project_role": next((t["project_role"] for t in tasks if t["owner"] == owner), None),
            "due_date": None,
            "status": "todo",
            "channel": "slack",
            "source_quote": "(generated: no deadline was given in the meeting)",
            "generated": True,
        }
        for owner, titles in missing.items()
    ]


def _flags(t: dict) -> list[str]:
    f = []
    if not t["owner"]:
        f.append("unassigned")
    if not t["due_date"] and not t.get("generated"):
        f.append("no_due_date")
    return f


def process(transcript: Transcript) -> dict:
    db.init()
    analysis, engine = analyze(transcript)
    a = analysis.model_dump()
    mid = transcript.meeting_id

    # Re-processing a meeting replaces its previous output.
    for table in ("human_tasks", "agent_tasks", "outbox"):
        db.execute(f"DELETE FROM {table} WHERE meeting_id = ?", mid)
    db.execute(
        "INSERT OR REPLACE INTO meetings (id, title, date, source, transcript_json, analysis_json, goals_json) "
        "VALUES (?,?,?,?,?,?,?)",
        mid, transcript.title, transcript.date, transcript.source, transcript.model_dump_json(),
        json.dumps({**a, "engine": engine}),
        json.dumps([{"goal": g, "owner": None} for g in a["next_meeting_goals"]]),
    )

    # Human tasks: store, then route to GitHub (engineering) or Slack DM (everything else).
    human = a["human_tasks"] + _due_date_followups(a["human_tasks"])
    for t in human:
        if t["channel"] == "github":
            delivery = executors.github_issue(mid, t)
        else:
            delivery = executors.slack_message(
                mid, f"New action item: *{t['title']}*\nDue: {t['due_date'] or 'not set'}\n> {t['source_quote']}",
                t["owner"],
            )
        db.execute(
            "INSERT INTO human_tasks (meeting_id, title, owner, project_role, due_date, status, channel, "
            "source_quote, flags, delivery) VALUES (?,?,?,?,?,?,?,?,?,?)",
            mid, t["title"], t["owner"], t["project_role"], t["due_date"], t["status"], t["channel"],
            t["source_quote"], json.dumps(_flags(t)), json.dumps(delivery),
        )

    # Per-person progress DM.
    for p in a["person_updates"]:
        mine = [t["title"] for t in human if t["owner"] == p["person"]]
        text = "\n".join(
            [f"*Your update from {transcript.title}*"]
            + [f"✅ {x}" for x in p["done"]]
            + [f"➡️ {x}" for x in mine or p["next"]]
            + [f"⛔ {x}" for x in p["blockers"]]
        )
        executors.slack_message(mid, text, p["person"])

    # Team channel recap.
    unassigned = [t["title"] for t in human if not t["owner"]]
    executors.slack_message(
        mid,
        f"*{transcript.title} recap*\n{a['summary']}\n\n*Decisions*\n"
        + "\n".join(f"• {d['decision']}" for d in a["decisions"])
        + ("\n\n*Needs an owner*\n" + "\n".join(f"• {u}" for u in unassigned) if unassigned else "")
        + f"\n\nDetails: {config.BASE_URL}/?meeting={mid}",
    )

    # Agent tasks: wait for approval. Darwin search shows which external agent could take each one.
    for t in a["agent_tasks"]:
        db.execute(
            "INSERT INTO agent_tasks (meeting_id, title, kind, payload_json, darwin_json) VALUES (?,?,?,?,?)",
            mid, t["title"], t["kind"], json.dumps(t),
            json.dumps(executors.darwin_search(t["capability_query"])),
        )
    return get_meeting(mid)


def decide(task_id: int, approve: bool) -> dict:
    row = db.rows("SELECT * FROM agent_tasks WHERE id = ?", task_id)[0]
    if row["approval"] != "pending":
        return get_meeting(row["meeting_id"])
    if not approve:
        db.execute("UPDATE agent_tasks SET approval='rejected' WHERE id=?", task_id)
        return get_meeting(row["meeting_id"])

    mid, task = row["meeting_id"], json.loads(row["payload_json"])
    if row["kind"] == "schedule_meeting":
        result = executors.schedule_meeting(mid, task)
    elif row["kind"] == "meeting_report":
        url = report.build(get_meeting(mid))
        result = {"ok": True, "report": url}
        executors.slack_message(mid, f"Meeting report is ready: {config.BASE_URL}{url}")
    else:
        # No built-in executor (e.g. purchases): hand the Darwin matches to the requester.
        darwin = json.loads(row["darwin_json"] or "{}")
        options = "\n".join(f"• {a['name']} ({a['readiness']})" for a in darwin.get("agents", [])) or "• none found"
        executors.slack_message(
            mid, f"Approved: *{task['title']}*. Agents that can do this:\n{options}\nA human completes it.",
            task.get("requested_by"),
        )
        result = {"ok": True, "detail": "Routed to a human with Darwin's agent matches (no auto-execution)."}
    db.execute("UPDATE agent_tasks SET approval='approved', result_json=? WHERE id=?", json.dumps(result), task_id)
    return get_meeting(mid)


def get_meeting(meeting_id: str) -> dict:
    m = db.rows("SELECT * FROM meetings WHERE id = ?", meeting_id)
    if not m:
        raise KeyError(meeting_id)
    m = m[0]
    loads = lambda s, d=None: json.loads(s) if s else d  # noqa: E731
    return {
        "id": m["id"], "title": m["title"], "date": m["date"], "source": m["source"],
        "transcript": loads(m["transcript_json"]),
        "analysis": loads(m["analysis_json"]),
        "team": db.team(),
        "human_tasks": [
            {**t, "flags": loads(t["flags"], []), "delivery": loads(t["delivery"])}
            for t in db.rows("SELECT * FROM human_tasks WHERE meeting_id=? ORDER BY id", meeting_id)
        ],
        "agent_tasks": [
            {**t, "payload": loads(t.pop("payload_json")), "result": loads(t.pop("result_json")),
             "darwin": loads(t.pop("darwin_json"))}
            for t in db.rows("SELECT * FROM agent_tasks WHERE meeting_id=? ORDER BY id", meeting_id)
        ],
        "outbox": db.rows("SELECT * FROM outbox WHERE meeting_id=? ORDER BY id", meeting_id),
        "flags": config.flags(),
    }
