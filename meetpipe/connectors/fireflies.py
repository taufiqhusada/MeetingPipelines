"""Fireflies.ai meeting transcripts via its GraphQL API (one request per run; free plan allows 50/day)."""

from __future__ import annotations

import requests

from ..config import settings
from ..models import ContextItem, RunScope, SourceNotConfigured, parse_ts
from ._agentlog import clip

API = "https://api.fireflies.ai/graphql"
QUERY = """
query Recent($limit: Int) {
  transcripts(limit: $limit) {
    id title date duration organizer_email participants transcript_url
    summary { overview action_items keywords }
    sentences { speaker_name text start_time }
  }
}"""


def _post(query: str, variables: dict) -> dict:
    if not settings.fireflies_api_key:
        raise SourceNotConfigured("no FIREFLIES_API_KEY in .env")
    r = requests.post(API, json={"query": query, "variables": variables}, timeout=60,
                      headers={"Authorization": f"Bearer {settings.fireflies_api_key}"})
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or data.get("errors"):
        msgs = "; ".join(e.get("message", "") for e in data.get("errors", [])) or r.text[:300]
        raise RuntimeError(f"Fireflies API {r.status_code}: {msgs}")
    return data["data"]


def recent(limit: int = 25) -> list[dict]:
    return _post(QUERY, {"limit": limit})["transcripts"] or []


def _lines(t: dict) -> list[str]:
    out, last = [], None
    for s in t.get("sentences") or []:  # merge consecutive sentences by the same speaker
        who = s.get("speaker_name") or "Unknown"
        if who == last and out:
            out[-1] += " " + s["text"]
        else:
            out.append(f"{who}: {s['text']}")
        last = who
    return out


def _as_list(v) -> list[str]:
    if not v:
        return []
    return [x.strip("-• ").strip() for x in (v.splitlines() if isinstance(v, str) else v) if str(x).strip()]


def fetch(scope: RunScope) -> list[ContextItem]:
    items = []
    for t in recent():
        when = parse_ts(t.get("date"))
        if not when or when < scope.since:
            continue
        s = t.get("summary") or {}
        parts = [f"Organizer: {t.get('organizer_email') or '-'} | Participants: {', '.join(t.get('participants') or []) or '-'}"
                 f" | Duration: {round(t.get('duration') or 0)} min"]
        if s.get("overview"):
            parts.append("Fireflies summary:\n" + clip(s["overview"], 2000))
        if actions := _as_list(s.get("action_items")):
            parts.append("Fireflies action items:\n" + "\n".join(f"- {a}" for a in actions))
        parts.append("Transcript:\n" + "\n".join(_lines(t)))
        items.append(ContextItem(
            id=f"FF-{t['id'][:6]}", source="fireflies", kind="meeting", title=t.get("title") or "Fireflies meeting",
            body="\n\n".join(parts), created_at=when, url=t.get("transcript_url"),
            author=t.get("organizer_email"), meta={"fireflies_id": t["id"]},
        ))
    return items


def to_after_transcript(t: dict) -> dict:
    """Fireflies transcript → the `Transcript` JSON the After stage accepts (after/schemas.py)."""
    when = parse_ts(t.get("date"))
    return {
        "meeting_id": f"fireflies-{t['id']}",
        "title": t.get("title") or "Team sync",
        "date": when.date().isoformat() if when else "",
        "attendees": list(dict.fromkeys(s.get("speaker_name") for s in t.get("sentences") or [] if s.get("speaker_name"))),
        "lines": [{"speaker": s.get("speaker_name") or "Unknown", "text": s["text"], "t": s.get("start_time")}
                  for s in t.get("sentences") or []],
        "source": "fireflies",
    }
