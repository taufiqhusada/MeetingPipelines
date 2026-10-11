"""GitHub PRs, issues and commits via the REST API."""

from __future__ import annotations

import re
import subprocess

import requests

from ..github_auth import token
from ..models import ContextItem, RunScope, parse_ts
from ._agentlog import clip

API = "https://api.github.com"
MAX_PRS, MAX_ISSUES, MAX_COMMENTS = 30, 40, 8


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    if tok := token():
        s.headers["Authorization"] = f"Bearer {tok}"
    return s


def detect_repo(path: str | None) -> str | None:
    """owner/name from the git remote of a local checkout."""
    if not path:
        return None
    out = subprocess.run(["git", "-C", path, "remote", "get-url", "origin"], capture_output=True, text=True)
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", out.stdout.strip())
    return m.group(1) if m else None


def _get(s: requests.Session, url: str, **params) -> list | dict:
    r = s.get(url if url.startswith("http") else API + url, params=params, timeout=30)
    if r.status_code == 403 and r.headers.get("X-RateLimit-Remaining") == "0":
        raise RuntimeError("GitHub rate limit hit; run: uv run run.py connect")
    r.raise_for_status()
    return r.json()


def _comments(s: requests.Session, repo: str, number: int) -> str:
    rows = _get(s, f"/repos/{repo}/issues/{number}/comments", per_page=100)
    rows = [c for c in rows if not (c.get("user") or {}).get("login", "").endswith("[bot]")][-MAX_COMMENTS:]
    return "\n".join(f"- @{c['user']['login']} ({c['created_at'][:10]}): {clip(c.get('body') or '', 600)}"
                     for c in rows)


def _reviews(s: requests.Session, repo: str, number: int) -> str:
    rows = _get(s, f"/repos/{repo}/pulls/{number}/reviews", per_page=100)
    return "\n".join(f"- @{r['user']['login']} {r['state']}: {clip(r.get('body') or '', 400)}"
                     for r in rows if r.get("user"))


def fetch(scope: RunScope) -> list[ContextItem]:
    repo = scope.repo or detect_repo(scope.path)
    if not repo:
        raise RuntimeError("no --repo given and no GitHub remote found at --path")
    s = session()
    since_iso = scope.since.strftime("%Y-%m-%dT%H:%M:%SZ")
    items: list[ContextItem] = []

    prs = []
    for pr in _get(s, f"/repos/{repo}/pulls", state="all", sort="updated", direction="desc", per_page=50):
        if parse_ts(pr["updated_at"]) < scope.since:
            break
        prs.append(pr)
    for pr in prs[:MAX_PRS]:
        n = pr["number"]
        state = "merged" if pr.get("merged_at") else ("draft" if pr.get("draft") else pr["state"])
        body = [f"State: {state} | branch {pr['head']['ref']} → {pr['base']['ref']} | "
                f"author @{pr['user']['login']} | updated {pr['updated_at'][:10]}",
                clip(pr.get("body") or "(no description)", 2500)]
        if reviews := _reviews(s, repo, n):
            body.append("Reviews:\n" + reviews)
        if comments := _comments(s, repo, n):
            body.append("Comments:\n" + comments)
        items.append(ContextItem(
            id=f"GH-PR-{n}", source="github", kind="pr", title=f"PR #{n}: {pr['title']}",
            body="\n\n".join(body), created_at=parse_ts(pr["updated_at"]), url=pr["html_url"],
            author=pr["user"]["login"],
            meta={"state": state, "merged_at": pr.get("merged_at"),
                  "labels": [l["name"] for l in pr.get("labels", [])]},
        ))

    issues = [i for i in _get(s, f"/repos/{repo}/issues", state="all", since=since_iso,
                               sort="updated", direction="desc", per_page=100)
              if "pull_request" not in i][:MAX_ISSUES]
    for it in issues:
        n = it["number"]
        assignees = ", ".join("@" + a["login"] for a in it.get("assignees", [])) or "unassigned"
        body = [f"State: {it['state']} | assignees {assignees} | labels "
                f"{', '.join(l['name'] for l in it.get('labels', [])) or '-'} | updated {it['updated_at'][:10]}",
                clip(it.get("body") or "(no description)", 2000)]
        if it.get("comments") and (comments := _comments(s, repo, n)):
            body.append("Comments:\n" + comments)
        items.append(ContextItem(
            id=f"GH-ISSUE-{n}", source="github", kind="issue", title=f"Issue #{n}: {it['title']}",
            body="\n\n".join(body), created_at=parse_ts(it["updated_at"]), url=it["html_url"],
            author=it["user"]["login"], meta={"state": it["state"], "assignees": assignees},
        ))

    commits = _get(s, f"/repos/{repo}/commits", since=since_iso, per_page=100)
    if commits:
        lines = [f"- {c['sha'][:7]} {c['commit']['author']['date'][:10]} "
                 f"{(c.get('author') or {}).get('login') or c['commit']['author']['name']}: "
                 f"{c['commit']['message'].splitlines()[0]}" for c in commits]
        items.append(ContextItem(
            id="GH-COMMITS", source="github", kind="commit", title=f"{len(commits)} commits on default branch",
            body="\n".join(lines), created_at=parse_ts(commits[0]["commit"]["author"]["date"]),
            url=f"https://github.com/{repo}/commits",
        ))
    return items
