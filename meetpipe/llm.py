"""Run prompts through a local coding-agent CLI: Claude Code (`claude -p`) or Codex (`codex exec`)."""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import settings

CLAUDE_FALLBACKS = [
    "~/.claude/local/claude",
    "~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude",
    "~/.cursor/extensions/anthropic.claude-code-*/resources/native-binary/claude",
]
CODEX_FALLBACKS = ["/Applications/ChatGPT.app/Contents/Resources/codex", "/Applications/Codex.app/Contents/Resources/codex"]


def _find(explicit: str | None, name: str, fallbacks: list[str]) -> str | None:
    if explicit:
        return explicit
    if found := shutil.which(name):
        return found
    for pattern in fallbacks:
        hits = sorted(glob.glob(os.path.expanduser(pattern)), key=os.path.getmtime)
        if hits:
            return hits[-1]  # newest install
    return None


def extract_json(text: str) -> dict:
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text[text.find("{"): text.rfind("}") + 1]
    return json.loads(candidate)


class LLM:
    def __init__(self, backend: str | None = None, timeout: int = 900):
        self.backend = backend or settings.llm
        self.timeout = timeout
        if self.backend == "claude":
            self.bin = _find(settings.claude_bin, "claude", CLAUDE_FALLBACKS)
        elif self.backend == "codex":
            self.bin = _find(settings.codex_bin, "codex", CODEX_FALLBACKS)
        else:
            raise ValueError(f"unknown llm backend {self.backend!r} (use claude or codex)")
        if not self.bin:
            raise RuntimeError(f"{self.backend} CLI not found; set {self.backend.upper()}_BIN in .env")

    def run(self, prompt: str) -> str:
        # Run from an empty temp dir so the agent doesn't pick up repo instructions or touch files.
        with tempfile.TemporaryDirectory(prefix="meetpipe-") as tmp:
            if self.backend == "claude":
                cmd = [self.bin, "-p", "--output-format", "text", "--tools", "",
                       "--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands"]
                env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
                out = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=tmp,
                                     timeout=self.timeout, env=env)
                result = out.stdout
            else:
                last = Path(tmp) / "last.txt"
                cmd = [self.bin, "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only",
                       "--color", "never", "-C", tmp, "-o", str(last), "-"]
                out = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=tmp,
                                     timeout=self.timeout)
                result = last.read_text() if last.exists() else ""
        if out.returncode != 0 or not result.strip():
            raise RuntimeError(f"{self.backend} failed (exit {out.returncode}): {(out.stderr or out.stdout)[-800:]}")
        return result.strip()

    def json(self, prompt: str) -> dict:
        text = self.run(prompt)
        try:
            return extract_json(text)
        except (json.JSONDecodeError, ValueError):
            text = self.run(prompt + "\n\nIMPORTANT: respond with ONLY the JSON object, no prose, no code fences.")
            return extract_json(text)
