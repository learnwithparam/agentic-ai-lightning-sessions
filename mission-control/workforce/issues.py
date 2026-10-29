"""KaaS's GitHub issues: the system of record a customer card is checked against.

A reply to a customer is a comment on their GitHub issue. The demo works on a copy of
fixtures/<tenant>/github.json, so a run never edits the fixture; with GITHUB_REPO and GITHUB_TOKEN it
posts to the real repository. Every comment the board posts carries its card id, so approving the same
reply twice posts one comment, and `replied` reads the answer back from GitHub instead of trusting the
agent that said it replied.
"""

from __future__ import annotations

import json
import os
import shutil
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from workforce.board import read_json
from workforce.sources import FIXTURES

AUTHOR = "kaas-support"


def marker(card_id: str) -> str:
    return f"<!-- mission-control {card_id} -->"


@dataclass
class Issues:
    path: Path
    repo: str | None = None
    token: str | None = None

    @classmethod
    def open(cls, path: Path, tenant: str = "kaas") -> Issues:
        """The fixture copy at `path`; the real repository too when GITHUB_REPO and GITHUB_TOKEN are set."""
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(FIXTURES / tenant / "github.json", path)
        repo, token = os.environ.get("GITHUB_REPO") or None, os.environ.get("GITHUB_TOKEN") or None
        return cls(path, repo, token) if repo and token else cls(path)

    def _rows(self) -> list[dict]:
        return read_json(self.path, [])

    def _api(self, path: str, body: dict | None = None, root: str = "") -> object:
        req = urllib.request.Request(  # noqa: S310 - fixed https host
            f"https://api.github.com{root or f'/repos/{self.repo}'}{path}",
            data=json.dumps(body).encode() if body else None,
            headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {self.token}"},
        )
        with urllib.request.urlopen(req, timeout=15) as res:  # noqa: S310 - fixed https host
            return json.load(res)

    def issue(self, number: int) -> dict:
        if self.repo:
            item = self._api(f"/issues/{number}")
            return {
                "number": number,
                "title": item["title"],
                "state": item["state"],
                "author": item["user"]["login"],
                "body": item.get("body") or "",
                "url": item["html_url"],
                "comments": self.comments(number),
            }
        for row in self._rows():
            if row["number"] == number:
                return row
        raise KeyError(f"no issue #{number}")

    def comments(self, number: int) -> list[dict]:
        if self.repo:
            rows = self._api(f"/issues/{number}/comments?per_page=100")
            return [
                {"author": row["user"]["login"], "body": row["body"], "url": row["html_url"]} for row in rows
            ]
        return self.issue(number)["comments"]

    def login(self) -> str:
        """Who the board posts as: the token's own login, or the fixture's support account."""
        if not self.repo:
            return AUTHOR
        if not hasattr(self, "_login"):
            self._login = self._api("", root="/user")["login"]
        return self._login

    def replied(self, number: int, card_id: str) -> dict | None:
        """The comment this card posted, as GitHub has it, or None. Only this closes a reply card."""
        # Anyone can paste the marker into a comment; only one written as the board counts.
        mine = (found for found in self.comments(number) if found["author"] == self.login())
        return next((found for found in mine if marker(card_id) in found["body"]), None)

    def reply(self, number: int, text: str, card_id: str, approved_by: str) -> dict:
        found = self.replied(number, card_id)
        if found:
            return found
        body = f"{text}\n\n{marker(card_id)}"
        if self.repo:
            posted = self._api(f"/issues/{number}/comments", {"body": body})
            return {
                "author": posted["user"]["login"],
                "body": posted["body"],
                "url": posted["html_url"],
            }
        rows = self._rows()
        row = next(candidate for candidate in rows if candidate["number"] == number)
        url = f"{row['url']}#{card_id}"
        comment = {"author": AUTHOR, "body": body, "url": url, "approved_by": approved_by}
        row["comments"].append(comment)
        self.path.write_text(json.dumps(rows, indent=1))
        return comment
