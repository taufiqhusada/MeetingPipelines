"""Exercise Slack startup and job routing without Slack or model network calls."""

import os
import runpy
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from meetpipe import slack_bot
from meetpipe.config import Settings, settings


class LLMSelectionTests(unittest.TestCase):
    def test_default_is_codex_and_environment_override_is_respected(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(Settings().llm, "codex")
        with patch.dict(os.environ, {"LLM": "claude"}):
            self.assertEqual(Settings().llm, "claude")

    def exercise_slack_job(self, start, expected_backend):
        handlers = {}
        app = Mock()

        def register(name):
            def decorate(handler):
                handlers[name] = handler
                return handler
            return decorate

        app.event.side_effect = register

        def immediate_thread(*, target, args, **kwargs):
            return Mock(start=lambda: target(*args))

        with patch.object(settings, "llm", "codex"), \
             patch.dict(os.environ, {
                 "SLACK_BOT_TOKEN": "fixture-bot", "SLACK_APP_TOKEN": "fixture-app",
                 "MEETPIPE_COORDINATOR_URL": "http://127.0.0.1:8766",
                 "MEETPIPE_COORDINATOR_ADMIN_TOKEN": "fixture-admin",
             }), \
             patch("slack_bolt.App", return_value=app), \
             patch("slack_bolt.adapter.socket_mode.SocketModeHandler"), \
             patch.object(slack_bot.threading, "Thread", side_effect=immediate_thread), \
             patch.object(slack_bot, "make_scope", return_value=Mock()), \
             patch.object(slack_bot, "_collect_team_brief", return_value=Mock(markdown=None, status={})) as collect:
            start()
            client = Mock()
            client.chat_postMessage.return_value = {"ts": "123"}
            handlers["app_mention"](
                {"text": "generate meeting prep", "ts": "123", "channel": "fixture", "user": "U123"},
                client, Mock())
            self.assertEqual(collect.call_count, 1)
            self.assertEqual(collect.call_args.args[2], expected_backend)

    def test_compatibility_entry_point_uses_configured_codex_for_jobs(self):
        entry = Path(__file__).resolve().parents[1] / "src" / "slack" / "app.py"
        self.exercise_slack_job(lambda: runpy.run_path(str(entry), run_name="__main__"), "codex")

    def test_explicit_backend_overrides_configuration(self):
        self.exercise_slack_job(lambda: slack_bot.serve(llm_name="claude"), "claude")


if __name__ == "__main__":
    unittest.main()
