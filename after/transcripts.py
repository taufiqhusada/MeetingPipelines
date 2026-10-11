"""Transcript sources: pasted text, JSON upload, or a Recall.ai meeting bot."""
import json
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

from . import config
from .schemas import Transcript, TranscriptLine

_LINE = re.compile(r"^\s*(?:\[?[\d:.]+\]?\s*)?([^:]{1,40}):\s*(.+)$")
_VTT_TIME = re.compile(r"^(\d+):(\d\d):(\d\d)[.,](\d+)\s*-->")
_VTT_VOICE = re.compile(r"^<v\s+([^>]+)>(.*?)(?:</v>)?$")


def _parse_vtt(text: str) -> list[TranscriptLine]:
    """WebVTT / SRT cues. Speakers come from <v Name> tags or a "Name: text" prefix."""
    lines, t = [], None
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw or raw == "WEBVTT" or raw.startswith("NOTE") or raw.isdigit():
            continue
        if m := _VTT_TIME.match(raw):
            h, mnt, sec, _ = m.groups()
            t = int(h) * 3600 + int(mnt) * 60 + int(sec)
            continue
        if m := _VTT_VOICE.match(raw):
            speaker, words = m.group(1).strip(), m.group(2).strip()
        elif m := _LINE.match(raw):
            speaker, words = m.group(1).strip(), m.group(2).strip()
        else:
            speaker, words = (lines[-1].speaker if lines else "Unknown"), raw
        if lines and lines[-1].speaker == speaker and lines[-1].t == t:
            lines[-1].text += " " + words
        else:
            lines.append(TranscriptLine(speaker=speaker, text=words, t=t))
    return lines


def from_text(text: str, meeting_id: str, title: str = "Team sync", day: str | None = None) -> Transcript:
    """Parse "Speaker: words" lines (or a WebVTT/SRT file). Lines without a speaker join the previous line."""
    lines: list[TranscriptLine] = []
    if text.lstrip().startswith("WEBVTT") or "-->" in text:
        lines = _parse_vtt(text)
    for raw in ([] if lines else text.splitlines()):
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


# ---------------------------------------------------------------- TranscripTonic
# Free, open-source Chrome extension that saves Google Meet transcripts from Meet's own captions and POSTs them
# to a webhook when the meeting ends. Body: {meetingTitle, meetingStartTimestamp, transcript, chatMessages}; with
# the "advanced" body, transcript is [{personName, timestamp, transcriptText}].

_SIMPLE_BLOCK = re.compile(r"^(.+?) \(([^()]*)\)$")


def _simple_blocks(text: str) -> list[tuple[str, str]]:
    """Simple body: "Name (TIME)\ntext\n\n" blocks."""
    out = []
    for block in re.split(r"\n\s*\n", text or ""):
        head, _, body = block.strip().partition("\n")
        if m := _SIMPLE_BLOCK.match(head.strip()):
            out.append((m.group(1).strip(), body.strip()))
    return out


def from_transcriptonic(body: dict) -> Transcript:
    title = (body.get("meetingTitle") or "Google Meet").strip()
    start = body.get("meetingStartTimestamp") or ""
    try:
        started = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(ZoneInfo(config.TIMEZONE))
    except ValueError:  # simple body uses a locale-formatted timestamp
        started = datetime.now(ZoneInfo(config.TIMEZONE))

    def seconds(ts: str | None) -> float | None:
        try:
            return (datetime.fromisoformat(ts.replace("Z", "+00:00")) - started).total_seconds()
        except (AttributeError, ValueError, TypeError):
            return None

    lines: list[TranscriptLine] = []
    transcript, chat = body.get("transcript") or [], body.get("chatMessages") or []
    if isinstance(transcript, list):
        lines += [TranscriptLine(speaker=b.get("personName") or "Unknown", text=b.get("transcriptText", ""),
                                 t=seconds(b.get("timestamp"))) for b in transcript if b.get("transcriptText")]
        lines += [TranscriptLine(speaker=f"{c.get('personName') or 'Unknown'} (chat)", text=c.get("chatMessageText", ""),
                                 t=seconds(c.get("timestamp"))) for c in chat if c.get("chatMessageText")]
        lines.sort(key=lambda l: l.t if l.t is not None else 0)
    else:
        lines += [TranscriptLine(speaker=n, text=t) for n, t in _simple_blocks(transcript) if t]
        lines += [TranscriptLine(speaker=f"{n} (chat)", text=t) for n, t in _simple_blocks(chat) if t]
    if not lines:
        raise RuntimeError("TranscripTonic sent an empty transcript (were Google Meet captions on?)")
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "meet"
    return Transcript(
        meeting_id=f"{slug}-{started.strftime('%Y%m%d-%H%M')}",
        title=title,
        date=started.date().isoformat(),
        attendees=sorted({l.speaker.removesuffix(" (chat)") for l in lines}),
        lines=lines,
        source="transcriptonic",
    )


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
