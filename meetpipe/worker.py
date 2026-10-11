"""Long-poll worker that retrieves repo-scoped Claude/Codex context on demand."""

from __future__ import annotations

import os
import random
import time
from urllib.parse import quote

from .config import settings
from .coordinator_client import request_json, validate_url
from .runner import make_scope, prepare


def _post_result(base_url: str, token: str, job_id: str, payload: dict) -> None:
    request_json(base_url, token, "POST", f"/v1/worker/jobs/{quote(job_id)}/result", payload)


CODING_SOURCES = ("claude_code", "codex")
# Personal sources a worker reads with its owner's own logins; any that aren't connected are skipped.
DEFAULT_WORKER_SOURCES = "claude_code,codex,gmail,drive,fireflies"


def worker_sources(value: str | None = None) -> list[str]:
    raw = value or os.getenv("MEETPIPE_WORKER_SOURCES") or DEFAULT_WORKER_SOURCES
    return [s.strip() for s in raw.split(",") if s.strip()]


def _run_job(job: dict, path: str, llm_name: str, sources: list[str] | tuple = CODING_SOURCES) -> dict:
    scope = make_scope(path=path, since=job["since"], repo=job.get("repo"), meeting=job.get("meeting"))
    # Session logs, mail and docs stay on this device. Only the local model's compact
    # brief and its citation index are uploaded to the coordinator.
    result = prepare(scope, sources=list(sources), llm_name=llm_name,
                     log=lambda line: print(line, flush=True))
    return {"brief": result.brief, "index": result.index, "status": result.status}


def serve(path: str, llm_name: str | None = None, poll_wait: int = 285, sources: list[str] | None = None) -> None:
    llm_name = llm_name or settings.llm
    sources = sources or worker_sources()
    base_url = os.getenv("MEETPIPE_COORDINATOR_URL", "").rstrip("/")
    token = os.getenv("MEETPIPE_WORKER_TOKEN", "")
    worker_id = os.getenv("MEETPIPE_WORKER_ID", "")
    if not base_url or not token or not worker_id:
        raise RuntimeError("Set MEETPIPE_COORDINATOR_URL, MEETPIPE_WORKER_ID and MEETPIPE_WORKER_TOKEN")
    validate_url(base_url)
    poll_wait = max(10, min(poll_wait, 300))
    print(f"Local context worker '{worker_id}' watching {path}; sources={','.join(sources)}; "
          f"long-poll wait={poll_wait}s", flush=True)
    backoff = 2
    while True:
        try:
            response = request_json(base_url, token, "GET", f"/v1/worker/jobs/next?wait={poll_wait}",
                                    timeout=poll_wait + 15)
            backoff = 2
            job = response.get("job")
            if not job:
                continue
            print(f"Received meeting prep job {job['id']}", flush=True)
            try:
                result = _run_job(job, path, llm_name, sources)
            except Exception as e:
                result = {"error": f"{type(e).__name__}: {e}"}
            try:
                _post_result(base_url, token, job["id"], result)
                print(f"Submitted result for {job['id']}", flush=True)
            except Exception as e:
                print(f"Could not submit result for {job['id']}: {e}", flush=True)
        except KeyboardInterrupt:
            print("Worker stopped", flush=True)
            return
        except Exception as e:
            print(f"Coordinator connection failed: {e}; retrying in {backoff}s", flush=True)
            time.sleep(backoff + random.random())
            backoff = min(backoff * 2, 60)
