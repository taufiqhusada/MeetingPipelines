"""Settings loaded from .env (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _path(name: str, default: str) -> Path:
    p = Path(os.path.expanduser(os.getenv(name, default)))
    return p if p.is_absolute() else ROOT / p


def _list(name: str, default: str) -> list[str]:
    return [s.strip() for s in os.getenv(name, default).split(",") if s.strip()]


@dataclass
class Settings:
    # Per-user tokens written by `run.py connect` (never committed, never in .env)
    token_dir: Path = field(default_factory=lambda: _path("MEETPIPE_TOKEN_DIR", "~/.meetpipe"))
    # Shared team apps: registered once by the app owner, distributed via .env
    google_client_id: str | None = field(default_factory=lambda: os.getenv("GOOGLE_CLIENT_ID") or None)
    google_client_secret: str | None = field(default_factory=lambda: os.getenv("GOOGLE_CLIENT_SECRET") or None)
    github_client_id: str | None = field(default_factory=lambda: os.getenv("GITHUB_CLIENT_ID") or None)
    github_client_secret: str | None = field(default_factory=lambda: os.getenv("GITHUB_CLIENT_SECRET") or None)
    github_app_slug: str | None = field(default_factory=lambda: os.getenv("GITHUB_APP_SLUG") or None)
    after_db_path: Path = field(default_factory=lambda: _path("AFTER_DB_PATH", "after/data/after.db"))
    fireflies_api_key: str | None = field(default_factory=lambda: os.getenv("FIREFLIES_API_KEY") or None)
    # Optional overrides: a personal access token / a downloaded OAuth client JSON
    github_token: str | None = field(default_factory=lambda: os.getenv("GITHUB_TOKEN") or None)
    google_credentials: Path = field(default_factory=lambda: _path("GOOGLE_CREDENTIALS_FILE", "credentials.json"))
    transcripts_dir: Path = field(default_factory=lambda: _path("TRANSCRIPTS_DIR", "transcripts"))
    runs_dir: Path = field(default_factory=lambda: _path("RUNS_DIR", "runs"))
    claude_projects_dir: Path = field(default_factory=lambda: _path("CLAUDE_PROJECTS_DIR", "~/.claude/projects"))
    codex_sessions_dir: Path = field(default_factory=lambda: _path("CODEX_SESSIONS_DIR", "~/.codex/sessions"))
    claude_bin: str | None = field(default_factory=lambda: os.getenv("CLAUDE_BIN") or None)
    codex_bin: str | None = field(default_factory=lambda: os.getenv("CODEX_BIN") or None)
    llm: str = field(default_factory=lambda: os.getenv("LLM", "claude"))
    transcript_senders: list[str] = field(default_factory=lambda: _list(
        "TRANSCRIPT_SENDERS",
        "meet-recordings-noreply@google.com,gemini-notes@google.com,no-reply@zoom.us,"
        "no-reply@otter.ai,fred@fireflies.ai,notetaker@fireflies.ai,read.ai",
    ))
    drive_max_files: int = field(default_factory=lambda: int(os.getenv("DRIVE_MAX_FILES", "300")))
    context_char_budget: int = field(default_factory=lambda: int(os.getenv("CONTEXT_CHAR_BUDGET", "350000")))

    @property
    def google_token(self) -> Path:
        return self.token_dir / "google_token.json"

    @property
    def github_token_file(self) -> Path:
        return self.token_dir / "github_token.json"


settings = Settings()


def set_env(key: str, value: str) -> None:
    """Write/replace KEY=value in .env (used by one-time setup flows) and update the live settings."""
    env = ROOT / ".env"
    lines = env.read_text().splitlines() if env.exists() else []
    lines = [l for l in lines if not l.startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    env.write_text("\n".join(lines) + "\n")
    os.environ[key] = value
    attr = key.lower()
    if hasattr(settings, attr):
        setattr(settings, attr, value)
