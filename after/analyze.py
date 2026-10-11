"""Transcript -> MeetingAnalysis with one structured Claude call."""
import json

import anthropic

from . import config, db
from .schemas import MeetingAnalysis, Transcript

SYSTEM = """You are the After stage of an engineering meeting agent. You read a meeting transcript and turn it \
into work: a summary, decisions, progress against last meeting's goals, a per-person update, and tasks.

Split every action item into exactly one of two kinds:
- human_tasks: work a person on the team must do (engineering, approvals, decisions, writing).
- agent_tasks: work the meeting agent itself was asked to do. kind=schedule_meeting when asked to book a meeting, \
kind=meeting_report when asked for a report/visual/recap, kind=other for anything else (purchases, research, bookings).

Rules:
- owner must be a handle from the team list, matched by name. If nobody clearly took the task, owner is null. \
Never guess an owner.
- due_date is an ISO date resolved against the meeting date ("Friday" = the next Friday after the meeting date). \
If no deadline was said, due_date is null. Do not invent deadlines.
- channel is "github" for code or engineering work in the repo, "slack" for everything else.
- project_role is the owner's role from the team list.
- source_quote is the shortest verbatim transcript snippet that justifies the item.
- goal_progress has one entry per previous goal, judged only from what was said in this meeting.
- person_updates has one entry per team member who attended.
- next_meeting_goals are the outcomes the team expects by the next meeting; the next meeting is compared against them.
- capability_query is 2-4 words, verb + object, naming the generic capability an outside agent would need, \
without names or dates (e.g. "create calendar event", "create slides", "order pizza").
- For schedule_meeting, attendees are team handles and start_datetime is ISO local time."""


def _prompt(t: Transcript) -> str:
    team = [{k: u[k] for k in ("handle", "name", "role")} for u in db.team()]
    lines = "\n".join(f"{l.speaker}: {l.text}" for l in t.lines)
    return (
        f"Meeting: {t.title}\nMeeting date: {t.date}\n\n"
        f"<team>\n{json.dumps(team, indent=1)}\n</team>\n\n"
        f"<previous_goals>\n{json.dumps(db.previous_goals(t.date, t.meeting_id), indent=1)}\n</previous_goals>\n\n"
        f"<transcript>\n{lines}\n</transcript>"
    )


def analyze(t: Transcript) -> tuple[MeetingAnalysis, str]:
    """Returns (analysis, engine). Falls back to the canned demo analysis when no API key is set."""
    if not config.ANTHROPIC_API_KEY:
        if t.source == "demo":
            data = json.loads((config.SEED_DIR / "demo_analysis.json").read_text())
            return MeetingAnalysis.model_validate(data), "canned-demo"
        raise RuntimeError("ANTHROPIC_API_KEY is not set; only the demo transcript works without it.")

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    response = client.messages.parse(
        model=config.CLAUDE_MODEL,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": _prompt(t)}],
        output_format=MeetingAnalysis,
        output_config={"effort": "medium"},
        # Server-side fallback: if a safety classifier declines, the API retries on a recommended model.
        extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
        extra_body={"fallbacks": "default"},
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined: {response.stop_details}")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise RuntimeError(f"Analysis incomplete (stop_reason={response.stop_reason})")
    return response.parsed_output, config.CLAUDE_MODEL
