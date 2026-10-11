"""Minimal app mention responder using Socket Mode."""

import os

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler


load_dotenv()

app = App(token=os.environ["SLACK_BOT_TOKEN"])
test_channel_id = os.getenv("SLACK_CHANNEL_TEST_BOT_FEATURES")


@app.event("app_mention")
def respond(event, say):
    # When a test channel is configured, keep this example scoped to it.
    if test_channel_id and event.get("channel") != test_channel_id:
        return

    say(
        text="Hello! My local Python agent is running.",
        thread_ts=event["ts"],
    )


if __name__ == "__main__":
    print("Agent is connecting to Slack...")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
