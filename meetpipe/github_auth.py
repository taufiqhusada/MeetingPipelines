"""GitHub login through the team's GitHub App (read-only), using the standard browser redirect flow.

The app itself is created once with `run.py setup-github` (GitHub "app manifest" flow), which writes
GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET / GITHUB_APP_SLUG into .env for the team to share.
"""

from __future__ import annotations

import json
import secrets
import shutil
import subprocess
import time
from urllib.parse import urlencode

import requests

from .config import settings

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
API = "https://api.github.com"


class GitHubNotConfigured(RuntimeError):
    pass


# ---------- one-time team setup: create the GitHub App from a manifest ----------

def app_manifest(port: int, name: str) -> dict:
    base = f"http://127.0.0.1:{port}"
    return {
        "name": name,
        "url": "https://github.com/taufiqhusada/MeetingPipelines",
        "description": "Gathers PRs, issues and commits (read-only) for pre-meeting briefs.",
        "public": True,  # lets teammates in other accounts/orgs install it on their repos
        "redirect_url": f"{base}/github/app-created",
        "callback_urls": [f"{base}/github/callback"],
        "request_oauth_on_install": False,
        "hook_attributes": {"url": "https://example.com/unused", "active": False},
        "default_permissions": {"contents": "read", "issues": "read", "pull_requests": "read", "metadata": "read"},
        "default_events": [],
    }


def convert_manifest(code: str) -> dict:
    """Exchange the manifest code for the new app's credentials."""
    r = requests.post(f"{API}/app-manifests/{code}/conversions", timeout=20,
                      headers={"Accept": "application/vnd.github+json"})
    r.raise_for_status()
    return r.json()


# ---------- per-user login ----------

def configured() -> bool:
    return bool(settings.github_client_id and settings.github_client_secret)


def authorize_url(redirect_uri: str) -> tuple[str, str]:
    if not configured():
        raise GitHubNotConfigured("team GitHub App not set up (run: uv run run.py setup-github)")
    state = secrets.token_urlsafe(16)
    q = urlencode({"client_id": settings.github_client_id, "redirect_uri": redirect_uri, "state": state})
    return f"{AUTHORIZE_URL}?{q}", state


def _token_request(**data) -> dict:
    r = requests.post(TOKEN_URL, timeout=20, headers={"Accept": "application/json"}, data={
        "client_id": settings.github_client_id, "client_secret": settings.github_client_secret, **data})
    out = r.json()
    if "access_token" not in out:
        raise RuntimeError(f"GitHub: {out.get('error')}: {out.get('error_description', '')}")
    return out


def finish_login(code: str, redirect_uri: str) -> str | None:
    save(_token_request(code=code, redirect_uri=redirect_uri))
    return whoami()


def save(token_response: dict) -> None:
    now = time.time()
    token_response["obtained_at"] = now
    if "expires_in" in token_response:
        token_response["expires_at"] = now + int(token_response["expires_in"]) - 60
    settings.token_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings.github_token_file.write_text(json.dumps(token_response))
    settings.github_token_file.chmod(0o600)


def disconnect() -> None:
    settings.github_token_file.unlink(missing_ok=True)


def _stored_token() -> str | None:
    if not settings.github_token_file.exists():
        return None
    data = json.loads(settings.github_token_file.read_text())
    if data.get("expires_at") and time.time() > data["expires_at"]:  # GitHub App user tokens last 8h
        if not (data.get("refresh_token") and configured()):
            return None
        try:
            save(_token_request(grant_type="refresh_token", refresh_token=data["refresh_token"]))
            data = json.loads(settings.github_token_file.read_text())
        except Exception:
            return None
    return data.get("access_token")


def token() -> str | None:
    """Precedence: GITHUB_TOKEN in .env → login from `run.py connect` → `gh auth token`."""
    if settings.github_token:
        return settings.github_token
    if tok := _stored_token():
        return tok
    if shutil.which("gh"):
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    return None


def _get(path: str, tok: str) -> requests.Response:
    return requests.get(API + path, timeout=15,
                        headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})


def whoami(tok: str | None = None) -> str | None:
    tok = tok or token()
    if not tok:
        return None
    r = _get("/user", tok)
    return r.json().get("login") if r.ok else None


def installations() -> int | None:
    """How many accounts/orgs this user can see the app installed on (None if not an app login)."""
    tok = _stored_token()
    if not tok:
        return None
    r = _get("/user/installations", tok)
    return r.json().get("total_count") if r.ok else None


def install_url() -> str | None:
    """Where users (or org admins, once per org) pick which repos the app may read."""
    return f"https://github.com/apps/{settings.github_app_slug}/installations/new" if settings.github_app_slug else None
