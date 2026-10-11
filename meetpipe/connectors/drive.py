"""Whole-Drive scan: list recent files (metadata only), let the LLM pick relevant ones, export those."""

from __future__ import annotations

import re

from ..config import settings
from ..google_auth import export_doc_text, service
from ..models import ContextItem, RunScope, parse_ts
from .transcript_text import strip_subtitles

MAX_PICKS = 20
FOLDER = "application/vnd.google-apps.folder"
READABLE = {
    "application/vnd.google-apps.document": "doc",
    "application/vnd.google-apps.presentation": "slides",
    "application/vnd.google-apps.spreadsheet": "sheet",
    "text/plain": "txt",
    "text/markdown": "md",
    "text/vtt": "vtt",
    "application/x-subrip": "srt",
}
_MEETING_WORDS = r"transcript|notes by gemini|meeting notes|recording|standup|sync|retro"
MEETING_NAME = re.compile(_MEETING_WORDS, re.I)
HEURISTIC = re.compile(r"spec|design|prd|rfc|architecture|plan|roadmap|requirement|proposal|" + _MEETING_WORDS, re.I)


def _list_files(drive, scope: RunScope) -> list[dict]:
    q = f"modifiedTime > '{scope.since:%Y-%m-%dT%H:%M:%S}' and trashed = false and mimeType != '{FOLDER}'"
    files, token = [], None
    while len(files) < settings.drive_max_files:
        resp = drive.files().list(
            q=q, orderBy="modifiedTime desc", pageSize=100, pageToken=token,
            includeItemsFromAllDrives=True, supportsAllDrives=True,
            fields="nextPageToken, files(id,name,mimeType,modifiedTime,owners(displayName),parents,webViewLink)",
        ).execute()
        files += resp.get("files", [])
        token = resp.get("nextPageToken")
        if not token:
            break
    files = [f for f in files[: settings.drive_max_files] if f["mimeType"] in READABLE or f["name"].lower().endswith((".txt", ".md", ".vtt", ".srt"))]
    folders: dict[str, str] = {}
    for pid in {p for f in files for p in f.get("parents", [])}:
        try:
            folders[pid] = drive.files().get(fileId=pid, fields="name", supportsAllDrives=True).execute()["name"]
        except Exception:
            folders[pid] = "?"
    for f in files:
        f["folder"] = "/".join(folders.get(p, "?") for p in f.get("parents", [])) or "My Drive"
    return files


def _triage(files: list[dict], scope: RunScope, llm) -> tuple[list[int], str]:
    listing = "\n".join(
        f"{i} | {f['name']} | folder: {f['folder']} | {READABLE.get(f['mimeType'], 'file')} | "
        f"modified {f['modifiedTime'][:10]} | owner {', '.join(o['displayName'] for o in f.get('owners', []))}"
        for i, f in enumerate(files))
    if llm is not None:
        prompt = f"""You are triaging a Google Drive listing to prepare a pre-meeting brief.
Project / repo: {scope.repo or scope.path or 'unknown'}
Meeting: {scope.meeting or 'general project sync'}
Extra keywords: {', '.join(scope.keywords) or '-'}

Pick at most {MAX_PICKS} files likely to matter for this project: specs, design docs, PRDs, RFCs, plans,
meeting transcripts/notes, retros. Skip unrelated personal files, invoices, templates, generic docs.
If nothing looks related, return an empty list.

Files (index | name | folder | type | modified | owner):
{listing}

Return ONLY JSON: {{"selected": [<index>, ...], "why": "<one short sentence>"}}"""
        try:
            out = llm.json(prompt)
            picks = [int(i) for i in out.get("selected", []) if 0 <= int(i) < len(files)]
            return picks[:MAX_PICKS], f"llm: {out.get('why', '')}"
        except Exception as e:
            reason = f"llm triage failed ({e}); used keyword heuristic"
    else:
        reason = "no llm; used keyword heuristic"
    words = [w.lower() for w in scope.keywords + ([scope.meeting] if scope.meeting else [])]
    picks = [i for i, f in enumerate(files)
             if HEURISTIC.search(f["name"]) or any(w in f["name"].lower() for w in words)]
    return picks[:MAX_PICKS], reason


def fetch(scope: RunScope, llm=None, skip_ids: set[str] | None = None) -> list[ContextItem]:
    drive = service("drive", "v3")
    files = [f for f in _list_files(drive, scope) if f["id"] not in (skip_ids or set())]
    if not files:
        return []
    picks, reason = _triage(files, scope, llm)
    items = []
    for i in picks:
        f = files[i]
        try:
            text = export_doc_text(drive, f["id"], f["mimeType"])
        except Exception as e:
            text = f"(could not export: {type(e).__name__})"
        if f["name"].lower().endswith((".vtt", ".srt")):
            text = strip_subtitles(text)
        items.append(ContextItem(
            id=f"DR-{f['id'][:6]}", source="drive",
            kind="meeting" if MEETING_NAME.search(f["name"]) else "doc",
            title=f["name"], body=text, created_at=parse_ts(f["modifiedTime"]), url=f.get("webViewLink"),
            author=", ".join(o["displayName"] for o in f.get("owners", [])),
            meta={"drive_id": f["id"], "folder": f["folder"], "triage": reason,
                  "candidates_listed": len(files)},
        ))
    return items
