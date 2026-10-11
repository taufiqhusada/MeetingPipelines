"""Summarizer + aggregator agents, and markdown rendering of their JSON."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .llm import LLM

PROMPTS = Path(__file__).parent / "prompts"
STATUS_ICON = {"done": "✅", "in_progress": "🟡", "no_evidence": "❌", "unclear": "❔"}
PRIORITY_ICON = {"high": "🔴", "medium": "🟠", "low": "⚪"}


def summarize(context_md: str, llm: LLM) -> dict:
    prompt = (PROMPTS / "summarize.md").read_text().replace("{context}", context_md)
    return llm.json(prompt)


def people_hint(labels: list[str]) -> str:
    """Display names for the per-person view: MEETPIPE_PEOPLE="taufik=Taufiq,gz1010=George,…" plus worker ids."""
    names = {l: l.capitalize() for l in labels if l != "shared"}
    for pair in os.getenv("MEETPIPE_PEOPLE", "").split(","):
        alias, _, name = pair.partition("=")
        if alias.strip() and name.strip():
            names[alias.strip()] = name.strip()
    return ", ".join(f"{a} → {n}" for a, n in names.items()) or "use the names found in the briefs"


def aggregate(briefs: dict[str, dict], llm: LLM) -> dict:
    labelled = {}
    for label, brief in briefs.items():
        b = json.loads(json.dumps(brief))
        for section in ("progress", "commitments", "gaps", "dependencies_and_conflicts",
                        "action_items", "open_questions"):
            for entry in b.get(section, []):
                entry["evidence"] = [f"{label}:{e}" for e in entry.get("evidence", [])]
                if entry.get("made_in"):
                    entry["made_in"] = f"{label}:{entry['made_in']}"
        labelled[label] = b
    prompt = ((PROMPTS / "aggregate.md").read_text().replace("{people}", people_hint(list(briefs)))
              .replace("{briefs}", json.dumps(labelled, indent=2)))
    return llm.json(prompt)


def _cite(ids: list[str], index: dict[str, dict]) -> str:
    out = []
    for i in ids or []:
        key = i.split(":", 1)[-1]
        url = (index.get(i) or index.get(key) or {}).get("url")
        out.append(f"[{i}]({url})" if url else f"`{i}`")
    return " " + " ".join(out) if out else ""


def to_markdown(brief: dict, index: dict[str, dict], title: str) -> str:
    md = [f"# {title}", "", brief.get("headline", ""), ""]
    if brief.get("meeting_goals"):
        md.append("## 🗳 Decisions for this meeting")
        md += [f"- {g['text']}{_cite(g.get('evidence'), index)}" for g in brief["meeting_goals"]]
        md.append("")
    if brief.get("progress"):
        md.append("## ✅ Progress so far")
        md += [f"- {p['summary']}{_cite(p.get('evidence'), index)}" for p in brief["progress"]]
        md.append("")
    if brief.get("commitments"):
        md.append("## 📌 Commitments from previous meetings")
        for c in brief["commitments"]:
            who = f"**{c['owner']}**: " if c.get("owner") else ""
            due = f" _(due {c['due']})_" if c.get("due") else ""
            icon = STATUS_ICON.get(c.get("status"), "❔")
            md.append(f"- {icon} {who}{c['what']}{due} — {c.get('status', 'unclear')}"
                      f"{_cite(([c['made_in']] if c.get('made_in') else []) + c.get('evidence', []), index)}")
        md.append("")
    if brief.get("gaps"):
        md.append("## 🕳 Gaps")
        md += [f"- {g['description']} — {g.get('why_it_matters', '')}{_cite(g.get('evidence'), index)}"
               for g in brief["gaps"]]
        md.append("")
    if brief.get("dependencies_and_conflicts"):
        md.append("## 🔗 Dependencies & conflicts")
        md += [f"- {d['description']}{_cite(d.get('evidence'), index)}" for d in brief["dependencies_and_conflicts"]]
        md.append("")
    if brief.get("action_items"):
        md.append("## 🎯 Needs action")
        for a in brief["action_items"]:
            who = f" (**{a['owner']}**)" if a.get("owner") else ""
            md.append(f"- {PRIORITY_ICON.get(a.get('priority'), '⚪')} {a['what']}{who}{_cite(a.get('evidence'), index)}")
        md.append("")
    if brief.get("open_questions"):
        md.append("## ❓ Open questions")
        md += [f"- {q['question']}{_cite(q.get('evidence'), index)}" for q in brief["open_questions"]]
        md.append("")
    return "\n".join(md)


PEOPLE_BUCKETS = [("progress", "✅ Done"), ("wip", "🔄 In progress"), ("next", "➡️ Next")]


def _ref(ids: list[str], index: dict[str, dict]) -> str:
    """One short source link per line, e.g. (PR #12), so lines stay readable."""
    for i in ids or []:
        key = i.split(":", 1)[-1]
        url = (index.get(i) or index.get(key) or {}).get("url")
        label = key.removeprefix("GH-").replace("ISSUE-", "#").replace("PR-", "PR #")
        if url:
            return f" ([{label}]({url}))"
    return ""


def to_people_markdown(brief: dict, index: dict[str, dict], title: str) -> str:
    """Short Slack preview: goal, decisions, then compact attributable updates."""
    md = [f"# 📋 {title}", ""]
    if brief.get("team_goal"):
        md += [f"🎯 **Team goal:** {brief['team_goal']}", ""]
    elif brief.get("headline"):
        md += [brief["headline"], ""]
    goals = brief.get("meeting_goals") or [
        {"text": q["question"], "evidence": q.get("evidence", [])}
        for q in brief.get("open_questions", [])
    ]
    if goals:
        md.append("## 🗳 Decisions for this meeting")
        md += [f"• {g['text']}{_ref(g.get('evidence'), index)}" for g in goals[:3]]
        md.append("")
    for person in brief.get("people", []):
        updates = []
        for key, label in PEOPLE_BUCKETS:
            items = person.get(key, [])[:1]
            if items:
                it = items[0]
                updates.append(f"• {label}: {it['text']}{_ref(it.get('evidence'), index)}")
        if updates:
            md += [f"**{person['name']}**", *updates, ""]
    md.append("_Details and evidence in the attached brief.md._")
    return "\n".join(md).strip()
