"""The analyst in Slack: tag it in a thread, it reads the thread and replies in it with sources.

`make slack` runs this over Socket Mode, so no public URL is needed. The handler is a plain function so
the tests drive it with a fake Slack client.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

from agents import SQLiteSession

from workforce.analyst import Reply, answer
from workforce.sources import Readers

MENTION = re.compile(r"<@[A-Z0-9]+>\s*")
MEMORY = Path(os.environ.get("WORKFORCE_MEMORY", Path(__file__).resolve().parent.parent / "memory.sqlite"))


def render(reply: Reply) -> str:
    if not reply.sources:
        return reply.text
    links = "\n".join(f"• <{source.url}|{source.source_id}> {source.title}" for source in reply.sources)
    return f"{reply.text}\n\n*Sources*\n{links}"


async def on_mention(event: dict, client, readers: Readers, memory: Path = MEMORY, model=None) -> str:
    """Read the thread the bot was tagged in, answer as the user who tagged it, and reply in the thread."""
    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]
    history = await client.conversations_replies(channel=channel, ts=thread_ts)
    thread = [
        (message.get("user", "bot"), MENTION.sub("", message.get("text", "")))
        for message in history["messages"]
        if message.get("ts") != event["ts"] and not message.get("bot_id")
    ]
    session = SQLiteSession(f"{channel}:{thread_ts}", str(memory))
    asker = readers.as_user(event["user"])
    reply = await answer(
        MENTION.sub("", event["text"]), readers=asker, thread=thread, session=session, model=model
    )
    text = render(reply)
    await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
    return text


def main() -> None:
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_bolt.async_app import AsyncApp

    app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])
    readers = Readers.from_env()

    @app.event("app_mention")
    async def mentioned(event, client):
        await on_mention(event, client, readers)

    asyncio.run(AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start_async())


if __name__ == "__main__":
    main()
