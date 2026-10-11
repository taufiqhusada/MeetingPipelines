# After: post-meeting agent

Takes a meeting transcript and turns it into work:

1. **Summary**: decisions and open questions
2. **Progress check**: last meeting's goals vs. what was said now (achieved / partial / not started)
3. **Per-person update**: done, next and blockers for each engineer, sent as a Slack DM
4. **Human tasks**: title, owner, project role, due date and status, routed to **GitHub issues** (engineering) or a **Slack DM** (everything else).
   No owner → flagged `UNASSIGNED`. No deadline → flagged, plus a generated "set due date" action item.
5. **Agent tasks**: queued for **approval** in the UI, then executed:
   - `schedule_meeting` → Google Calendar event with a Meet link (or an `.ics` invite until Google is set up)
   - `meeting_report` → one-page visual HTML report, link posted to Slack
   - `other` (purchases, etc.) → **Darwin Search** finds agents that could do it; a human completes it (we never act through Darwin)
6. `next_meeting_goals` are saved, so the next meeting is compared against them.

```
transcript (paste / JSON / Recall.ai bot)
   → analyze.py      one structured Claude call → MeetingAnalysis (schemas.py)
   → pipeline.py     store (SQLite) → route human tasks → Darwin search → queue agent tasks
   → executors.py    GitHub · Slack · Calendar · Darwin  (all logged to the outbox)
   → report.py       visual report
   app.py + web/     FastAPI + one-page UI
```

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r after/requirements.txt
cp .env.example .env          # fill in what you have; blanks fall back to dry-run
.venv/bin/uvicorn after.app:app --reload
# open http://localhost:8000 → "Run demo meeting"
```

With no keys at all, the demo transcript runs on a canned analysis and every message lands in the outbox, so the whole flow is demoable offline.

## Tests

```bash
.venv/bin/python -m pytest after/tests -q   # offline: temp DB, no keys, DRY_RUN, Darwin stubbed
```

Sample transcripts for manual testing are in `after/seed/transcripts/` (pick one with **Load sample** in the UI):
`feature_test.txt` exercises every feature, and `before_kickoff_sample.vtt` is the Before stage's VTT format.
Set `SLACK_ONLY_TO=<your slack username>` to redirect every Slack message to yourself while testing live.

## Setup flags (shown at the top of the UI)

| Key | Without it |
|---|---|
| `ANTHROPIC_API_KEY` | Only the demo transcript works (canned analysis) |
| `RECALL_API_KEY` | No live-meeting bot; paste transcripts instead |
| `SLACK_BOT_TOKEN` | Slack messages only logged (waiting on the Slack teammate) |
| `GITHUB_TOKEN` | Issues only logged |
| Google Calendar | `.ics` invite only. Setup steps are in `after/google_setup.py`, then run `python -m after.google_setup` |
| `DRY_RUN=1` (default) | Nothing leaves the machine, even with keys set |

## API

| Method | Path | |
|---|---|---|
| POST | `/api/process/demo` | Run the seeded demo meeting |
| POST | `/api/process/text` | `{text, meeting_id, title?, date?}`: `Speaker: words` lines |
| POST | `/api/process/json` | A `Transcript` object (see `schemas.py`) |
| GET | `/api/meetings/{id}` | Full result |
| POST | `/api/agent-tasks/{id}/approve` or `/reject` | Approve or reject an agent task |
| POST | `/api/recall/bots` | `{meeting_url}`: send the Recall bot (free meeting captions) |
| GET | `/api/recall/bots/{bot_id}` | Bot status / transcript readiness |
| POST | `/api/recall/bots/{bot_id}/process` | Fetch the transcript and run the pipeline |

## Contracts for teammates

- **Before**: reads `meetings.goals_json` (the goals the next meeting is compared against) and `human_tasks.status`. Team roles live in `seed/team.json` → `users` table until the shared DB is decided.
- **Slack**: everything we'd send is in the `outbox` table (`channel`, `target`, `body`). Swap `executors.slack_message` for your integration.
- **During**: hand us a `Transcript` JSON (`meeting_id, title, date, attendees, lines[{speaker, text, t}]`).

## Open items
- Shared DB / Slack contract / Keewano: undecided (SQLite placeholder)
- Purchasing: deferred; only Darwin search, a human completes it
- Seed team members are fake. Set `GITHUB_ASSIGN=1` once real GitHub handles are in `seed/team.json`
