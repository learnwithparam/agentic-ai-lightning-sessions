"""Where the analyst reads from: GitHub issues, Linear issues and markdown docs.

Each reader returns Source rows with a stable id the answer cites. The fixture readers serve a tenant's
folder under fixtures/, so every test and the offline demo run with no keys. GitHub can also be read
live; Linear is fixture-only until a later step.

Permissions travel with each row: `access` is the groups that may read it, and a search returns only
rows the asker shares a group with. A user with no groups, or a doc missing from access.json, matches
nothing, so a gap in the lists hides a source instead of leaking it.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"


@dataclass(frozen=True)
class Source:
    source_id: str
    kind: str
    title: str
    text: str
    url: str
    access: frozenset[str] = frozenset()


def terms(query: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9]+", query.lower()) if len(word) > 2}


def rank(rows: list[Source], query: str, groups: frozenset[str], limit: int = 3) -> list[Source]:
    """Rows the groups may read, most query terms matched first. A row matching none is never returned."""
    want = terms(query)
    scored = [(len(want & terms(f"{row.title} {row.text}")), row) for row in rows if row.access & groups]
    return [row for hits, row in sorted(scored, key=lambda pair: -pair[0]) if hits > 0][:limit]


def fixture_github(tenant: str) -> list[Source]:
    rows = json.loads((FIXTURES / tenant / "github.json").read_text())
    return [
        Source(
            f"GH#{row['number']}",
            "github",
            row["title"],
            f"{row['state']}. {row['body']}",
            row["url"],
            frozenset(row["access"]),
        )
        for row in rows
    ]


def fixture_linear(tenant: str) -> list[Source]:
    rows = json.loads((FIXTURES / tenant / "linear.json").read_text())
    return [
        Source(
            row["identifier"],
            "linear",
            row["title"],
            f"{row['state']}, {row['assignee']}. {row['description']}",
            row["url"],
            frozenset(row["access"]),
        )
        for row in rows
    ]


def docs(folder: Path) -> list[Source]:
    acl = folder / "access.json"
    access = json.loads(acl.read_text()) if acl.exists() else {}
    out = []
    for doc in sorted(folder.glob("*.md")):
        body = doc.read_text()
        title = body.splitlines()[0].lstrip("# ").strip() if body else doc.stem
        out.append(
            Source(
                f"doc:{doc.stem}",
                "docs",
                title,
                body,
                doc.resolve().as_uri(),
                frozenset(access.get(doc.stem, [])),
            )
        )
    return out


def live_github(repo: str, query: str, access: frozenset[str], token: str | None = None) -> list[Source]:
    """GitHub's issue search for one repository, every row readable by `access`. Public repositories work
    without a token."""
    search = urllib.parse.quote(f"repo:{repo} {query}")
    req = urllib.request.Request(  # noqa: S310 - fixed https host
        f"https://api.github.com/search/issues?q={search}&per_page=3",
        headers={
            "Accept": "application/vnd.github+json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    with urllib.request.urlopen(req, timeout=10) as res:  # noqa: S310 - fixed https host
        items = json.load(res)["items"]
    return [
        Source(
            f"GH#{item['number']}",
            "github",
            item["title"],
            f"{item['state']}. {item.get('body') or ''}"[:1500],
            item["html_url"],
            access,
        )
        for item in items
    ]


def people(tenant: str) -> dict[str, list[str]]:
    return json.loads((FIXTURES / tenant / "people.json").read_text())


@dataclass
class Readers:
    """One tenant's sources, searched as one user. from_env switches GitHub and docs to live ones when
    they are configured."""

    tenant: str = "kaas"
    user: str = ""
    github_repo: str | None = None
    github_token: str | None = None
    github_access: frozenset[str] = frozenset({"engineering"})
    docs_dir: Path | None = None

    @classmethod
    def from_env(cls, tenant: str = "kaas", user: str = "") -> Readers:
        docs_dir = os.environ.get("DOCS_DIR")
        return cls(
            tenant,
            user,
            os.environ.get("GITHUB_REPO") or None,
            os.environ.get("GITHUB_TOKEN") or None,
            frozenset((os.environ.get("GITHUB_ACCESS") or "engineering").split(",")),
            Path(docs_dir) if docs_dir else None,
        )

    def as_user(self, user: str) -> Readers:
        return replace(self, user=user)

    @property
    def groups(self) -> frozenset[str]:
        return frozenset(people(self.tenant).get(self.user, []))

    def github(self, query: str) -> list[Source]:
        if self.github_repo:
            if not self.github_access & self.groups:
                return []
            return live_github(self.github_repo, query, self.github_access, self.github_token)
        return rank(fixture_github(self.tenant), query, self.groups)

    def linear(self, query: str) -> list[Source]:
        return rank(fixture_linear(self.tenant), query, self.groups)

    def docs(self, query: str) -> list[Source]:
        return rank(docs(self.docs_dir or FIXTURES / self.tenant / "docs"), query, self.groups)
