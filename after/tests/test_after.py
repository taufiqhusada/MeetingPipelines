"""Offline tests for the After stage.  Run: .venv/bin/python -m pytest after/tests -q

Every test runs against a temp DB/output dir with no API keys and DRY_RUN on, and Darwin is stubbed,
so nothing touches the network.
"""
import pytest

from after import analyze, config, db, executors, pipeline, transcripts

SEED_VTT = config.SEED_DIR / "transcripts" / "before_kickoff_sample.vtt"


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "after.db")
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(config, "GOOGLE_TOKEN_FILE", tmp_path / "no_google_token.json")
    for key in ("ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "GITHUB_TOKEN", "RECALL_API_KEY", "SLACK_ONLY_TO"):
        monkeypatch.setattr(config, key, "")
    monkeypatch.setattr(config, "DRY_RUN", True)
    monkeypatch.setattr(
        executors, "darwin_search",
        lambda q, max_results=3: {"query": q, "status": "completed", "error": None,
                                  "agents": [{"name": f"agent for {q}", "description": "", "readiness": "unavailable", "why": ""}]},
    )
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
