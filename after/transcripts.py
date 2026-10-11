"""Transcript sources: pasted text, JSON upload, or a Recall.ai meeting bot."""
import json
import re
from datetime import date

import httpx

from . import config
from .schemas import Transcript, TranscriptLine

_LINE = re.compile(r"^\s*(?:\[?[\d:.]+\]?\s*)?([^:]{1,40}):\s*(.+)$")


def from_text(text: str, meeting_id: str, title: str = "Team sync", day: str | None = None) -> Transcript:
    """Parse "Speaker: words" lines. Lines without a speaker are appended to the previous line."""
    lines: list[TranscriptLine] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        m = _LINE.match(raw)
        if m:
            lines.append(TranscriptLine(speaker=m.group(1).strip(), text=m.group(2).strip()))
        elif lines:
            lines[-1].text += " " + raw.strip()
        else:
            lines.append(TranscriptLine(speaker="Unknown", text=raw.strip()))
    return Transcript(
        meeting_id=meeting_id,
        title=title,
        date=day or date.today().isoformat(),
        attendees=sorted({l.speaker for l in lines}),
        lines=lines,
    )


def from_json(payload: str | dict) -> Transcript:
    data = json.loads(payload) if isinstance(payload, str) else payload
    return Transcript.model_validate(data)


def demo() -> Transcript:
    return from_json((config.SEED_DIR / "transcripts" / "demo_sync.json").read_text())


# ---------------------------------------------------------------- Recall.ai
# Docs: https://docs.recall.ai/docs/quickstart  (bot joins Zoom / Meet / Teams, meeting captions are free)

def _recall() -> httpx.Client:
    if not config.RECALL_API_KEY:
        raise RuntimeError("RECALL_API_KEY is not set")
    return httpx.Client(
        base_url=f"https://{config.RECALL_REGION}.recall.ai/api/v1",
        headers={"Authorization": f"Token {config.RECALL_API_KEY}"},
        timeout=30,
    )


def recall_create_bot(meeting_url: str, bot_name: str = "Meeting Agent") -> dict:
    with _recall() as c:
        r = c.post(
            "/bot/",
            json={
                "meeting_url": meeting_url,
                "bot_name": bot_name,
                "recording_config": {"transcript": {"provider": {"meeting_captions": {}}}},
            },
        )
        r.raise_for_status()
        return r.json()


def recall_bot_status(bot_id: str) -> dict:
    """Returns {status, transcript_url}; transcript_url is set once the recording is processed."""
    with _recall() as c:
        r = c.get(f"/bot/{bot_id}/")
        r.raise_for_status()
        bot = r.json()
    changes = bot.get("status_changes") or []
    status = changes[-1]["code"] if changes else "unknown"
    url = None
    for rec in bot.get("recordings") or []:
        url = ((((rec.get("media_shortcuts") or {}).get("transcript") or {}).get("data")) or {}).get("download_url")
        if url:
            break
    return {"bot_id": bot_id, "status": status, "transcript_url": url, "meeting_url": (bot.get("meeting_url") or {})}


def recall_transcript(bot_id: str, title: str = "Team sync") -> Transcript:
    info = recall_bot_status(bot_id)
    if not info["transcript_url"]:
        raise RuntimeError(f"Transcript not ready (bot status: {info['status']})")
    segments = httpx.get(info["transcript_url"], timeout=60).json()
    # Download schema: [{participant: {name}, words: [{text, start_timestamp: {relative}}]}]
    lines = [
        TranscriptLine(
            speaker=(seg.get("participant") or {}).get("name") or "Unknown",
            text=" ".join(w["text"] for w in seg.get("words", [])),
            t=(seg["words"][0].get("start_timestamp") or {}).get("relative") if seg.get("words") else None,
        )
        for seg in segments
        if seg.get("words")
    ]
    return Transcript(
        meeting_id=f"recall-{bot_id[:8]}",
        title=title,
        date=date.today().isoformat(),
        attendees=sorted({l.speaker for l in lines}),
        lines=lines,
        source="recall",
    )
