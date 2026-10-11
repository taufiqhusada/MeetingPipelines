"""Local HTTP integration tests. No Slack messages, real logs, or model calls."""

import json
import os
import re
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import ANY, Mock, patch

from meetpipe import coordinator, slack_bot, worker
from meetpipe.config import settings
from meetpipe.coordinator_client import request_json
from meetpipe.runner import RunResult, make_scope


def sample_brief(summary="Implemented the worker", evidence="CX-123456"):
    return {"headline": summary, "progress": [{"summary": summary, "evidence": [evidence]}],
            "commitments": [], "gaps": [], "action_items": [], "open_questions": [],
            "coverage_notes": "Fixture context"}


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="meetpipe-test-")
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {
            "MEETPIPE_COORDINATOR_DB": str(self.root / "jobs.sqlite3"),
            "MEETPIPE_COORDINATOR_ADMIN_TOKEN": "fixture-admin",
            "MEETPIPE_WORKER_TOKENS": "alice=fixture-alice,bob=fixture-bob",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)
        coordinator._init_db()
        self.server = coordinator.ThreadingHTTPServer(("127.0.0.1", 0), coordinator.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        os.environ["MEETPIPE_COORDINATOR_URL"] = self.url

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.env.stop()
        self.tmp.cleanup()

    def create(self):
        return coordinator.create_job(self.url, "fixture-admin", {
            "since": "7d", "repo": "example/project", "meeting": "Weekly sync"})

    def claim(self, name, wait=0):
        return request_json(self.url, f"fixture-{name}", "GET",
                            f"/v1/worker/jobs/next?wait={wait}", timeout=wait + 3)["job"]

    def submit(self, name, job_id, payload=None):
        return request_json(self.url, f"fixture-{name}", "POST",
                            f"/v1/worker/jobs/{job_id}/result", payload or {"brief": sample_brief()})

    def test_long_poll_waits_and_wakes_all_workers(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = [pool.submit(self.claim, name, 3) for name in ("alice", "bob")]
            time.sleep(0.15)
            self.assertTrue(all(not p.done() for p in pending), "idle requests must remain open")
            started = time.monotonic()
            job = self.create()
            received = [p.result(timeout=2) for p in pending]
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual([j["id"] for j in received], [job["id"], job["id"]])
        self.assertEqual(received[0]["repo"], "example/project")
        self.assertEqual(received[0]["meeting"], "Weekly sync")
        self.assertIsNone(self.claim("alice"), "a claimed job must not be delivered twice")
        self.submit("alice", job["id"])
        self.submit("bob", job["id"], {"error": "Fixture model unavailable"})
        view = coordinator.get_job(self.url, "fixture-admin", job["id"])
        self.assertEqual(view["status"], "complete")
        self.assertEqual([w["status"] for w in view["workers"]], ["complete", "failed"])

    def test_idle_request_returns_empty_after_timeout(self):
        started = time.monotonic()
        self.assertIsNone(self.claim("alice", wait=1))
        self.assertGreaterEqual(time.monotonic() - started, 0.9)

    def test_auth_and_result_ownership(self):
        with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
            request_json(self.url, "wrong-token", "GET", "/v1/worker/jobs/next?wait=0")
        with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
            coordinator.create_job(self.url, "fixture-alice", {"since": "7d"})
        job = self.create()
        self.claim("alice")
        with self.assertRaisesRegex(RuntimeError, "HTTP 409"):
            self.submit("bob", job["id"])
        with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
            coordinator.get_job(self.url, "fixture-bob", job["id"])

    def test_offline_worker_times_out_and_is_not_delivered_stale_job(self):
        job = self.create()
        self.claim("alice")
        self.submit("alice", job["id"])
        with coordinator._connect() as db:
            db.execute("UPDATE jobs SET deadline=? WHERE id=?", (time.time() - 1, job["id"]))
        view = coordinator.get_job(self.url, "fixture-admin", job["id"])
        self.assertEqual([w["status"] for w in view["workers"]], ["complete", "timed_out"])
        self.assertIsNone(self.claim("bob"))

    def test_late_result_is_rejected_even_without_admin_status_poll(self):
        job = self.create()
        self.claim("alice")
        with coordinator._connect() as db:
            db.execute("UPDATE jobs SET deadline=? WHERE id=?", (time.time() - 1, job["id"]))
        with self.assertRaisesRegex(RuntimeError, "HTTP 409"):
            self.submit("alice", job["id"])

    def test_actual_log_retrieval_redaction_and_team_brief_posting(self):
        repo = self.root / "checkout"
        repo.mkdir()
        cc = self.root / "claude"
        cx = self.root / "codex"
        cc_dir = cc / re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(repo))
        cc_dir.mkdir(parents=True)
        cx.mkdir()
        ts = datetime.now(timezone.utc).isoformat()
        secret = "sk-proj-" + "x" * 30
        (cc_dir / "abc123-session.jsonl").write_text(json.dumps({
            "type": "user", "timestamp": ts, "cwd": str(repo),
            "message": {"content": "Build long polling using " + secret}}) + "\n")
        rows = [
            {"type": "session_meta", "payload": {"id": "fixture-123456", "cwd": str(repo)}},
            {"type": "response_item", "timestamp": ts, "payload": {
                "type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "Implemented queue delivery"}]}},
        ]
        (cx / "rollout-test.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
        rows[0]["payload"]["cwd"] = str(self.root / "unrelated")
        rows[1]["payload"]["content"][0]["text"] = "PRIVATE unrelated session"
        (cx / "rollout-unrelated.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
        prompts = []

        class FixtureLLM:
            def __init__(self, *_args):
                pass

            def json(self, prompt):
                prompts.append(prompt)
                if "=== BRIEFS START ===" in prompt:
                    return sample_brief("Team brief", "alice:CX-123456")
                return sample_brief()

        out = self.root / "shared"
        out.mkdir()
        shared = RunResult(out, {"github": {"status": "ok", "items": 1}},
                           {"GH-PR-1": {"url": "https://example.com/pr/1"}},
                           brief=sample_brief("Shared PR", "GH-PR-1"))
        captured_briefs = []
        real_aggregate = slack_bot.brief_mod.aggregate

        def merge(briefs, _llm):
            captured_briefs.append(briefs)
            return real_aggregate(briefs, _llm)

        def do_worker(name):
            job = self.claim(name, wait=3)
            if name == "alice":
                payload = worker._run_job(job, str(repo), "codex")
                self.assertEqual(payload["status"]["claude_code"]["items"], 1)
                self.assertEqual(payload["status"]["codex"]["items"], 1)
                self.assertEqual(set(payload), {"brief", "index", "status"})
            else:
                payload = {"brief": sample_brief("Bob implemented auth"), "index": {}, "status": {}}
            self.submit(name, job["id"], payload)

        with patch.multiple(settings, claude_projects_dir=cc, codex_sessions_dir=cx,
                            runs_dir=self.root / "runs"), \
             patch("meetpipe.runner.LLM", FixtureLLM), \
             patch("meetpipe.slack_bot.prepare", return_value=shared), \
             patch("meetpipe.slack_bot.LLM", FixtureLLM), \
             patch("meetpipe.slack_bot.brief_mod.aggregate", side_effect=merge):
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending = [pool.submit(do_worker, name) for name in ("alice", "bob")]
                result = slack_bot._collect_team_brief(
                    make_scope(str(repo), "7d", repo="example/project", meeting="Weekly sync"),
                    {"since": "7d"}, "codex")
                for p in pending:
                    p.result(timeout=3)
        self.assertNotIn(secret, "\n".join(prompts))
        self.assertNotIn("PRIVATE unrelated session", "\n".join(prompts))
        self.assertIn("[REDACTED]", "\n".join(prompts))
        self.assertIn('"alice:CX-123456"', "\n".join(prompts))
        self.assertEqual(set(captured_briefs[0]), {"alice", "bob", "shared"})
        self.assertEqual(result.status["workers"]["items"], 2)
        self.assertIn("alice:CX-123456", (result.run_dir / "brief.md").read_text())
        self.assertNotIn("Coverage:", (result.run_dir / "brief.md").read_text())
        client = Mock()
        slack_bot.post_brief("C123456", result, thread_ts="fixture-thread", client=client)
        self.assertTrue(client.chat_postMessage.called)
        self.assertEqual(client.files_upload_v2.call_args.kwargs["thread_ts"], "fixture-thread")
        self.assertTrue(Path(client.files_upload_v2.call_args.kwargs["file"]).exists())
        self.assertNotIn("Sources:", client.files_upload_v2.call_args.kwargs["initial_comment"])


class WorkerLoopTests(unittest.TestCase):
    def test_idle_reconnect_does_not_retrieve_until_job_arrives(self):
        job = {"id": "a" * 32, "since": "7d"}
        with patch.dict(os.environ, {
            "MEETPIPE_COORDINATOR_URL": "http://127.0.0.1:8766",
            "MEETPIPE_WORKER_TOKEN": "fixture-alice", "MEETPIPE_WORKER_ID": "alice",
        }), patch("meetpipe.worker.request_json", side_effect=[
            {"job": None}, {"job": job}, {"ok": True}, KeyboardInterrupt(),
        ]) as http, patch("meetpipe.worker._run_job", return_value={"brief": sample_brief()}) as retrieve:
            worker.serve("/fixture/checkout", "codex", poll_wait=10)
        retrieve.assert_called_once_with(job, "/fixture/checkout", "codex", worker.worker_sources(), on_event=ANY)
        self.assertEqual([call.args[2] for call in http.call_args_list], ["GET", "GET", "POST", "GET"])

    def test_retrieval_error_is_reported_and_worker_resumes_waiting(self):
        job = {"id": "b" * 32, "since": "7d"}
        with patch.dict(os.environ, {
            "MEETPIPE_COORDINATOR_URL": "http://127.0.0.1:8766",
            "MEETPIPE_WORKER_TOKEN": "fixture-alice", "MEETPIPE_WORKER_ID": "alice",
        }), patch("meetpipe.worker.request_json", side_effect=[
            {"job": job}, {"ok": True}, KeyboardInterrupt(),
        ]) as http, patch("meetpipe.worker._run_job", side_effect=RuntimeError("fixture failure")):
            worker.serve("/fixture/checkout", "codex", poll_wait=10)
        self.assertIn("fixture failure", http.call_args_list[1].args[4]["error"])
        self.assertEqual(http.call_args_list[2].args[2], "GET")

    def test_remote_plain_http_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "HTTPS"):
            request_json("http://remote.example.com", "fixture", "GET", "/health")

    def test_lan_http_requires_opt_in_and_sends_authenticated_request(self):
        with patch.dict(os.environ, {"MEETPIPE_ALLOW_INSECURE_HTTP": "0"}):
            with self.assertRaisesRegex(RuntimeError, "HTTPS"):
                request_json("http://192.168.104.144:8766", "fixture", "GET", "/health")
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"ok": true}'
        with patch.dict(os.environ, {"MEETPIPE_ALLOW_INSECURE_HTTP": "1"}), \
                patch("meetpipe.coordinator_client.urlopen", return_value=response) as transport:
            self.assertEqual(request_json("http://192.168.104.144:8766", "fixture", "GET", "/health"),
                             {"ok": True})
        request = transport.call_args.args[0]
        self.assertEqual(request.full_url, "http://192.168.104.144:8766/health")
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture")

    def test_lan_opt_in_does_not_allow_public_ips_or_hostname_tricks(self):
        with patch.dict(os.environ, {"MEETPIPE_ALLOW_INSECURE_HTTP": "1"}):
            for url in ("http://8.8.8.8", "http://localhost.attacker.example", "http://remote.example.com"):
                with self.subTest(url=url), self.assertRaisesRegex(RuntimeError, "HTTPS"):
                    request_json(url, "fixture", "GET", "/health")
            with self.assertRaisesRegex(RuntimeError, "HTTP or HTTPS"):
                request_json("file://localhost/tmp/example", "fixture", "GET", "/health")

    def test_worker_accepts_lan_http_with_opt_in(self):
        with patch.dict(os.environ, {
            "MEETPIPE_COORDINATOR_URL": "http://192.168.104.144:8766",
            "MEETPIPE_ALLOW_INSECURE_HTTP": "1",
            "MEETPIPE_WORKER_TOKEN": "fixture-alice", "MEETPIPE_WORKER_ID": "alice",
        }), patch("meetpipe.worker.request_json", side_effect=KeyboardInterrupt()) as http:
            worker.serve("/fixture/checkout", "codex", poll_wait=10)
        self.assertEqual(http.call_args.args[0], "http://192.168.104.144:8766")


if __name__ == "__main__":
    unittest.main()
