"""Seed REAL test data into your own accounts so every connector has something to read.

Creates (idempotent: re-running skips what already exists):
  Google Drive  folder "MeetingPipelines (test data)" with
                  - "MeetingPipelines — Design Spec v1"            (Google Doc)
                  - "Meet Recordings/MeetingPipelines kickoff — Transcript" (Google Doc)
  Gmail         2 messages INSERTED into your own inbox (nothing is sent to anyone):
                  - "Meeting notes: MeetingPipelines kickoff (Oct 6)"  → links the transcript Doc
                  - "Transcript: MeetingPipelines design review (Oct 9)" → .vtt attachment
  GitHub        5 issues on the repo, labelled "meetpipe-test"

Google: asks for a one-time consent with WRITE scopes (drive.file, gmail.insert). That token is used
only here and is not saved. GitHub: uses GITHUB_SEED_TOKEN, else GITHUB_TOKEN (also the commented
"# GITHUB_TOKEN=" line in .env). Needs Issues: read & write on the repo.

  uv run scripts/seed_test_data.py --dry-run
  uv run scripts/seed_test_data.py                 # both
  uv run scripts/seed_test_data.py --only google   # or --only github
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import sys
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timezone
from pathlib import Path

import requests
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from meetpipe.google_auth import client_config  # noqa: E402

DATA = Path(__file__).parent / "seed_data"
REPO = "taufiqhusada/MeetingPipelines"
FOLDER_NAME = "MeetingPipelines (test data)"
SEED_SCOPES = ["https://www.googleapis.com/auth/drive.file", "https://www.googleapis.com/auth/gmail.insert",
               "https://www.googleapis.com/auth/gmail.readonly"]
GDOC = "application/vnd.google-apps.document"
FOLDER = "application/vnd.google-apps.folder"

ISSUES = [
    {"title": "Slack wrapper: post brief.md and team brief to a channel",
     "body": "Spec R9. Post each person's `brief.md` and the aggregated team brief into the team channel.\n\n"
             "Owner: Michael (committed at kickoff, Oct 6, target Mon Oct 13).\n"
             "Status from design review (Oct 9): not started, reading Slack Bolt docs."},
    {"title": "After-meeting pipeline spec: transcript → GitHub issues",
     "body": "Spec R11. Write the spec for turning a new meeting transcript into tracked action items.\n\n"
             "Owner: George. Originally due Wed Oct 8 (kickoff), moved to Sat Oct 11 at the design review."},
    {"title": "Aggregator inbox: read briefs from a shared folder",
     "body": "Decision from design review (Oct 9): start with a shared folder for aggregation; move to a Slack "
             "channel once the Slack bot can read messages.\n\nNeeds: `publish` command, author identity on "
             "brief.json, `aggregate --from <folder>`."},
    {"title": "Codex summarizer returns 401 Unauthorized on macOS",
     "body": "`run.py doctor --ping` → `llm:codex` fails with `401 Unauthorized: Missing bearer` from "
             "api.openai.com. Claude backend works. Workaround: `--llm claude`. Probably needs `codex login`.",
     "assignees": ["taufiqhusada"]},
    {"title": "Support multiple Google accounts per person",
     "body": "Spec R12 (v2). Some of us keep meeting notes in one Gmail and specs in another Drive. "
             "Allow connecting more than one Google account."},
]


# ------------------------------- Google -------------------------------

def google_services():
    cfg = client_config()
    if not cfg:
        sys.exit("GOOGLE_CLIENT_ID/SECRET missing in .env")
    print("Opening browser for a ONE-TIME write consent (create Drive files, add mail to your inbox)…")
    creds = InstalledAppFlow.from_client_config(cfg, SEED_SCOPES).run_local_server(
        port=0, prompt="consent", authorization_prompt_message="")
    return build("drive", "v3", credentials=creds), build("gmail", "v1", credentials=creds)


def _find(drive, name: str, mime: str, parent: str | None = None) -> str | None:
    q = f"name = '{name}' and mimeType = '{mime}' and trashed = false"
    if parent:
        q += f" and '{parent}' in parents"
    files = drive.files().list(q=q, fields="files(id)", spaces="drive").execute().get("files", [])
    return files[0]["id"] if files else None


def _folder(drive, name: str, parent: str | None = None) -> str:
    if fid := _find(drive, name, FOLDER, parent):
        return fid
    body = {"name": name, "mimeType": FOLDER, **({"parents": [parent]} if parent else {})}
    return drive.files().create(body=body, fields="id").execute()["id"]


def _doc(drive, name: str, text: str, parent: str) -> tuple[str, bool]:
    if fid := _find(drive, name, GDOC, parent):
        return fid, False
    media = MediaInMemoryUpload(text.encode(), mimetype="text/plain")
    f = drive.files().create(body={"name": name, "mimeType": GDOC, "parents": [parent]},
                             media_body=media, fields="id").execute()
    return f["id"], True


def _insert_mail(gmail, me: str, subject: str, body: str, date: datetime,
                 attachment: tuple[str, str] | None = None) -> bool:
    if gmail.users().messages().list(userId="me", q=f'subject:"{subject}"').execute().get("messages"):
        return False
    msg = EmailMessage()
    msg["From"] = f"MeetingPipelines notes <{me}>"
    msg["To"] = me
    msg["Subject"] = subject
    msg["Date"] = format_datetime(date)
    msg.set_content(body)
    if attachment:
        name, content = attachment
        msg.add_attachment(content.encode(), maintype="text", subtype="vtt", filename=name)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail.users().messages().insert(userId="me", internalDateSource="dateHeader",
                                    body={"raw": raw, "labelIds": ["INBOX", "UNREAD"]}).execute()
    return True


def seed_google(dry: bool) -> None:
    spec = (DATA / "design_spec.md").read_text()
    kickoff = (DATA / "transcript_kickoff.txt").read_text()
    review_vtt = (DATA / "transcript_design_review.vtt").read_text()
    if dry:
        print(f"[dry-run] Drive: folder '{FOLDER_NAME}' + 'MeetingPipelines — Design Spec v1' "
              f"+ 'Meet Recordings/MeetingPipelines kickoff — Transcript'")
        print("[dry-run] Gmail: insert 'Meeting notes: MeetingPipelines kickoff (Oct 6)' and "
              "'Transcript: MeetingPipelines design review (Oct 9)' (+ .vtt)")
        return
    drive, gmail = google_services()
    me = gmail.users().getProfile(userId="me").execute()["emailAddress"]

    root = _folder(drive, FOLDER_NAME)
    recordings = _folder(drive, "Meet Recordings", root)
    spec_id, new = _doc(drive, "MeetingPipelines — Design Spec v1", spec, root)
    print(f"{'created' if new else 'exists '}  Drive doc: Design Spec v1  https://docs.google.com/document/d/{spec_id}")
    tr_id, new = _doc(drive, "MeetingPipelines kickoff — Transcript", kickoff, recordings)
    tr_url = f"https://docs.google.com/document/d/{tr_id}/edit"
    print(f"{'created' if new else 'exists '}  Drive doc: kickoff transcript  {tr_url}")

    notes = f"""Meeting notes — MeetingPipelines kickoff
