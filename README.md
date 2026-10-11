# Charlotte: connect your work
<img width="1351" height="837" alt="Screenshot 2026-10-11 at 4 46 24 PM" src="https://github.com/user-attachments/assets/013951d7-98af-4817-a01b-1aece428b614" />

<img width="1484" height="854" alt="Screenshot 2026-10-11 at 4 48 03 PM" src="https://github.com/user-attachments/assets/92786669-1cf5-4b73-947d-d842d7a7c19c" />
<img width="1474" height="849" alt="Screenshot 2026-10-11 at 4 47 53 PM" src="https://github.com/user-attachments/assets/c072d1ed-a5fb-4a8b-8965-5180c1c08aa8" />


**Connects your work.** Meeting agent for engineering teams: **Before** (context gathering) → **During** (live transcript) → **After** (summary, task delegation, agent actions).

- [`meetpipe/`](#before-stage-pre-meeting-context-meetpipe): pre-meeting context brief: coding-agent logs, GitHub, Gmail, Drive → progress, commitments vs. evidence, gaps, action items.
- [`after/`](after/README.md): post-meeting agent: summary, progress vs. last meeting, human tasks to GitHub/Slack, agent tasks (calendar, report, Darwin search) with approval.

## Before stage: pre-meeting context (`meetpipe`)

Pre-meeting context, gathered for you. One command pulls what the team has actually been doing
(Claude Code + Codex sessions, GitHub PRs/issues/commits, Drive docs, meeting transcripts from Gmail)
and has a local coding agent (Claude Code or Codex) write a brief: **progress, commitments vs. evidence,
gaps, and action items**, every line cited back to its source.

```
gather (parallel)            trim + redact             summarize (claude -p | codex exec)
claude_code  codex  github → runs/<ts>/raw/*.json  →  runs/<ts>/context.md  →  brief.json + brief.md
gmail  drive  local          manifest.json (status)                         aggregate → team_brief.md
```

## Quick start (teammate)

```bash
uv sync
cp .env.example .env               # paste the team app lines the app owner shared
uv run run.py connect              # browser opens → click "Connect Google" and "Connect GitHub"
uv run run.py doctor --ping        # every line should be ✅
uv run run.py run --since 7d --meeting "Weekly sync"
```

Output lands in `runs/<timestamp>/`: `brief.md` (readable), `brief.json` (for Slack/aggregation),
`context.md` (exactly what the LLM saw), `raw/` (per-source dumps), `manifest.json` (source status).

### `run` options

| flag | default | |
|---|---|---|
| `--path` | `.` | local repo; coding-agent sessions whose cwd is inside it are included |
| `--repo` | from git remote | GitHub `owner/name` |
| `--since` | `14d` | `7d`, `2w`, `48h`, or `2026-10-01` |
| `--meeting` | – | upcoming meeting title, steers Drive triage + summary focus |
| `--keywords` | – | extra hints for Drive triage, comma-separated |
| `--sources` | all | `claude_code,codex,github,gmail,drive,local` |
| `--llm` | `$LLM` / `claude` | `claude` or `codex` |
| `--no-llm` | – | gather only (keyword-based Drive triage, no summary) |

### From Slack

```bash
uv run run.py slack-bot            # keep running; needs SLACK_BOT_TOKEN + SLACK_APP_TOKEN in .env
```
Then in a channel the bot is in: `@agent generate meeting prep` (options: `since 7d`, `for "Weekly sync"`,
or `@agent help`). The pipeline runs on the bot host's laptop and the brief is posted in the thread
(+ `brief.md` attached). Only one bot process per Slack app should be running. From the terminal instead:
`uv run run.py run --post "#channel"`.

### Master dashboard

```bash
uv run run.py coordinator --host 0.0.0.0
uv run run.py dashboard --host 0.0.0.0 --llm codex --slack
```

The dashboard supports **Run live**, **Reset**, and **Replay**. A reset immediately clears the master UI and
allows another run; it cannot recall work already sent to teammates' local workers. Every dashboard event stream
is saved under `runs/dashboard/`, so a completed run can be replayed after the dashboard restarts.

### Merge several briefs (team view)

```bash
uv run run.py aggregate alice=runs/2026-10-11_0900 bob=path/to/bobs/run --meeting "Weekly sync"
```

## Sources

| source | how | auth |
|---|---|---|
| Claude Code | `~/.claude/projects/*/*.jsonl` → prompts, answers, files edited, commands | none |
| Codex | `~/.codex/sessions/**/rollout-*.jsonl` | none |
| GitHub | REST API: PRs (+reviews, comments), issues (+comments), commits | `GITHUB_TOKEN` |
| Gmail | emails from Meet / Gemini / Zoom / Otter / Fireflies or with transcript-ish subjects; body + `.txt/.vtt/.srt` attachments + linked Google Docs | Google OAuth |
| Drive | lists every file modified in the window (metadata only), the LLM picks ≤20 relevant ones, exports those | Google OAuth |
| Local | `./transcripts/*.{txt,md,vtt,srt}` | none |

Secrets (API keys, tokens, private keys, `KEY=value` assignments) are redacted before anything is written
to `context.md` or sent to the LLM.

## Setup

### How auth works

The team registers **one** Google OAuth client and **one** read-only GitHub App, once. Their IDs go in
`.env` and are shared with teammates. Each person then runs `uv run run.py connect` and clicks
**Connect** on a localhost page. That opens the normal Google/GitHub consent screens, and tokens are
saved per user in `~/.meetpipe/` (mode 600). Nobody pastes tokens.

### GitHub (one-time, app owner, about 1 minute)

```bash
uv run run.py setup-github
```
GitHub opens with a pre-filled read-only app (Contents, Issues, Pull requests). Click **Create GitHub App**.
`GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` and `GITHUB_APP_SLUG` are written to `.env` automatically. Then click
**Install on repos**. An org admin installs it once per org; personal repos are installed by their owner.

### Google (one-time, app owner, about 10 minutes, free)

1. https://console.cloud.google.com → create a project.
2. Enable the [Gmail API](https://console.cloud.google.com/apis/library/gmail.googleapis.com) and the
   [Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com).
3. [Google Auth Platform](https://console.cloud.google.com/auth/overview) → Get started: app name + support email.
   Audience **Internal** (Workspace org: anyone in the org can connect) or **External**.
4. External only: **Audience → Test users**, add each teammate's Google email (up to 100).
5. **Clients → Create client → Desktop app**. Copy the client ID + secret into `.env` as
   `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`.

Notes: `gmail.readonly` and `drive.readonly` are Google *restricted* scopes. External apps in Testing mode
work for up to 100 listed test users, and their logins expire every 7 days (just click Connect again). Opening
the app to anyone requires Google's app verification. Teammates will see "Google hasn't verified this app":
Advanced → Continue.

### LLM CLIs
Auto-detected (PATH, the VS Code extension's bundled `claude`, ChatGPT.app's bundled `codex`).
Override with `CLAUDE_BIN` / `CODEX_BIN` in `.env`. Both must be logged in
(`claude` → `/login`; `codex login`).
