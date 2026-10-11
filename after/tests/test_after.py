"""Offline tests for the After stage.  Run: .venv/bin/python -m pytest after/tests -q

Every test runs against a temp DB/output dir with no API keys and DRY_RUN on, and Darwin is stubbed,
so nothing touches the network.
"""
import pytest

from after import analyze, config, db, executors, pipeline, transcripts

SEED_VTT = config.SEED_DIR / "transcripts" / "before_kickoff_sample.vtt"


def fake_darwin_search(q, max_results=3):
    return {"query": q, "status": "completed", "error": None, "searchId": "srch_1", "searchToken": "tok_secret",
            "agents": [{"name": f"agent for {q}", "agentId": "ag_ready", "capabilityId": "cap_1", "description": "",
                        "readiness": "ready", "canStartThread": True, "connectionPrompt": "prompt", "why": ""},
                       {"name": "stub", "agentId": "ag_blocked", "capabilityId": "cap_2", "description": "",
                        "readiness": "unavailable", "canStartThread": False, "connectionPrompt": "prompt", "why": ""}]}


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "after.db")
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "GOOGLE_TOKEN_FILE", tmp_path / "no_google_token.json")
    for key in ("ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "GITHUB_TOKEN", "RECALL_API_KEY", "SLACK_ONLY_TO"):
        monkeypatch.setattr(config, key, "")
    monkeypatch.setattr(config, "DRY_RUN", True)
    monkeypatch.setattr(config, "DARWIN_API_KEY", "")
    monkeypatch.setattr(executors, "darwin_search", fake_darwin_search)
    db.init()


@pytest.fixture
def demo_meeting():
    return pipeline.process(transcripts.demo())


# ---------------------------------------------------------------- transcripts

def test_text_transcript_speakers_and_continuations():
    t = transcripts.from_text("Sam: hello\nstill Sam\n[00:12] Alex: hi there", "m1", day="2026-10-11")
    assert [(l.speaker, l.text) for l in t.lines] == [("Sam", "hello still Sam"), ("Alex", "hi there")]
    assert t.attendees == ["Alex", "Sam"]
    assert t.date == "2026-10-11"


def test_vtt_transcript_from_before_stage():
    t = transcripts.from_text(SEED_VTT.read_text(), "kickoff", day="2026-10-10")
    assert set(t.attendees) == {"George", "Michael", "Taufiq"}
    assert t.lines[0].speaker == "Taufiq" and t.lines[0].t == 1
    assert all("-->" not in l.text and "<v" not in l.text for l in t.lines)


def test_json_transcript_roundtrip():
    demo = transcripts.demo()
    assert transcripts.from_json(demo.model_dump_json()) == demo


def test_non_demo_transcript_needs_api_key():
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        analyze.analyze(transcripts.from_text("Sam: hi", "m1"))


# ---------------------------------------------------------------- task rules

def test_missing_due_dates_become_followup_tasks():
    tasks = [
        {"title": "A", "owner": "george", "project_role": "After", "due_date": None, "status": "todo"},
        {"title": "B", "owner": "george", "project_role": "After", "due_date": "2026-10-14", "status": "todo"},
        {"title": "C", "owner": None, "project_role": None, "due_date": None, "status": "todo"},
        {"title": "D", "owner": "michael", "project_role": "Slack", "due_date": None, "status": "done"},
    ]
    followups = {f["owner"]: f["title"] for f in pipeline._due_date_followups(tasks)}
    assert followups == {"george": "Set due dates for: A", None: "Assign an owner and due date for: C"}


def test_flags():
    assert pipeline._flags({"owner": None, "due_date": None}) == ["unassigned", "no_due_date"]
    assert pipeline._flags({"owner": "george", "due_date": "2026-10-14"}) == []
    assert pipeline._flags({"owner": "george", "due_date": None, "generated": True}) == []


def test_darwin_query_fallbacks_get_shorter():
    assert executors._darwin_queries("order snacks for delivery") == ["order snacks for delivery", "order snacks"]
    assert executors._darwin_queries("generate a one-page visual meeting report") == [
        "generate a one-page visual meeting report", "generate one-page visual", "generate one-page"]
    assert executors._darwin_queries("order pizza") == ["order pizza"]


# ---------------------------------------------------------------- pipeline

def test_demo_pipeline_end_to_end(demo_meeting):
    m = demo_meeting
    assert m["analysis"]["engine"] == "canned-demo"
    assert len(m["analysis"]["goal_progress"]) == 4  # one per previous-meeting goal

    by_title = {t["title"]: t for t in m["human_tasks"]}
    video = by_title["Record the demo video for submission"]
    assert video["owner"] is None and set(video["flags"]) == {"unassigned", "no_due_date"}
    assert any(t["title"].startswith("Assign an owner and due date") for t in m["human_tasks"])

    # Engineering tasks go to GitHub, the rest to Slack; DRY_RUN means nothing was sent.
    assert {t["channel"] for t in m["human_tasks"]} == {"github", "slack"}
    assert m["outbox"] and not any(o["sent"] for o in m["outbox"])
    assert any(o["channel"] == "github" for o in m["outbox"])

    assert [t["approval"] for t in m["agent_tasks"]] == ["pending"] * 3
    assert all(t["darwin"]["agents"] for t in m["agent_tasks"])


def test_reprocessing_replaces_previous_output(demo_meeting):
    again = pipeline.process(transcripts.demo())
    assert len(again["human_tasks"]) == len(demo_meeting["human_tasks"])
    assert len(again["agent_tasks"]) == 3


def test_next_meeting_compares_against_this_meetings_goals(demo_meeting):
    goals = db.previous_goals("2026-10-18")
    assert [g["goal"] for g in goals] == demo_meeting["analysis"]["next_meeting_goals"]


def test_approve_schedule_writes_ics(demo_meeting):
    task = next(t for t in demo_meeting["agent_tasks"] if t["kind"] == "schedule_meeting")
    m = pipeline.decide(task["id"], approve=True)
    result = next(t for t in m["agent_tasks"] if t["id"] == task["id"])["result"]
    ics = (config.OUTPUT_DIR / "invites" / "sync-2026-10-11-next.ics").read_text()
    assert result["start"] == "2026-10-15T15:00:00" and result["end"] == "2026-10-15T15:30:00"
    assert "DTSTART;TZID=America/Los_Angeles:20261015T150000" in ics
    assert result["calendar"]["sent"] is False  # no Google token in tests


def test_approve_report_builds_html(demo_meeting):
    task = next(t for t in demo_meeting["agent_tasks"] if t["kind"] == "meeting_report")
    m = pipeline.decide(task["id"], approve=True)
    url = next(t for t in m["agent_tasks"] if t["id"] == task["id"])["result"]["report"]
    html = (config.OUTPUT_DIR / "reports" / url.rsplit("/", 1)[-1]).read_text()
    assert "MeetingPipelines weekly sync" in html and "Record the demo video" in html
    assert any("report attached" in o["body"].lower() for o in m["outbox"])


def test_other_task_is_routed_to_human_not_executed(demo_meeting):
    task = next(t for t in demo_meeting["agent_tasks"] if t["kind"] == "other")
    m = pipeline.decide(task["id"], approve=True)
    result = next(t for t in m["agent_tasks"] if t["id"] == task["id"])["result"]
    assert "no auto-execution" in result["detail"]


def test_reject_and_decisions_are_final(demo_meeting):
    task = demo_meeting["agent_tasks"][0]
    pipeline.decide(task["id"], approve=False)
    m = pipeline.decide(task["id"], approve=True)  # second decision is ignored
    row = next(t for t in m["agent_tasks"] if t["id"] == task["id"])
    assert row["approval"] == "rejected" and row["result"] is None


# ---------------------------------------------------------------- executors

def test_slack_only_to_redirects_everything(monkeypatch):
    monkeypatch.setattr(config, "SLACK_ONLY_TO", "georgezhou1010")
    executors.slack_message("m1", "your tasks", "michael")
    executors.slack_message("m1", "channel recap")
    rows = db.rows("SELECT target, body FROM outbox WHERE meeting_id='m1'")
    assert {r["target"] for r in rows} == {"georgezhou1010"}
    assert "would go to pham_michael" in rows[0]["body"]
    assert "would go to test-bot-features" in rows[1]["body"]


def test_github_issue_body_dry_run():
    task = {"title": "Ship it", "owner": "george", "project_role": "After", "due_date": "2026-10-14",
            "status": "todo", "source_quote": "I'll ship it"}
    assert executors.github_issue("m1", task) == {"sent": False, "detail": "dry-run"}
    body = db.rows("SELECT body FROM outbox WHERE meeting_id='m1'")[0]["body"]
    assert "George Zhou" in body and "2026-10-14" in body and "> I'll ship it" in body


# ---------------------------------------------------------------- API

def test_api(monkeypatch):
    from fastapi.testclient import TestClient

    from after.app import app

    client = TestClient(app)
    assert client.get("/api/status").status_code == 200
    m = client.post("/api/process/demo").json()
    assert client.get(f"/api/meetings/{m['id']}").json()["id"] == m["id"]
    assert client.get("/api/meetings/nope").status_code == 404
    assert client.post(f"/api/agent-tasks/{m['agent_tasks'][0]['id']}/maybe").status_code == 400
    assert client.post("/api/process/text", json={"text": "Sam: hi", "meeting_id": "x"}).status_code == 400
    names = [x["name"] for x in client.get("/api/samples").json()]
    assert "feature_test.txt" in names and "before_kickoff_sample.vtt" in names
    page = client.get("/")
    assert page.status_code == 200 and "Meeting Agent" in page.text


# ---------------------------------------------------------------- Darwin Act

class FakeResponse:
    def __init__(self, status, data):
        self.status_code, self._data, self.content = status, data, b"x"

    def json(self):
        return self._data


def _other_task(meeting):
    return next(t for t in meeting["agent_tasks"] if t["kind"] == "other")


def test_search_token_never_reaches_the_browser(demo_meeting):
    assert all("searchToken" not in t["darwin"] for t in demo_meeting["agent_tasks"])
    assert db.rows("SELECT darwin_json FROM agent_tasks")[0]["darwin_json"].count("tok_secret") == 1


def test_act_needs_a_key(demo_meeting):
    with pytest.raises(RuntimeError, match="DARWIN_API_KEY"):
        pipeline.run_via_darwin(_other_task(demo_meeting)["id"], "ag_ready")


def test_act_refuses_unstartable_agent(demo_meeting, monkeypatch):
    monkeypatch.setattr(config, "DARWIN_API_KEY", "dk_test")
    with pytest.raises(RuntimeError, match="can't start a thread"):
        pipeline.run_via_darwin(_other_task(demo_meeting)["id"], "ag_blocked")
    assert _other_task(pipeline.get_meeting(demo_meeting["id"]))["approval"] == "pending"


def test_act_start_and_refresh(demo_meeting, monkeypatch):
    monkeypatch.setattr(config, "DARWIN_API_KEY", "dk_test")
    sent = {}

    def fake_post(url, headers, json, timeout):
        sent.update(url=url, headers=headers, body=json)
        return FakeResponse(202, {"actRequestId": "actreq_1", "threads": [{"threadId": "thr_1"}]})

    monkeypatch.setattr(executors.httpx, "post", fake_post)
    monkeypatch.setattr(executors.httpx, "get",
                        lambda url, params, headers, timeout: FakeResponse(200, {"url": url, "params": params}))
    task = _other_task(demo_meeting)
    m = pipeline.run_via_darwin(task["id"], "ag_ready")

    assert sent["url"].endswith("/api/v3/act")
    assert sent["body"]["searchId"] == "srch_1" and sent["body"]["agentIds"] == ["ag_ready"]
    assert "without explicit approval" in sent["body"]["message"]
    assert sent["headers"]["Authorization"] == "Bearer dk_test"
    assert sent["headers"]["X-Search-Token"] == "tok_secret"
    assert sent["headers"]["Idempotency-Key"] == f"{demo_meeting['id']}-{task['id']}-ag_ready"
    row = _other_task(m)
    assert row["approval"] == "approved" and row["act"]["actRequestId"] == "actreq_1"

    latest = _other_task(pipeline.refresh_darwin(task["id"]))["act"]["latest"]
    assert latest["url"].endswith("/act/requests/actreq_1") and latest["params"] == {"includeThreadState": "true"}

    with pytest.raises(RuntimeError, match="already approved"):
        pipeline.run_via_darwin(task["id"], "ag_ready")


def test_act_preview_403_is_surfaced(demo_meeting, monkeypatch):
    monkeypatch.setattr(config, "DARWIN_API_KEY", "dk_test")
    monkeypatch.setattr(executors.httpx, "post",
                        lambda *a, **k: FakeResponse(403, {"code": "ACT_PREVIEW_ACCESS_REQUIRED"}))
    with pytest.raises(RuntimeError, match="ACT_PREVIEW_ACCESS_REQUIRED"):
        pipeline.run_via_darwin(_other_task(demo_meeting)["id"], "ag_ready")


def test_private_contacts_override_seed(tmp_path):
    (tmp_path / "team_contacts.json").write_text('{"george": {"email": "g@example.com"}}')
    db.init()
    assert {u["handle"]: u["email"] for u in db.team()}["george"] == "g@example.com"


def test_test_label_marks_calendar_events(demo_meeting, monkeypatch):
    monkeypatch.setattr(config, "TEST_LABEL", "This is a test, no action needed.")
    task = next(t for t in demo_meeting["agent_tasks"] if t["kind"] == "schedule_meeting")
    pipeline.decide(task["id"], approve=True)
    ics = (config.OUTPUT_DIR / "invites" / "sync-2026-10-11-next.ics").read_text()
    assert "SUMMARY:[TEST] " in ics and "This is a test, no action needed." in ics
