"""Read the After stage's SQLite store: last meetings' decisions, tasks (owner/due/status) and next goals.

This closes the loop After → next Before: the goals and tasks After saved become the commitments the next
pre-meeting brief checks against GitHub, coding sessions and docs.
"""

from __future__ import annotations

import json
import sqlite3

from ..config import settings
from ..models import ContextItem, RunScope, SourceNotConfigured, parse_ts


def fetch(scope: RunScope) -> list[ContextItem]:
    path = settings.after_db_path
    if not path.exists():
        raise SourceNotConfigured(f"no After database at {path}")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        meetings = db.execute("SELECT * FROM meetings WHERE analysis_json IS NOT NULL AND date >= ? ORDER BY date",
                              (scope.since.date().isoformat(),)).fetchall()
        items = []
        for m in meetings:
            a = json.loads(m["analysis_json"])
            tasks = db.execute("SELECT * FROM human_tasks WHERE meeting_id = ? ORDER BY id", (m["id"],)).fetchall()
            agent = db.execute("SELECT title, kind, approval FROM agent_tasks WHERE meeting_id = ?", (m["id"],)).fetchall()
            parts = [f"Summary: {a.get('summary', '')}"]
            if a.get("decisions"):
                parts.append("Decisions:\n" + "\n".join(f"- {d['decision']}" for d in a["decisions"]))
            if tasks:
                parts.append("Tasks assigned in this meeting (owner | due | status):\n" + "\n".join(
                    f"- {t['title']} | {t['owner'] or 'UNASSIGNED'} | {t['due_date'] or 'no due date'} | {t['status']}"
                    for t in tasks))
            if agent:
                parts.append("Agent tasks:\n" + "\n".join(f"- {t['title']} ({t['kind']}, {t['approval']})" for t in agent))
            if a.get("next_meeting_goals"):
                parts.append("Goals for the next meeting:\n" + "\n".join(f"- {g}" for g in a["next_meeting_goals"]))
            if a.get("open_questions"):
                parts.append("Open questions:\n" + "\n".join(f"- {q}" for q in a["open_questions"]))
            items.append(ContextItem(
                id=f"AF-{m['id'][-10:]}", source="after", kind="meeting",
                title=f"After-meeting record: {m['title']} ({m['date']})", body="\n\n".join(parts),
                created_at=parse_ts(m["date"]), meta={"meeting_id": m["id"]},
            ))
        return items
    finally:
        db.close()
