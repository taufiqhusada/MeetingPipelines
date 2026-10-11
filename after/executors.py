"""Side effects: GitHub issues, Slack messages, calendar invites, Darwin search.

Everything goes through `outbox()` so the UI shows what was (or would have been) sent.
"""
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from . import config, db


def outbox(meeting_id: str, channel: str, target: str, body: str, send=None) -> dict:
    """Run `send()` unless dry-run / not configured; always log the attempt."""
    sent, detail = False, "dry-run"
    if send is not None and not config.DRY_RUN:
        try:
            detail = send() or "ok"
            sent = True
        except Exception as e:  # surfaced in the UI, never crashes the pipeline
            detail = f"error: {e}"
    db.execute(
        "INSERT INTO outbox (meeting_id, channel, target, body, sent, detail) VALUES (?,?,?,?,?,?)",
        meeting_id, channel, target, body, int(sent), str(detail),
    )
    return {"sent": sent, "detail": detail}


def _user(handle: str | None) -> dict:
    return next((u for u in db.team() if u["handle"] == handle), {}) if handle else {}


# ---------------------------------------------------------------- GitHub

def github_issue(meeting_id: str, task: dict) -> dict:
    owner = _user(task.get("owner"))
    body = "\n".join([
        f"**Owner:** {owner.get('name', 'UNASSIGNED')} ({task.get('project_role') or 'role n/a'})",
        f"**Due:** {task.get('due_date') or 'not set'}",
        f"**Status:** {task['status']}",
        f"**From meeting:** `{meeting_id}`",
        "",
        f"> {task['source_quote']}",
        "",
        "_Created by the Meeting Agent (After stage)._",
    ])
    payload = {"title": task["title"], "body": body, "labels": ["meeting-agent"]}
    if config.GITHUB_ASSIGN and owner.get("github"):
        payload["assignees"] = [owner["github"]]

    def send():
        if not config.GITHUB_TOKEN:
            raise RuntimeError("GITHUB_TOKEN not set")
        r = httpx.post(
            f"https://api.github.com/repos/{config.GITHUB_REPO}/issues",
            headers={"Authorization": f"Bearer {config.GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
            json=payload,
            timeout=20,
        )
        r.raise_for_status()
        return r.json()["html_url"]

    return outbox(meeting_id, "github", config.GITHUB_REPO, f"{task['title']}\n\n{body}",
                  send if config.GITHUB_TOKEN else None)


# ---------------------------------------------------------------- Slack

def slack_message(meeting_id: str, text: str, handle: str | None = None, files: list[str] | None = None) -> dict:
    """DM `handle` (by Slack username) if known, otherwise post to the team channel.

    Sending goes through the Slack teammate's helper (src/slack/messaging.py), which resolves
    usernames/channel names to IDs and opens DMs.
    """
    user = _user(handle)
    target = user.get("slack_user_id") or config.SLACK_CHANNEL
    if handle and not user.get("slack_user_id"):
        text = f"*For {user.get('name', handle)}:*\n{text}"
    if config.SLACK_ONLY_TO and target != config.SLACK_ONLY_TO:
        text = f"_[test redirect: would go to {target}]_\n{text}"
        target = config.SLACK_ONLY_TO

    def send():
        from src.slack.messaging import send_message  # imported lazily: it needs SLACK_BOT_TOKEN at import
        send_message(recipients=[target], text=text, files=files)
        return "ok"

    body = text + (f"\n[files: {', '.join(files)}]" if files else "")
    return outbox(meeting_id, "slack", target, body, send if config.SLACK_BOT_TOKEN else None)


# ---------------------------------------------------------------- Calendar

def _ics(task: dict, start: datetime, end: datetime, emails: list[str]) -> str:
    fmt = "%Y%m%dT%H%M%S"
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//MeetingAgent//After//EN", "BEGIN:VEVENT",
        f"UID:{uuid.uuid4()}@meeting-agent",
        f"DTSTAMP:{datetime.now(timezone.utc).strftime(fmt)}Z",
        f"DTSTART;TZID={config.TIMEZONE}:{start.strftime(fmt)}",
        f"DTEND;TZID={config.TIMEZONE}:{end.strftime(fmt)}",
        f"SUMMARY:{task['title']}",
        "DESCRIPTION:" + "\\n".join(["Agenda:"] + [f"- {a}" for a in task.get("agenda", [])]),
        *[f"ATTENDEE;RSVP=TRUE:mailto:{e}" for e in emails],
        "END:VEVENT", "END:VCALENDAR",
    ]
    return "\r\n".join(lines)


