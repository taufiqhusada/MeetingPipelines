"""Google (Gmail + Drive, read-only): one shared team OAuth client, one token per user in ~/.meetpipe."""

from __future__ import annotations

import json
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from .config import settings

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]


# Google may return scopes in a different form/order (or fewer, if a box was unticked); don't let
# oauthlib reject the whole login for that. We check what was actually granted ourselves.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


class GoogleNotConnected(RuntimeError):
    pass


def client_config() -> dict | None:
    """The team's OAuth client: GOOGLE_CLIENT_ID/SECRET in .env, or a downloaded credentials.json."""
    if settings.google_client_id and settings.google_client_secret:
        return {"installed": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }}
    if settings.google_credentials.exists():
        return json.loads(settings.google_credentials.read_text())
    return None


def start_flow(redirect_uri: str) -> tuple[Flow, str]:
    """Begin the consent flow; returns the flow (keep it for the callback) and the URL to send the user to."""
    cfg = client_config()
    if not cfg:
        raise GoogleNotConnected("no Google OAuth app configured (GOOGLE_CLIENT_ID/SECRET in .env)")
    flow = Flow.from_client_config(cfg, SCOPES, redirect_uri=redirect_uri)
    url, _ = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true")
    return flow, url


def finish_flow(flow: Flow, code: str) -> list[str]:
    """Save the token; return the scopes the user did NOT grant (empty list = all good)."""
    token = flow.fetch_token(code=code)
    granted = set((token.get("scope") or "").split()) if isinstance(token.get("scope"), str) else set(token.get("scope") or [])
    save(flow.credentials)
    return [s for s in SCOPES if granted and s not in granted]


def save(creds: Credentials) -> None:
    settings.token_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings.google_token.write_text(creds.to_json())
    settings.google_token.chmod(0o600)


def disconnect() -> None:
    settings.google_token.unlink(missing_ok=True)


def credentials() -> Credentials:
    if not settings.google_token.exists():
        raise GoogleNotConnected("Google not connected; run: uv run run.py connect")
    creds = Credentials.from_authorized_user_file(str(settings.google_token), SCOPES)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            save(creds)
            return creds
        except Exception:
            pass
    raise GoogleNotConnected("Google login expired; run: uv run run.py connect")


def service(name: str, version: str):
    return build(name, version, credentials=credentials(), cache_discovery=False)


def whoami() -> str | None:
    try:
        return service("gmail", "v1").users().getProfile(userId="me").execute()["emailAddress"]
    except Exception:
        return None


def export_doc_text(drive, file_id: str, mime: str | None = None, limit: int = 40000) -> str:
    """Plain text of a Drive file (Google Docs/Slides export, or raw text files)."""
    if mime is None:
        mime = drive.files().get(fileId=file_id, fields="mimeType", supportsAllDrives=True).execute()["mimeType"]
    if mime in ("application/vnd.google-apps.document", "application/vnd.google-apps.presentation"):
        data = drive.files().export(fileId=file_id, mimeType="text/plain").execute()
    elif mime == "application/vnd.google-apps.spreadsheet":
        data = drive.files().export(fileId=file_id, mimeType="text/csv").execute()
    else:
        data = drive.files().get_media(fileId=file_id, supportsAllDrives=True).execute()
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
    return text[:limit]
