"""SQLite store. Placeholder until the shared DB with the Before teammate is decided."""
import json
import sqlite3
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    handle TEXT PRIMARY KEY, name TEXT, role TEXT, email TEXT, slack_user_id TEXT, github TEXT
);
CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY, title TEXT, date TEXT, source TEXT,
    transcript_json TEXT, analysis_json TEXT, goals_json TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS human_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, meeting_id TEXT, title TEXT, owner TEXT, project_role TEXT,
    due_date TEXT, status TEXT, channel TEXT, source_quote TEXT, flags TEXT, delivery TEXT
);
CREATE TABLE IF NOT EXISTS agent_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, meeting_id TEXT, title TEXT, kind TEXT, payload_json TEXT,
    approval TEXT DEFAULT 'pending', result_json TEXT, darwin_json TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, meeting_id TEXT, channel TEXT, target TEXT, body TEXT,
    sent INTEGER, detail TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


@contextmanager
def conn():
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            for u in json.loads((config.SEED_DIR / "team.json").read_text()):
                c.execute(
                    "INSERT INTO users VALUES (?,?,?,?,?,?)",
                    (u["handle"], u["name"], u["role"], u.get("email"), u.get("slack_user_id"), u.get("github")),
                )
        prev = json.loads((config.SEED_DIR / "previous_meeting.json").read_text())
        c.execute(
            "INSERT OR IGNORE INTO meetings (id, title, date, source, goals_json) VALUES (?,?,?,?,?)",
            (prev["id"], prev["title"], prev["date"], "seed", json.dumps(prev["goals"])),
        )


def rows(sql, *args):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def execute(sql, *args) -> int:
    with conn() as c:
        return c.execute(sql, args).lastrowid


def team() -> list[dict]:
    return rows("SELECT * FROM users ORDER BY handle")


def previous_goals(before_date: str) -> list[dict]:
    """Goals set by the latest meeting before this one: what Before would compare against."""
    r = rows(
        "SELECT goals_json FROM meetings WHERE date < ? AND goals_json IS NOT NULL ORDER BY date DESC LIMIT 1",
        before_date,
    )
    return json.loads(r[0]["goals_json"]) if r else []
