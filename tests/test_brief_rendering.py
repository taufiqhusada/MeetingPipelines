"""Verify the short Slack preview stays separate from its evidence-rich attachment."""

import unittest
from pathlib import Path
from unittest.mock import Mock

from meetpipe import brief, slack_bot
from meetpipe.runner import RunResult


class BriefRenderingTests(unittest.TestCase):
    def setUp(self):
        self.data = {
            "headline": "Worker rollout awaits review.",
            "team_goal": "Ship reliable meeting prep.",
            "people": [{"name": "Alice", "progress": [], "next": [], "wip": [
                {"text": "Worker rollout awaits review", "evidence": ["alice:CX-1", "shared:GH-PR-2"]},
                {"text": "Extra detail for the attachment", "evidence": []},
            ]}, {"name": "Empty", "progress": [], "wip": [], "next": []}],
            "meeting_goals": [{"text": "Assign the rollout reviewer", "evidence": ["shared:GH-PR-2"]}],
            "progress": [{"summary": "Detailed implementation and validation context", "evidence": ["shared:GH-PR-2"]}],
            "coverage_notes": "Legacy collection diagnostics",
        }
        self.index = {"shared:GH-PR-2": {"url": "https://example.com/pr/2"}}

    def test_preview_leads_with_decisions_and_omits_empty_people(self):
        preview = brief.to_people_markdown(self.data, self.index, "Weekly sync")
        self.assertLess(preview.index("Decisions for this meeting"), preview.index("Alice"))
        self.assertNotIn("Empty", preview)
        self.assertNotIn("Extra detail", preview)
        self.assertIn("https://example.com/pr/2", preview)
        self.assertIn("attached brief.md", preview)
        detailed = brief.to_markdown(self.data, self.index, "Weekly sync")
        self.assertIn("Detailed implementation", detailed)
        self.assertIn("shared:GH-PR-2", detailed)
        self.assertNotIn("Legacy collection diagnostics", detailed)

    def test_posting_without_people_does_not_expand_to_detailed_markdown(self):
        data = {k: v for k, v in self.data.items() if k != "people"}
        result = RunResult(Path("/tmp/fixture"), {"gmail": {"status": "skipped"}},
                           self.index, brief=data, markdown="Full detail should not appear", title="Sync")
        client = Mock()
        slack_bot.post_brief("C123456", result, client=client)
        text = "\n".join(c.kwargs["text"] for c in client.chat_postMessage.call_args_list)
        self.assertIn("Assign the rollout reviewer", text)
        self.assertNotIn("Full detail", text)
        self.assertNotIn("Detailed implementation", text)
        self.assertNotIn("Sources:", client.files_upload_v2.call_args.kwargs["initial_comment"])


if __name__ == "__main__":
    unittest.main()
