# Local worker setup

Run commands from the repo root. Each device needs the repo dependencies installed and Claude or Codex CLI logged in.

### On your laptop—the master

Set these in its `.env`, alongside your existing Slack tokens:

```dotenv
MEETPIPE_COORDINATOR_URL=http://192.168.104.144:8766
MEETPIPE_ALLOW_INSECURE_HTTP=1
MEETPIPE_COORDINATOR_ADMIN_TOKEN=<random-admin-token>
MEETPIPE_WORKER_TOKENS=alice=<alice-token>,bob=<bob-token>
SLACK_BOT_TOKEN=<bot-token>
SLACK_APP_TOKEN=<app-token>
LLM=codex
```

Generate a separate random value for the coordinator admin token and each worker token:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Use your existing Slack app and bot tokens for `SLACK_APP_TOKEN` and `SLACK_BOT_TOKEN`.

Start the coordinator in one terminal:

```bash
.venv/bin/python run.py coordinator --host 0.0.0.0 --port 8766
```

Start the single Slack bot in another:

```bash
.venv/bin/python src/slack/app.py
```

### On each teammate’s device

Set their local `.env`:

```dotenv
MEETPIPE_COORDINATOR_URL=http://192.168.104.144:8766
MEETPIPE_ALLOW_INSECURE_HTTP=1
MEETPIPE_WORKER_ID=alice
MEETPIPE_WORKER_TOKEN=<alice-token>
MEETPIPE_WORKER_PATH=/absolute/path/to/the/project
LLM=codex
```

Their ID and token must match your master’s registry. Use `LLM=claude` if they summarize with Claude instead.

Run:

```bash
.venv/bin/python run.py worker
```

Run a worker on your laptop too if you want your own Claude/Codex context included; give yourself an ID/token in the registry.

### Trigger it

In Slack, mention the bot:

```text
@YourBot generate meeting prep since=7d
```

All devices must be able to reach the master’s IP, and the master must stay awake. Replace `192.168.104.144` on each device if the master’s IP changes. HTTP is unencrypted, so use a trusted LAN.
