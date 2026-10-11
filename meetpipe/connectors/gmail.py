"""Meeting transcripts / AI notes that arrive by email (Meet, Gemini, Zoom, Otter, Fireflies...)."""

from __future__ import annotations

import base64
import re
from email.utils import parsedate_to_datetime

from ..config import settings
from ..google_auth import export_doc_text, service
from ..models import ContextItem, RunScope, parse_ts
from ._agentlog import clip
from .transcript_text import html_to_text, to_text

MAX_MESSAGES, MAX_DOCS_PER_MAIL = 40, 3
DOC_LINK = re.compile(r"https://docs\.google\.com/document/d/([A-Za-z0-9_\-]{20,})")
TEXT_ATTACHMENTS = (".txt", ".vtt", ".srt", ".md")


def build_query(scope: RunScope) -> str:
    who = " OR ".join(f"from:{s}" for s in settings.transcript_senders)
    subjects = 'subject:transcript OR subject:"meeting notes" OR subject:"notes by gemini" OR subject:recap'
    return f"after:{scope.since:%Y/%m/%d} ({who} OR {subjects})"


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="replace")


def _walk(part: dict):
    yield part
    for p in part.get("parts", []) or []:
        yield from _walk(p)


def fetch(scope: RunScope) -> list[ContextItem]:
    gmail, drive = service("gmail", "v1"), service("drive", "v3")
    query = build_query(scope)
    refs = gmail.users().messages().list(userId="me", q=query, maxResults=MAX_MESSAGES).execute().get("messages", [])
    items = []
    for ref in refs:
        msg = gmail.users().messages().get(userId="me", id=ref["id"], format="full").execute()
        headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
        plain, htmls, attachments = [], [], []
        for part in _walk(msg["payload"]):
            mime, body, fname = part.get("mimeType", ""), part.get("body", {}), part.get("filename") or ""
            if fname.lower().endswith(TEXT_ATTACHMENTS) and body.get("attachmentId"):
                att = gmail.users().messages().attachments().get(
                    userId="me", messageId=msg["id"], id=body["attachmentId"]).execute()
                attachments.append((fname, to_text(fname, _decode(att["data"]))))
            elif mime == "text/plain" and body.get("data") and not fname:
                plain.append(_decode(body["data"]))
            elif mime == "text/html" and body.get("data") and not fname:
                htmls.append(_decode(body["data"]))
        raw_html = "\n".join(htmls)
        text = "\n".join(plain) or html_to_text(raw_html)

        sections = [f"From: {headers.get('from', '')}\nTo: {headers.get('to', '')}\n\n{clip(text, 8000)}"]
        for fname, content in attachments:
            sections.append(f"--- Attachment {fname} ---\n{content}")
        doc_ids = list(dict.fromkeys(DOC_LINK.findall(text + raw_html)))[:MAX_DOCS_PER_MAIL]
        for doc_id in doc_ids:
            try:
                sections.append(f"--- Linked Google Doc {doc_id} ---\n{export_doc_text(drive, doc_id)}")
            except Exception as e:  # doc not shared with us, deleted, etc.
                sections.append(f"--- Linked Google Doc {doc_id}: could not fetch ({type(e).__name__}) ---")

        try:
            sent = parsedate_to_datetime(headers["date"])
        except Exception:
            sent = parse_ts(int(msg["internalDate"]))
        items.append(ContextItem(
            id=f"GM-{msg['id'][-6:]}", source="gmail", kind="meeting",
            title=headers.get("subject", "(no subject)"), body="\n\n".join(sections), created_at=sent,
            url=f"https://mail.google.com/mail/u/0/#all/{msg['id']}", author=headers.get("from"),
            meta={"drive_ids": doc_ids, "query": query},
        ))
    return items
