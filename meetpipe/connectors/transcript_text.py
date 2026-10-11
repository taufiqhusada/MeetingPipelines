"""Normalize transcript formats (.vtt/.srt/html) to plain text."""

from __future__ import annotations

import html
import re

_TS = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3}\s*-->\s*\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3}.*$")


def strip_subtitles(text: str) -> str:
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s == "WEBVTT" or s.isdigit() or _TS.match(s) or s.startswith(("NOTE", "Kind:", "Language:")):
            continue
        s = re.sub(r"<v\s+([^>]+)>", r"\1: ", s)
        s = re.sub(r"</?[^>]+>", "", s)
        if not out or out[-1] != s:
            out.append(s)
    return "\n".join(out)


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style).*?</\1>", "", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>|</h\d>", "\n", raw)
    text = html.unescape(re.sub(r"<[^>]+>", "", raw))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def to_text(name: str, raw: str) -> str:
    lower = name.lower()
    if lower.endswith((".vtt", ".srt")):
        return strip_subtitles(raw)
    if lower.endswith((".html", ".htm")):
        return html_to_text(raw)
    return raw
