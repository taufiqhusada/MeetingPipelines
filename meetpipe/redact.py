"""Strip secrets before anything is written to disk or sent to an LLM."""

from __future__ import annotations

import re

_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\b(sk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,})"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    re.compile(r"\b(xox[abposr]-[A-Za-z0-9\-]{10,})"),
    re.compile(r"\b(AKIA[0-9A-Z]{16})\b"),
    re.compile(r"\b(AIza[0-9A-Za-z_\-]{35})\b"),
    re.compile(r"\b(ya29\.[0-9A-Za-z_\-]{20,})"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWT
]
# KEY=value / "token": "value" style assignments
_ASSIGN = re.compile(
    r"""(?i)\b([A-Z0-9_]*(?:api[_-]?key|secret|token|passwd|password|client[_-]?secret)[A-Z0-9_]*)"""
    r"""(\s*[:=]\s*["']?)([^\s"',;]{8,})"""
)


def redact(text: str) -> str:
    if not text:
        return text
    for pat in _PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return _ASSIGN.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