Oct 6, 2026 · Taufiq, Michael, George

Summary
The team agreed to build the pre-meeting context-gathering backend first (no Slack yet): coding-agent
logs, GitHub, Drive docs and meeting transcripts, summarized in one LLM call with no database.

Next steps
- Taufiq: build connectors for Claude Code, Codex, GitHub, Gmail and Drive by Fri Oct 10.
- Michael: Slack wrapper first version by Mon Oct 13.
- George: after-meeting pipeline spec in Drive by Wed Oct 8.
- Michael: open GitHub issues for the remaining pieces.
- Taufiq: investigate a click-to-connect consent flow so nobody pastes tokens.

Open question: where do teammates' briefs meet for aggregation (Slack vs shared folder)?

Full transcript: {tr_url}
"""
    oct6 = datetime(2026, 10, 6, 17, 30, tzinfo=timezone.utc)
    oct9 = datetime(2026, 10, 9, 17, 30, tzinfo=timezone.utc)
    print(("inserted" if _insert_mail(gmail, me, "Meeting notes: MeetingPipelines kickoff (Oct 6)", notes, oct6)
           else "exists  ") + "  Gmail: kickoff meeting notes (links transcript Doc)")
    review_body = ("Transcript attached for the MeetingPipelines design review (Oct 9, 2026).\n"
                   "Attendees: Taufiq, Michael, George.\n")
    print(("inserted" if _insert_mail(gmail, me, "Transcript: MeetingPipelines design review (Oct 9)", review_body,
                                      oct9, ("design-review-2026-10-09.vtt", review_vtt))
           else "exists  ") + "  Gmail: design review transcript (.vtt attachment)")


# ------------------------------- GitHub -------------------------------

def _github_token() -> str | None:
    if tok := os.getenv("GITHUB_SEED_TOKEN") or os.getenv("GITHUB_TOKEN"):
        return tok
    env = ROOT / ".env"
    if env.exists() and (m := re.search(r"^#?\s*GITHUB_TOKEN=(\S+)", env.read_text(), re.M)):
        return m.group(1)
    return None


def seed_github(dry: bool) -> None:
    if dry:
        for i in ISSUES:
            print(f"[dry-run] GitHub issue on {REPO}: {i['title']}")
        return
    tok = _github_token()
    if not tok:
        sys.exit("No GitHub token: set GITHUB_SEED_TOKEN (needs Issues: read & write)")
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
    api = f"https://api.github.com/repos/{REPO}"
    s.post(f"{api}/labels", json={"name": "meetpipe-test", "color": "ededed",
                                  "description": "Test data for Meeting Pipelines"})  # 422 if it exists
    existing = {i["title"] for i in s.get(f"{api}/issues", params={"state": "all", "per_page": 100}).json()
                if isinstance(i, dict)}
    for issue in ISSUES:
        if issue["title"] in existing:
            print(f"exists    GitHub: {issue['title']}")
            continue
        r = s.post(f"{api}/issues", json={**issue, "labels": ["meetpipe-test"]})
        if r.status_code == 403:
            sys.exit(f"GitHub 403: token lacks Issues write on {REPO}. "
                     "Edit the token → Repository permissions → Issues: Read and write.")
        r.raise_for_status()
        print(f"created   GitHub: #{r.json()['number']} {issue['title']}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--only", choices=["google", "github"])
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    if a.only in (None, "google"):
        seed_google(a.dry_run)
    if a.only in (None, "github"):
        seed_github(a.dry_run)
    print("\nNext: uv run run.py run --since 14d --meeting \"MeetingPipelines weekly sync\"")


if __name__ == "__main__":
    main()