def schedule_meeting(meeting_id: str, task: dict) -> dict:
    if not task.get("start_datetime"):
        return {"ok": False, "detail": "No time was given in the meeting; needs a human to pick one."}
    start = datetime.fromisoformat(task["start_datetime"])
    end = start + timedelta(minutes=task.get("duration_minutes") or 30)
    emails = [u["email"] for u in db.team() if u["handle"] in task.get("attendees", []) and u.get("email")]

    ics_path = config.OUTPUT_DIR / "invites" / f"{meeting_id}-next.ics"
    ics_path.parent.mkdir(parents=True, exist_ok=True)
    ics_path.write_text(_ics(task, start, end, emails))
    result = {"ok": True, "ics": f"/output/invites/{ics_path.name}", "start": start.isoformat(), "end": end.isoformat()}

    if config.GOOGLE_TOKEN_FILE.exists():
        def send():
            from .google_setup import calendar_service
            event = {
                "summary": task["title"],
                "description": "Agenda:\n" + "\n".join(f"- {a}" for a in task.get("agenda", [])),
                "start": {"dateTime": start.isoformat(), "timeZone": config.TIMEZONE},
                "end": {"dateTime": end.isoformat(), "timeZone": config.TIMEZONE},
                "attendees": [{"email": e} for e in emails],
                "conferenceData": {"createRequest": {"requestId": uuid.uuid4().hex}},
            }
            created = calendar_service().events().insert(
                calendarId="primary", body=event, conferenceDataVersion=1, sendUpdates="all"
            ).execute()
            return created.get("htmlLink")
        result["calendar"] = outbox(meeting_id, "calendar", ", ".join(emails), json.dumps(task), send)
    else:
        result["calendar"] = outbox(meeting_id, "calendar", ", ".join(emails), f".ics only: {ics_path.name}")
    return result


# ---------------------------------------------------------------- Darwin

def _darwin_queries(query: str) -> list[str]:
    """Darwin 500s on some longer phrasings; fall back to shorter verb + object forms."""
    words = [w for w in query.split() if w.lower() not in {"a", "an", "the", "for", "of", "to"}]
    out = [query]
    for n in (3, 2):
        short = " ".join(words[:n])
        if len(words) > n and short not in out:
            out.append(short)
    return out


def darwin_search(query: str, max_results: int = 3) -> dict:
    """Find agents on the agentic web that could do this task. Search only: we never act through Darwin."""
    headers = {"Content-Type": "application/json"}
    if config.DARWIN_API_KEY:
        headers["Authorization"] = f"Bearer {config.DARWIN_API_KEY}"
    data, error = None, None
    for q in _darwin_queries(query):
        try:
            r = httpx.post("https://darwin.so/api/v3/search", headers=headers,
                           json={"query": q, "maxResults": max_results}, timeout=30)
            if r.status_code >= 500:
                error = f"Darwin returned {r.status_code}"
                time.sleep(1)
                continue
            r.raise_for_status()
            data, query = r.json(), q
            break
        except Exception as e:
            error = f"Darwin search failed: {e}"
    if data is None:
        return {"query": query, "error": error, "agents": []}
    resp = data.get("response") or {}
    agents = resp.get("agents") or []
    return {
        "query": query,
        "status": data.get("status"),
        # needs_input: Darwin asks a clarifying question instead of returning matches
        "error": None if agents else (resp.get("question") or resp.get("overview") or "No matching agents"),
        "agents": [
            {
                "name": a.get("name"),
                "description": (a.get("description") or "")[:240],
                "readiness": a.get("readiness"),
                "why": (a.get("reasons") or [""])[0],
            }
            for a in agents
        ],
    }
