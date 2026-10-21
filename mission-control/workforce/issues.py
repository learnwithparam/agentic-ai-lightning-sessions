"""Acme's GitHub issues: the system of record a customer card is checked against.

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

from workforce.sources import FIXTURES

AUTHOR = "acme-support"


def marker(card_id: str) -> str:
    return f"<!-- mission-control {card_id} -->"


@dataclass
class Issues:
    path: Path
    repo: str | None = None
    token: str | None = None

    @classmethod
    def open(cls, path: Path, tenant: str = "acme") -> Issues:
        """The fixture copy at `path`; the real repository too when GITHUB_REPO and GITHUB_TOKEN are set."""
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(FIXTURES / tenant / "github.json", path)
        repo, token = os.environ.get("GITHUB_REPO") or None, os.environ.get("GITHUB_TOKEN") or None
        return cls(path, repo, token) if repo and token else cls(path)

    def _rows(self) -> list[dict]:
        return json.loads(self.path.read_text())

    def _api(self, path: str, body: dict | None = None) -> object:
        req = urllib.request.Request(  # noqa: S310 - fixed https host
            f"https://api.github.com/repos/{self.repo}{path}",
            data=json.dumps(body).encode() if body else None,
            headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {self.token}"},
        )
        with urllib.request.urlopen(req, timeout=15) as res:  # noqa: S310 - fixed https host
            return json.load(res)

    def issue(self, number: int) -> dict:
        if self.repo:
            i = self._api(f"/issues/{number}")
            return {
                "number": number,
                "title": i["title"],
                "state": i["state"],
                "author": i["user"]["login"],
                "body": i.get("body") or "",
                "url": i["html_url"],
                "comments": self.comments(number),
            }
        for row in self._rows():
            if row["number"] == number:
                return row
        raise KeyError(f"no issue #{number}")

    def comments(self, number: int) -> list[dict]:
        if self.repo:
            rows = self._api(f"/issues/{number}/comments?per_page=100")
            return [{"author": c["user"]["login"], "body": c["body"], "url": c["html_url"]} for c in rows]
        return self.issue(number)["comments"]

    def replied(self, number: int, card_id: str) -> dict | None:
        """The comment this card posted, as GitHub has it, or None. Only this closes a reply card."""
        return next((c for c in self.comments(number) if marker(card_id) in c["body"]), None)

    def reply(self, number: int, text: str, card_id: str, approved_by: str) -> dict:
        if found := self.replied(number, card_id):
            return found
        body = f"{text}\n\n{marker(card_id)}"
        if self.repo:
            c = self._api(f"/issues/{number}/comments", {"body": body})
            return {"author": c["user"]["login"], "body": c["body"], "url": c["html_url"]}
        rows = self._rows()
        row = next(r for r in rows if r["number"] == number)
        url = f"{row['url']}#{card_id}"
        comment = {"author": AUTHOR, "body": body, "url": url, "approved_by": approved_by}
        row["comments"].append(comment)
        self.path.write_text(json.dumps(rows, indent=1))
        return comment
