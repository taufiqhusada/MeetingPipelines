"""Small HTTPS-fronted job coordinator for Slack-triggered local context workers.

Run behind a TLS reverse proxy when accessed outside localhost. Worker and admin
credentials are bearer tokens configured separately on the coordinator and clients.
"""

from __future__ import annotations

import json
import hmac
import os
import re
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

MAX_BODY = 2 * 1024 * 1024
MAX_WAIT = 300
_changed = threading.Condition()


def _db_path() -> Path:
    return Path(os.path.expanduser(os.getenv("MEETPIPE_COORDINATOR_DB", "~/.meetpipe/coordinator.sqlite3")))


def _workers() -> dict[str, str]:
    """Parse MEETPIPE_WORKER_TOKENS as comma-separated worker_id=secret pairs."""
    value = os.getenv("MEETPIPE_WORKER_TOKENS", "")
    out = {}
    for entry in value.split(","):
        if not entry.strip():
            continue
        worker_id, sep, token = entry.strip().partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", worker_id) or not token:
            raise RuntimeError("MEETPIPE_WORKER_TOKENS entries must be worker_id=token")
        if token in out.values():
            raise RuntimeError("Each worker must have a unique token")
        out[worker_id] = token
    return out


def _init_db() -> None:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, created REAL NOT NULL, deadline REAL NOT NULL,
            since TEXT NOT NULL, repo TEXT, meeting TEXT
        );
        CREATE TABLE IF NOT EXISTS worker_jobs (
            job_id TEXT NOT NULL, worker_id TEXT NOT NULL, status TEXT NOT NULL,
            result_json TEXT, updated REAL NOT NULL,
            PRIMARY KEY(job_id, worker_id), FOREIGN KEY(job_id) REFERENCES jobs(id)
        );
        CREATE INDEX IF NOT EXISTS worker_jobs_queue ON worker_jobs(worker_id, status, updated);
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, worker_id TEXT NOT NULL,
            ts REAL NOT NULL, payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS events_job ON events(job_id, id);
        """)


def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(_db_path(), timeout=10)
    db.row_factory = sqlite3.Row
    return db


def _expire(db: sqlite3.Connection, job_id: str) -> None:
    row = db.execute("SELECT deadline FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row and row["deadline"] <= time.time():
        db.execute("UPDATE worker_jobs SET status='timed_out', updated=? "
                   "WHERE job_id=? AND status IN ('queued','running')", (time.time(), job_id))


def _job_view(db: sqlite3.Connection, job_id: str) -> dict | None:
    job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        return None
    _expire(db, job_id)
    db.commit()
    rows = db.execute("SELECT worker_id,status,result_json FROM worker_jobs WHERE job_id=? ORDER BY worker_id",
                      (job_id,)).fetchall()
    workers = []
    for row in rows:
        result = json.loads(row["result_json"]) if row["result_json"] else None
        workers.append({"worker_id": row["worker_id"], "status": row["status"], "result": result})
    states = [w["status"] for w in workers]
    done = all(s in {"complete", "failed", "timed_out"} for s in states)
    return {"id": job_id, "status": "complete" if done else "running", "deadline": job["deadline"],
            "workers": workers}


class Handler(BaseHTTPRequestHandler):
    server_version = "MeetpipeCoordinator/1"

    def log_message(self, *_args):
        # Avoid logging auth headers or private job content.
        pass

    def _send(self, code: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        if size <= 0 or size > MAX_BODY:
            raise ValueError("request body is empty or too large")
        obj = json.loads(self.rfile.read(size))
        if not isinstance(obj, dict):
            raise ValueError("request body must be a JSON object")
        return obj

    def _bearer(self) -> str:
        value = self.headers.get("Authorization", "")
        return value[7:] if value.startswith("Bearer ") else ""

    def _admin(self) -> bool:
        expected = os.getenv("MEETPIPE_COORDINATOR_ADMIN_TOKEN", "")
        return bool(expected) and hmac.compare_digest(self._bearer(), expected)

    def _worker(self) -> str | None:
        token = self._bearer()
        return next((worker_id for worker_id, secret in _workers().items()
                     if hmac.compare_digest(secret, token)), None)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/v1/jobs":
            if not self._admin():
                return self._send(401, {"error": "unauthorized"})
            workers = _workers()
            if not workers:
                return self._send(503, {"error": "no local workers configured"})
            try:
                body = self._body()
                timeout = max(30, min(int(body.get("timeout_seconds", 240)), 1800))
                job_id, now = uuid.uuid4().hex, time.time()
                since = str(body.get("since") or "14d")
                repo = str(body.get("repo") or "") or None
                meeting = str(body.get("meeting") or "") or None
            except (ValueError, TypeError, json.JSONDecodeError) as e:
                return self._send(400, {"error": str(e)})
            with _connect() as db:
                db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?)",
                           (job_id, now, now + timeout, since, repo, meeting))
                db.executemany("INSERT INTO worker_jobs VALUES (?,?, 'queued', NULL, ?)",
                               [(job_id, name, now) for name in workers])
            with _changed:
                _changed.notify_all()
            return self._send(201, {"id": job_id, "deadline": now + timeout,
                                    "workers": list(workers)})

        match = re.fullmatch(r"/v1/worker/jobs/([a-f0-9]{32})/events", path)
        if match:  # live progress for the dashboard: source/stage updates, item titles + links only
            worker_id = self._worker()
            if not worker_id:
                return self._send(401, {"error": "unauthorized"})
            try:
                body = self._body()
                if len(json.dumps(body)) > 64_000:
                    raise ValueError("event too large")
            except (ValueError, TypeError, json.JSONDecodeError) as e:
                return self._send(400, {"error": str(e)})
            with _connect() as db:
                if not db.execute("SELECT 1 FROM worker_jobs WHERE job_id=? AND worker_id=?",
                                  (match.group(1), worker_id)).fetchone():
                    return self._send(404, {"error": "job not assigned to this worker"})
                db.execute("INSERT INTO events (job_id, worker_id, ts, payload_json) VALUES (?,?,?,?)",
                           (match.group(1), worker_id, time.time(), json.dumps(body, ensure_ascii=False)))
            return self._send(200, {"ok": True})

        match = re.fullmatch(r"/v1/worker/jobs/([a-f0-9]{32})/result", path)
        if match:
            worker_id = self._worker()
            if not worker_id:
                return self._send(401, {"error": "unauthorized"})
            try:
                body = self._body()
                encoded = json.dumps(body, ensure_ascii=False)
            except (ValueError, TypeError, json.JSONDecodeError) as e:
                return self._send(400, {"error": str(e)})
            job_id = match.group(1)
            with _connect() as db:
                db.execute("BEGIN IMMEDIATE")
                _expire(db, job_id)
                row = db.execute("SELECT status FROM worker_jobs WHERE job_id=? AND worker_id=?",
                                 (job_id, worker_id)).fetchone()
                if not row:
                    return self._send(404, {"error": "job not assigned to this worker"})
                if row["status"] != "running":
                    return self._send(409, {"error": f"job is {row['status']}"})
                status = "failed" if body.get("error") else "complete"
                db.execute("UPDATE worker_jobs SET status=?, result_json=?, updated=? "
                           "WHERE job_id=? AND worker_id=?",
                           (status, encoded, time.time(), job_id, worker_id))
            with _changed:
                _changed.notify_all()
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})

    def do_GET(self):
        path = urlparse(self.path)
        if path.path == "/v1/worker/jobs/next":
            worker_id = self._worker()
            if not worker_id:
                return self._send(401, {"error": "unauthorized"})
            try:
                wait = max(0, min(int(parse_qs(path.query).get("wait", [MAX_WAIT])[0]), MAX_WAIT))
            except ValueError:
                return self._send(400, {"error": "wait must be an integer"})
            end = time.monotonic() + wait
            while True:
                # Hold the condition lock while checking the DB so a job cannot
                # be inserted between the empty check and the wait notification.
                with _changed:
                    with _connect() as db:
                        db.execute("BEGIN IMMEDIATE")
                        row = db.execute(
                            "SELECT w.job_id,j.since,j.repo,j.meeting,j.deadline "
                            "FROM worker_jobs w JOIN jobs j ON j.id=w.job_id "
                            "WHERE w.worker_id=? AND w.status='queued' AND j.deadline>? "
                            "ORDER BY j.created LIMIT 1", (worker_id, time.time())).fetchone()
                        if row:
                            db.execute("UPDATE worker_jobs SET status='running',updated=? "
                                       "WHERE job_id=? AND worker_id=? AND status='queued'",
                                       (time.time(), row["job_id"], worker_id))
                            db.commit()
                            job = {"id": row["job_id"], "since": row["since"], "repo": row["repo"],
                                   "meeting": row["meeting"], "deadline": row["deadline"]}
                        else:
                            db.commit()
                            job = None
                    if job:
                        return self._send(200, {"job": job})
                    remaining = end - time.monotonic()
                    if remaining <= 0:
                        return self._send(200, {"job": None})
                    _changed.wait(timeout=remaining)

        match = re.fullmatch(r"/v1/jobs/([a-f0-9]{32})/events", path.path)
        if match:
            if not self._admin():
                return self._send(401, {"error": "unauthorized"})
            try:
                after = int(parse_qs(path.query).get("after", ["0"])[0])
            except ValueError:
                return self._send(400, {"error": "after must be an integer"})
            with _connect() as db:
                rows = db.execute("SELECT id, worker_id, ts, payload_json FROM events WHERE job_id=? AND id>? "
                                  "ORDER BY id LIMIT 500", (match.group(1), after)).fetchall()
            return self._send(200, {"events": [{"id": r["id"], "actor": r["worker_id"], "ts": r["ts"],
                                                **json.loads(r["payload_json"])} for r in rows]})

        match = re.fullmatch(r"/v1/jobs/([a-f0-9]{32})", path.path)
        if match:
            if not self._admin():
                return self._send(401, {"error": "unauthorized"})
            with _connect() as db:
                view = _job_view(db, match.group(1))
            if view is None:
                return self._send(404, {"error": "unknown job"})
            return self._send(200, view)
        if path.path == "/health":
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})


def serve(host: str = "127.0.0.1", port: int = 8766) -> None:
    if not os.getenv("MEETPIPE_COORDINATOR_ADMIN_TOKEN"):
        raise RuntimeError("Set MEETPIPE_COORDINATOR_ADMIN_TOKEN before starting the coordinator")
    if not _workers():
        raise RuntimeError("Set MEETPIPE_WORKER_TOKENS=worker_id=token[,worker_id=token...] before starting")
    _init_db()
    print(f"MeetingPipelines coordinator listening on {host}:{port}; put remote access behind HTTPS", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


def create_job(base_url: str, token: str, payload: dict) -> dict:
    from .coordinator_client import request_json
    return request_json(base_url, token, "POST", "/v1/jobs", payload)


def get_job(base_url: str, token: str, job_id: str) -> dict:
    from .coordinator_client import request_json
    return request_json(base_url, token, "GET", f"/v1/jobs/{job_id}")


def get_events(base_url: str, token: str, job_id: str, after: int = 0) -> list[dict]:
    from .coordinator_client import request_json
    return request_json(base_url, token, "GET", f"/v1/jobs/{job_id}/events?after={after}")["events"]
