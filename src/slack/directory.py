"""Fetch Slack workspace channel and user name-to-ID mappings."""

import json
import os

from dotenv import load_dotenv
from slack_sdk import WebClient


load_dotenv()
client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])


def _pages(method, **kwargs):
    cursor = None
    while True:
        response = method(cursor=cursor, **kwargs)
        yield response
        cursor = response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break


def get_channel_mapping():
    """Return visible, non-archived channel names mapped to channel IDs."""
    channels = {}
    for page in _pages(
        client.conversations_list,
        types="public_channel,private_channel",
        limit=1000,
        exclude_archived=True,
    ):
        for channel in page["channels"]:
            channels[channel["name"]] = channel["id"]
    return dict(sorted(channels.items(), key=lambda item: item[0].casefold()))


def get_user_mapping():
    """Return active Slack usernames mapped to user IDs (including bot users)."""
    users = {}
    for page in _pages(client.users_list, limit=1000):
        for user in page["members"]:
            if user.get("deleted") or user.get("name") is None:
                continue
            users[user["name"]] = user["id"]
    return dict(sorted(users.items(), key=lambda item: item[0].casefold()))


def get_workspace_mappings():
    """Return current channel and user mappings from the Slack API."""
    return {
        "channels": get_channel_mapping(),
        "users": get_user_mapping(),
    }


if __name__ == "__main__":
    print(json.dumps(get_workspace_mappings(), indent=2, ensure_ascii=False))
