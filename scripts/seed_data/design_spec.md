MeetingPipelines — Design Spec v1

Owner: Taufiq
Status: Draft for review
Last updated: Oct 7, 2026

1. Problem
People spend 15–30 minutes before every sync digging through PRs, issues, docs, chat and their own
coding-agent history to remember what happened since the last meeting. Commitments made in meetings
are forgotten or never checked.

2. Goal
Before a meeting, automatically produce a brief that answers:
- What have we done so far?
- What did we commit to last time, and did it happen?
- What is missing (gaps)?
- What needs action or a decision?

3. Scope (v1, backend only)
R1. Coding-agent context: read Claude Code and Codex session logs locally for the project folder.
R2. GitHub: pull PRs (with reviews and comments), issues and commits for the meeting window.
R3. Google Drive: find design docs, specs and meeting notes relevant to the project without the user
    having to pick folders.
R4. Meeting transcripts: Google Meet transcripts, Gemini notes, and Zoom/Otter/Fireflies emails from Gmail.
R5. Secret redaction before anything is written to disk or sent to an LLM.
R6. Summarizer agent: progress, commitments vs evidence, gaps, action items, open questions — every
    line cites its source.
R7. Aggregator agent: merge briefs from several teammates into one team brief, surfacing
    dependencies and conflicts.
R8. One-click account connection (Google + GitHub) for every teammate; no manual token pasting.

4. Out of scope for v1 (planned v2)
R9.  Slack delivery: post the personal brief and the team brief into a channel.
R10. Calendar trigger: run automatically 30 minutes before a calendar event.
R11. After-meeting pipeline: turn the new transcript into tracked action items (GitHub issues).
R12. Multiple Google accounts per person.

5. Architecture
One-shot CLI per person (run.py run) → runs/<timestamp>/ with raw/, context.md, brief.json, brief.md.
No database. Each teammate runs locally because coding-agent logs only exist on their machine.
Only brief.json leaves the laptop.

6. Open questions
Q1. Where do briefs meet for aggregation: a Slack channel or a shared folder?
Q2. Do we need Google app verification, or is Testing mode (100 test users) enough for the hackathon?
Q3. Which LLM backend is the default: Claude Code or Codex?

7. Milestones
M1 (Oct 10): connectors R1–R4 working end to end on real data.
M2 (Oct 13): summarizer + aggregator, consent flow (R5–R8).
M3 (Oct 16): Slack delivery (R9) and demo.
