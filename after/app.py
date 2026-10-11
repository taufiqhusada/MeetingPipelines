"""FastAPI server for the After stage.  Run: uvicorn after.app:app --reload"""
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, pipeline, transcripts

app = FastAPI(title="Meeting Agent: After")
db.init()
config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/output", StaticFiles(directory=config.OUTPUT_DIR), name="output")


class TextIn(BaseModel):
    text: str
    meeting_id: str
    title: str = "Team sync"
    date: str | None = None


class RecallIn(BaseModel):
    meeting_url: str


def _run(fn, *args):
    try:
        return fn(*args)
    except KeyError as e:
        raise HTTPException(404, f"Unknown meeting {e}")
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@app.get("/")
def index():
    return FileResponse(config.ROOT / "web" / "index.html")


@app.get("/api/status")
def status():
    return {"flags": config.flags(), "model": config.CLAUDE_MODEL, "dry_run": config.DRY_RUN,
            "meetings": db.rows("SELECT id, title, date, source, created_at FROM meetings "
                                "WHERE analysis_json IS NOT NULL ORDER BY created_at DESC LIMIT 15")}


@app.get("/api/samples")
def samples():
    """Fake transcripts for testing features (pasted-text and VTT formats)."""
    folder = config.SEED_DIR / "transcripts"
    return [{"name": f.name, "text": f.read_text()} for f in sorted(folder.glob("*")) if f.suffix in (".txt", ".vtt")]


@app.post("/api/process/demo")
def process_demo():
    return _run(pipeline.process, transcripts.demo())


@app.post("/api/process/text")
def process_text(body: TextIn):
    return _run(pipeline.process, transcripts.from_text(body.text, body.meeting_id, body.title, body.date))


@app.post("/api/process/json")
def process_json(body: dict):
    return _run(pipeline.process, transcripts.from_json(body))


@app.get("/api/meetings/{meeting_id}")
def meeting(meeting_id: str):
    return _run(pipeline.get_meeting, meeting_id)


# Darwin routes must come before /{decision}, which would otherwise swallow "darwin".
class DarwinRunIn(BaseModel):
    agent_id: str


@app.post("/api/agent-tasks/{task_id}/darwin")
def run_via_darwin(task_id: int, body: DarwinRunIn):
    return _run(pipeline.run_via_darwin, task_id, body.agent_id)


@app.post("/api/agent-tasks/{task_id}/darwin/refresh")
def refresh_darwin(task_id: int):
    return _run(pipeline.refresh_darwin, task_id)


@app.post("/api/agent-tasks/{task_id}/{decision}")
def decide(task_id: int, decision: str, approver: str | None = None):
    if decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve or reject")
    if approver is not None and approver not in {u["slack_user_id"] for u in db.team()}:
        raise HTTPException(403, f"{approver or 'This Slack user'} isn't on the team list, so can't approve tasks")
    return _run(pipeline.decide, task_id, decision == "approve", approver)


# TranscripTonic (Chrome extension for Google Meet) posts here when a meeting ends.
# Extension settings -> "Set up webhooks": URL http://localhost:8000/api/webhooks/transcriptonic, body type "Advanced".
@app.post("/api/webhooks/transcriptonic", status_code=202)
def transcriptonic_webhook(body: dict, background: BackgroundTasks):
    transcript = _run(transcripts.from_transcriptonic, body)
    background.add_task(_process_quietly, transcript)  # analysis takes ~40s; answer the extension right away
    return {"accepted": True, "meeting_id": transcript.meeting_id, "lines": len(transcript.lines),
            "view": f"{config.BASE_URL}/?meeting={transcript.meeting_id}"}


def _process_quietly(transcript):
    try:
        pipeline.process(transcript)
    except Exception as e:  # nobody is waiting on this request; log for the server console
        print(f"[transcriptonic] processing {transcript.meeting_id} failed: {e}", flush=True)


# Recall.ai: send a bot to a live meeting, then process its transcript once the call ends.
@app.post("/api/recall/bots")
def recall_bot(body: RecallIn):
    return _run(transcripts.recall_create_bot, body.meeting_url)


@app.get("/api/recall/bots/{bot_id}")
def recall_status(bot_id: str):
    return _run(transcripts.recall_bot_status, bot_id)


@app.post("/api/recall/bots/{bot_id}/process")
def recall_process(bot_id: str):
    return _run(lambda: pipeline.process(transcripts.recall_transcript(bot_id)))
