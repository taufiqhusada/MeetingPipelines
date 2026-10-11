"""`run.py dashboard`: live web UI on the master: context graph, interactive brief, task tracker.

Live mode drives the same flow as the Slack bot (coordinator job → workers + shared sources → merge) and
streams progress events to the page. `--demo` replays a scripted run from local data (latest brief in
runs/, the e2e demo's After DB) so the UI can be shown without workers.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import ROOT, settings

WEB = Path(__file__).parent / "web"
RUNS: dict[str, dict] = {}
LOCK = threading.Lock()


# ------------------------------------------------------------------ run records

def _new_run(mode: str, opts: dict) -> dict:
    run = {"id": uuid.uuid4().hex[:12], "mode": mode, "opts": opts, "started": time.time(), "status": "running",
           "job_id": None, "workers": {}, "events": [], "brief": None, "index": {}, "error": None}
    with LOCK:
        RUNS[run["id"]] = run
        RUNS["latest"] = run
    return run


def _emit(run: dict, event: dict) -> None:
    with LOCK:
        event = {"seq": len(run["events"]) + 1, "ts": event.get("ts") or time.time(),
                 "actor": event.get("actor") or "master", **event}
        run["events"].append(event)
        if event.get("kind") == "job":
            run["job_id"] = event["job_id"]
            run["workers"] = {w: "queued" for w in event.get("workers", [])}
        if event.get("kind") == "worker_status":
            run["workers"][event["actor"]] = event["status"]


# ------------------------------------------------------------------ live mode

def _live(run: dict) -> None:
    from .coordinator import get_events, get_job
    from .runner import make_scope
    from .slack_bot import _collect_team_brief

    url, token = os.getenv("MEETPIPE_COORDINATOR_URL", ""), os.getenv("MEETPIPE_COORDINATOR_ADMIN_TOKEN", "")
    stop = threading.Event()

    def poll_workers():  # forward worker statuses + their live events from the coordinator
        after, last = 0, {}
        while not stop.is_set():
            job_id = run["job_id"]
            if job_id:
                try:
                    for e in get_events(url, token, job_id, after):
                        after = e.pop("id")
                        _emit(run, e)
                    for w in get_job(url, token, job_id).get("workers", []):
                        if last.get(w["worker_id"]) != w["status"]:
                            last[w["worker_id"]] = w["status"]
                            _emit(run, {"kind": "worker_status", "actor": w["worker_id"], "status": w["status"]})
                except Exception as e:
                    _emit(run, {"kind": "log", "text": f"coordinator poll failed: {e}"})
            stop.wait(1.0)

    poller = threading.Thread(target=poll_workers, daemon=True)
    poller.start()
    try:
        scope = make_scope(run["opts"].get("path", "."), run["opts"].get("since", "14d"),
                           meeting=run["opts"].get("meeting"))
        result = _collect_team_brief(scope, run["opts"], run["opts"].get("llm", settings.llm),
                                     on_event=lambda e: _emit(run, e))
        time.sleep(1.5)  # let the poller flush the final worker events
        run["brief"], run["index"], run["result"] = result.brief, result.index, result
        run["status"] = "done"
        _emit(run, {"kind": "stage", "stage": "done", "actor": "team"})
    except Exception as e:
        run["status"], run["error"] = "error", f"{type(e).__name__}: {e}"
        _emit(run, {"kind": "stage", "stage": "error", "actor": "team", "reason": run["error"]})
    finally:
        stop.set()


# ------------------------------------------------------------------ demo mode

DEMO_PEOPLE = {
    "taufik": {"claude_code": ["meetpipe connectors + consent page", "Slack prep bot", "per-person team brief"],
               "codex": [],
               "gmail": ["Meeting notes: MeetingPipelines kickoff (Oct 6)",
                         "Transcript: MeetingPipelines design review (Oct 9)"],
               "drive": ["MeetingPipelines — Design Spec v1"],
               "fireflies": ["Meet 1"]},
    "michael": {"claude_code": ["coordinator long-poll + worker tokens", "LAN worker connections"],
                "codex": ["Slack wrapper send_message + directory"],
                "gmail": [], "drive": ["Aggregation notes"], "fireflies": []},
    "george": {"claude_code": ["After stage: Darwin Act support", "After: tests + transcript picker"],
               "codex": [], "gmail": ["Recap: After stage review"], "drive": [], "fireflies": []},
}
DEMO_SHARED = {"github": ["PR #1: Add After stage", "PR #7: meetpipe context backend", "Issue #2: Slack wrapper",
                          "Issue #4: Aggregator inbox", "Issue #5: Codex 401", "PR #8: multiple local agents"]}


def _demo_brief() -> tuple[dict | None, dict]:
    """Latest per-person brief found locally (runs/), else None (the UI shows the graph only)."""
    candidates = [ROOT / "runs" / "people_test.json"] + sorted((ROOT / "runs").glob("20*/brief.json"), reverse=True)
    for path in candidates:
        if path.exists():
            brief = json.loads(path.read_text())
            if brief.get("people"):
                # Resolve every cited "label:ID" against the local run manifests (which store bare IDs).
                local = {}
                for manifest in sorted((ROOT / "runs").glob("20*/manifest.json"), reverse=True)[:30]:
                    for k, v in json.loads(manifest.read_text()).get("items", {}).items():
                        local.setdefault(k, v)
                cited = set()
                def walk(x):
                    if isinstance(x, dict):
                        for k, v in x.items():
                            if k in ("evidence",) and isinstance(v, list):
                                cited.update(e for e in v if isinstance(e, str) and ":" in e)
                            else:
                                walk(v)
                    elif isinstance(x, list):
                        for v in x:
                            walk(v)
                walk(brief)
                index = {c: local[c.split(":", 1)[1]] for c in cited if c.split(":", 1)[1] in local}
                return brief, index
    return None, {}


def _demo_items(index: dict) -> dict[str, dict[str, list[dict]]]:
    """actor -> source -> items. Real items from the local brief's citation index (so brief evidence lines up
    with graph nodes), topped up with dummy items for teammates without local data."""
    out: dict[str, dict[str, list[dict]]] = {}
    for key, meta in index.items():
        label, _, item_id = key.partition(":")
        actor = "master" if label == "shared" else label
        if actor not in DEMO_PEOPLE and actor != "master":
            continue
        out.setdefault(actor, {}).setdefault(meta.get("source") or "local", []).append(
            {"id": item_id, "title": meta.get("title") or item_id, "url": meta.get("url"), "kind": meta.get("kind")})
    for actor, sources in list(DEMO_PEOPLE.items()) + [("master", DEMO_SHARED)]:
        for source, names in sources.items():
            have = out.setdefault(actor, {}).setdefault(source, [])
            if not have:
                have += [{"id": f"{source[:2].upper()}-{actor[:2]}{i}", "title": t, "url": None,
                          "kind": {"github": "pr", "gmail": "meeting", "drive": "doc", "fireflies": "meeting"}.get(source, "session")}
                         for i, t in enumerate(names)]
    return out


def _demo(run: dict) -> None:
    brief, index = _demo_brief()
    items = _demo_items(index)
    workers = list(DEMO_PEOPLE)
    _emit(run, {"kind": "job", "job_id": "demo", "workers": workers})
    _emit(run, {"kind": "stage", "stage": "gathering", "actor": "master"})
    plan = []  # (delay, event), interleaved across laptops
    for actor, sources in items.items():
        t = random.uniform(0.3, 1.2) if actor != "master" else 0.1
        if actor != "master":
            plan.append((t, {"kind": "worker_status", "actor": actor, "status": "running"}))
            plan.append((t + 0.1, {"kind": "stage", "stage": "gathering", "actor": actor}))
        for source in sources:
            plan.append((t + 0.3, {"kind": "source", "source": source, "state": "fetching", "actor": actor}))
        for source, its in sources.items():
            t += random.uniform(0.9, 2.4)
            plan.append((t, {"kind": "source", "source": source, "state": "ok" if its else "skipped",
                             "items": len(its), "titles": its[:25], "actor": actor}))
        if actor != "master":
            t += 0.5
            plan.append((t, {"kind": "stage", "stage": "summarizing", "actor": actor}))
            t += random.uniform(2.5, 5)
            plan.append((t, {"kind": "stage", "stage": "done", "actor": actor}))
            plan.append((t + 0.1, {"kind": "worker_status", "actor": actor, "status": "complete"}))
    start = time.time()
    for delay, event in sorted(plan, key=lambda p: p[0]):
        time.sleep(max(0, start + delay - time.time()))
        _emit(run, event)
    _emit(run, {"kind": "stage", "stage": "merging", "actor": "master", "briefs": ["shared", *workers]})
    time.sleep(3)
    run["brief"], run["index"] = brief, index
    run["status"] = "done"
    _emit(run, {"kind": "stage", "stage": "done", "actor": "team"})


# ------------------------------------------------------------------ starting runs (button or Slack)

def start_run(opts: dict, demo: bool) -> tuple[dict | None, str | None]:
    latest = RUNS.get("latest")
    if latest and latest["status"] == "running":
        return None, "a run is already in progress"
    run = _new_run("demo" if demo else "live", opts)
    threading.Thread(target=_demo if demo else _live, args=(run,), daemon=True).start()
    return run, None


def dashboard_url(port: int) -> str:
    if url := os.getenv("MEETPIPE_DASHBOARD_URL"):
        return url.rstrip("/")
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
    except OSError:
        ip = "localhost"
    return f"http://{ip}:{port}"


def serve_slack(server) -> None:
    """`@agent generate meeting prep` in Slack starts the same run the dashboard animates, then posts the brief."""
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    from .slack_bot import HELP, parse_command, post_brief, source_summary

    app = App(token=os.environ["SLACK_BOT_TOKEN"])
    link = dashboard_url(server.server_port)

    def follow(run: dict, channel: str, thread_ts: str, status_ts: str, client) -> None:
        while run["status"] == "running":
            time.sleep(1)
        result = run.get("result")
        if run["status"] != "done" or not result or not result.markdown:
            client.chat_update(channel=channel, ts=status_ts,
                               text=f"❌ Meeting prep failed: `{run.get('error') or 'no brief produced'}`")
            return
        client.chat_update(channel=channel, ts=status_ts,
                           text=f"✅ Brief ready. Sources: {source_summary(result.status)} · <{link}|open the live graph>")
        post_brief(channel, result, thread_ts=thread_ts, client=client)

    @app.event("app_mention")
    def on_mention(event, client, say):
        opts = parse_command(event.get("text", ""))
        thread_ts = event.get("thread_ts") or event["ts"]
        if opts is None:
            return say(text="Try `@agent generate meeting prep` (or `@agent help`).", thread_ts=thread_ts)
        if opts.get("help"):
            return say(text=HELP, thread_ts=thread_ts)
        run, err = start_run({"since": opts.get("since", "14d"), "meeting": opts.get("meeting"),
                              "path": server.path, "llm": server.llm}, server.demo)
        if err:
            return say(text=f"I'm already preparing a brief. Watch it live: {link}", thread_ts=thread_ts)
        msg = client.chat_postMessage(channel=event["channel"], thread_ts=thread_ts,
                                      text=f"On it <@{event.get('user', '')}>! Asking every laptop for context… "
                                           f"watch it live: <{link}|meeting prep graph>")
        threading.Thread(target=follow, args=(run, event["channel"], thread_ts, msg["ts"], client), daemon=True).start()

    @app.event("message")
    def ignore_messages():
        pass

    print(f"Slack: listening for @agent mentions (dashboard link {link})", flush=True)
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()


# ------------------------------------------------------------------ tasks (After DB + brief actions)

def _after_db() -> Path:
    return Path(os.path.expanduser(os.getenv("AFTER_DB_PATH", str(settings.after_db_path))))


def _tasks() -> dict:
    tasks, path = [], _after_db()
    if path.exists():
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        try:
            for t in db.execute("SELECT t.*, m.title AS meeting_title, m.date AS meeting_date FROM human_tasks t "
                                "LEFT JOIN meetings m ON m.id = t.meeting_id ORDER BY t.id"):
                delivery = json.loads(t["delivery"] or "{}")
                detail = str(delivery.get("detail") or "")
                tasks.append({"id": t["id"], "origin": "after", "title": t["title"], "owner": t["owner"],
                              "due": t["due_date"], "status": t["status"], "channel": t["channel"],
                              "flags": json.loads(t["flags"] or "[]"), "quote": t["source_quote"],
                              "meeting": f"{t['meeting_title']} · {t['meeting_date']}",
                              "url": detail if detail.startswith("http") else None})
        finally:
            db.close()
    latest = RUNS.get("latest")
    for i, a in enumerate(((latest or {}).get("brief") or {}).get("action_items", [])):
        tasks.append({"id": f"brief-{i}", "origin": "brief", "title": a.get("what"), "owner": a.get("owner"),
                      "due": None, "status": "todo", "channel": None, "flags": [a.get("priority", "")],
                      "quote": None, "meeting": "Pre-meeting brief", "url": None})
    return {"tasks": tasks, "after_db": str(path), "after_db_found": path.exists()}


def _set_task_status(task_id: int, status: str) -> None:
    if status not in {"todo", "in_progress", "blocked", "done"}:
        raise ValueError("bad status")
    with sqlite3.connect(_after_db()) as db:
        db.execute("UPDATE human_tasks SET status=? WHERE id=?", (status, task_id))


# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def _send(self, code: int, body: bytes | dict, ctype: str = "application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", "0") or 0)
        return json.loads(self.rfile.read(n)) if 0 < n < 100_000 else {}

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self._send(200, (WEB / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/config":
            people = {}
            for pair in os.getenv("MEETPIPE_PEOPLE", "").split(","):
                alias, _, name = pair.partition("=")
                if alias.strip() and name.strip():
                    people[alias.strip()] = name.strip()
            return self._send(200, {"demo": self.server.demo, "llm": settings.llm, "people": people,
                                    "coordinator": bool(os.getenv("MEETPIPE_COORDINATOR_URL"))})
        if url.path.startswith("/api/runs/"):
            run = RUNS.get(url.path.rsplit("/", 1)[1])
            if not run:
                return self._send(404, {"error": "no run yet"})
            after = int(parse_qs(url.query).get("after", ["0"])[0])
            with LOCK:
                view = {k: v for k, v in run.items() if k not in ("events", "result")}
                view["events"] = run["events"][after:]
            if view["status"] != "done":
                view["brief"], view["index"] = None, {}
            return self._send(200, view)
        if url.path == "/api/tasks":
            return self._send(200, _tasks())
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        try:
            body = self._body()
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})
        if url.path == "/api/runs":
            opts = {"since": body.get("since") or "14d", "meeting": body.get("meeting") or None,
                    "path": self.server.path, "llm": self.server.llm}
            run, err = start_run(opts, bool(self.server.demo or body.get("demo")))
            if err:
                return self._send(409, {"error": err, "id": RUNS["latest"]["id"]})
            return self._send(201, {"id": run["id"]})
        if url.path.startswith("/api/tasks/"):
            try:
                _set_task_status(int(url.path.rsplit("/", 1)[1]), body.get("status", ""))
            except (ValueError, sqlite3.Error) as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})


def serve(host: str = "127.0.0.1", port: int = 8770, demo: bool = False, path: str = ".", llm: str = "claude",
          slack: bool = False) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    server.demo, server.path, server.llm = demo, os.path.abspath(path), llm
    if slack:
        threading.Thread(target=serve_slack, args=(server,), daemon=True).start()
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    print(f"Dashboard{' (demo mode)' if demo else ''}: http://{shown}:{port}  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
