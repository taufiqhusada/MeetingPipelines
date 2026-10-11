"""Data contracts. `Transcript` is what During/Recall hands us; `MeetingAnalysis` is what Claude returns."""
from typing import Literal, Optional

from pydantic import BaseModel, Field


class TranscriptLine(BaseModel):
    speaker: str
    text: str
    t: Optional[float] = None  # seconds from start


class Transcript(BaseModel):
    meeting_id: str
    title: str = "Team sync"
    date: str  # ISO date, used to resolve "Friday", "next week", ...
    attendees: list[str] = Field(default_factory=list)
    lines: list[TranscriptLine]
    source: str = "manual"  # manual | recall | demo


class GoalProgress(BaseModel):
    goal: str
    owner: Optional[str]
    status: Literal["achieved", "partial", "not_started", "dropped"]
    evidence: str


class Decision(BaseModel):
    decision: str
    rationale: str


class PersonUpdate(BaseModel):
    person: str
    done: list[str]
    next: list[str]
    blockers: list[str]


class HumanTask(BaseModel):
    title: str
    owner: Optional[str] = Field(description="Team member handle, or null if nobody took it")
    project_role: Optional[str]
    due_date: Optional[str] = Field(description="ISO date, or null if no deadline was said")
    status: Literal["todo", "in_progress", "blocked", "done"]
    channel: Literal["github", "slack"] = Field(description="github for code/engineering work, slack otherwise")
    source_quote: str


class AgentTask(BaseModel):
    title: str
    kind: Literal["schedule_meeting", "meeting_report", "other"]
    details: str
    capability_query: str = Field(description="Short generic capability phrase used to search for agents")
    requested_by: Optional[str]
    source_quote: str
    start_datetime: Optional[str] = Field(description="schedule_meeting only: ISO local datetime")
    duration_minutes: Optional[int]
    attendees: list[str]
    agenda: list[str]


class MeetingAnalysis(BaseModel):
    summary: str
    decisions: list[Decision]
    open_questions: list[str]
    goal_progress: list[GoalProgress]
    person_updates: list[PersonUpdate]
    human_tasks: list[HumanTask]
    agent_tasks: list[AgentTask]
    next_meeting_goals: list[str]
