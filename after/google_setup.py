"""One-time Google Calendar OAuth.

1. Google Cloud Console -> create project -> enable "Google Calendar API".
2. OAuth consent screen -> External -> add your Gmail as a test user.
3. Credentials -> Create OAuth client ID -> Desktop app -> download JSON
   to after/data/google_credentials.json
4. Run:  python -m after.google_setup   (opens a browser, saves after/data/google_token.json)
"""
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from . import config

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def _credentials() -> Credentials:
    creds = None
    if config.GOOGLE_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(config.GOOGLE_TOKEN_FILE), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        if not config.GOOGLE_CREDENTIALS_FILE.exists():
            raise SystemExit(f"Missing {config.GOOGLE_CREDENTIALS_FILE}. See the steps at the top of this file.")
        flow = InstalledAppFlow.from_client_secrets_file(str(config.GOOGLE_CREDENTIALS_FILE), SCOPES)
        creds = flow.run_local_server(port=0)
    config.GOOGLE_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.GOOGLE_TOKEN_FILE.write_text(creds.to_json())
    return creds


def calendar_service():
    return build("calendar", "v3", credentials=_credentials(), cache_discovery=False)


if __name__ == "__main__":
    calendar_service()
    print(f"Google Calendar connected. Token saved to {config.GOOGLE_TOKEN_FILE}")
