"""Utilities for sending messages and optional files through Slack."""

import os
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv
from slack_sdk import WebClient

from src.slack.directory import get_channel_mapping, get_user_mapping


load_dotenv()
client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])


def _resolve_channel(recipient):
    name = recipient.removeprefix("#").casefold()

    # Prefer configured channel mappings, including the test channel in .env.
    for key, channel_id in os.environ.items():
        if key.startswith("SLACK_CHANNEL_"):
            configured_name = key.removeprefix("SLACK_CHANNEL_").replace("_", "-").casefold()
            if name in {configured_name, channel_id.casefold()}:
                return channel_id

    if recipient.startswith(("C", "G")):
        return recipient

    for channel_name, channel_id in get_channel_mapping().items():
        if channel_name.casefold() == name:
            return channel_id

    raise ValueError(f"Could not find Slack channel {recipient!r}; check its name and bot access.")


def _resolve_user(recipient):
    candidate = recipient.removeprefix("@").casefold()
    if recipient.startswith(("U", "W")):
        return recipient

    users = get_user_mapping()
    for user_name, user_id in users.items():
        if user_name.casefold() == candidate:
            return user_id
    raise ValueError(f"Could not find Slack user {recipient!r}; pass their Slack username or ID.")


def send_message(recipients: Iterable[str], text: str, files: Iterable[str | Path] | None = None):
    """Send text and optional files to Slack channel names or user names/IDs.

    User recipients receive a DM. The bot needs chat:write and files:write to
    send messages/files, plus channels:read, groups:read, users:read, and
    im:write to resolve names and open DMs. Existing channel ID mappings in
    SLACK_CHANNEL_* environment variables avoid channel lookup calls.
    """
    file_paths = [Path(file).expanduser().resolve() for file in (files or [])]
    for file_path in file_paths:
        if not file_path.is_file():
            raise FileNotFoundError(f"Slack upload file does not exist: {file_path}")

    resolved = []
    for recipient in recipients:
        if not recipient:
            raise ValueError("Recipients must be non-empty channel names or user names/IDs.")
        if recipient.startswith("#") or recipient.startswith(("C", "G")):
            resolved.append((recipient, _resolve_channel(recipient)))
        elif recipient.startswith(("U", "W")):
            user_id = _resolve_user(recipient)
            dm = client.conversations_open(users=user_id)
            resolved.append((recipient, dm["channel"]["id"]))
        else:
            # Bare names can refer to either a channel or a user. Prefer a
            # matching channel, then resolve the name as a user.
            try:
                channel_id = _resolve_channel(recipient)
            except ValueError:
                user_id = _resolve_user(recipient)
                dm = client.conversations_open(users=user_id)
                resolved.append((recipient, dm["channel"]["id"]))
            else:
                resolved.append((recipient, channel_id))

    responses = []
    for recipient, conversation_id in resolved:
        if file_paths:
            response = client.files_upload_v2(
                channel=conversation_id,
                file_uploads=[{"file": str(path), "title": path.name} for path in file_paths],
                initial_comment=text,
            )
        else:
            response = client.chat_postMessage(channel=conversation_id, text=text)
        responses.append(response)
        print(f"Sent message to {recipient}.")
    return responses
