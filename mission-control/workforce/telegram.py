"""Approval on Telegram: the pending action as a message with Approve and Decline buttons.

The answer is read by long-polling getUpdates, so the demo needs no public URL. Only a tap in
TELEGRAM_CHAT_ID on this card's buttons counts. Who approved is the tapper's numeric Telegram id, which
the GitHub comment records: a username is whatever its owner types, so it cannot name an approver.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from workforce.board import Card

Call = Callable[[str, dict], dict]


def bot_api(token: str) -> Call:
    def call(method: str, payload: dict) -> dict:
        req = urllib.request.Request(  # noqa: S310 - fixed https host
            f"https://api.telegram.org/bot{token}/{method}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=40) as res:  # noqa: S310 - fixed https host
            return json.load(res)["result"]

    return call


@dataclass
class Telegram:
    chat_id: int
    call: Call

    @classmethod
    def from_env(cls) -> Telegram | None:
        token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        return cls(int(chat), bot_api(token)) if token and chat else None

    async def __call__(self, card: Card) -> tuple[str, bool]:
        p = card.pending
        msg = await asyncio.to_thread(
            self.call,
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": f"{card.title}\nReply on GH#{p['issue']}, drafted by {p['maker']}:\n\n{p['body']}\n\n"
                f"Rests on {', '.join(p['rests_on'])}.",
                "reply_markup": {
                    "inline_keyboard": [
                        [
                            {"text": "Approve", "callback_data": f"{card.id}:yes"},
                            {"text": "Decline", "callback_data": f"{card.id}:no"},
                        ]
                    ]
                },
            },
        )
        offset = 0
        while True:
            updates = await asyncio.to_thread(
                self.call,
                "getUpdates",
                {"offset": offset, "timeout": 30, "allowed_updates": ["callback_query"]},
            )
            for u in updates:
                offset = u["update_id"] + 1
                q = u.get("callback_query") or {}
                if q.get("message", {}).get("message_id") != msg["message_id"]:
                    continue
                if q["message"]["chat"]["id"] != self.chat_id or q.get("data") not in (
                    f"{card.id}:yes",
                    f"{card.id}:no",
                ):
                    continue
                yes = q["data"].endswith(":yes")
                await asyncio.to_thread(self.call, "answerCallbackQuery", {"callback_query_id": q["id"]})
                await asyncio.to_thread(
                    self.call,
                    "editMessageReplyMarkup",
                    {"chat_id": self.chat_id, "message_id": msg["message_id"], "reply_markup": {}},
                )
                return f"telegram:{q['from']['id']}", yes
