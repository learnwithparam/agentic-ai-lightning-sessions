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
    id: str
    kind: str
    title: str
    text: str
    url: str
    access: frozenset[str] = frozenset()


def terms(query: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2}


def rank(rows: list[Source], query: str, groups: frozenset[str], limit: int = 3) -> list[Source]:
    """Rows the groups may read, most query terms matched first. A row matching none is never returned."""
    want = terms(query)
    scored = [(len(want & terms(f"{r.title} {r.text}")), r) for r in rows if r.access & groups]
    return [r for n, r in sorted(scored, key=lambda x: -x[0]) if n > 0][:limit]


def fixture_github(tenant: str) -> list[Source]:
    rows = json.loads((FIXTURES / tenant / "github.json").read_text())
    return [
        Source(
            f"GH#{r['number']}",
            "github",
            r["title"],
            f"{r['state']}. {r['body']}",
            r["url"],
            frozenset(r["access"]),
        )
        for r in rows
    ]


def fixture_linear(tenant: str) -> list[Source]:
    rows = json.loads((FIXTURES / tenant / "linear.json").read_text())
    return [
        Source(
            r["identifier"],
            "linear",
            r["title"],
            f"{r['state']}, {r['assignee']}. {r['description']}",
            r["url"],
            frozenset(r["access"]),
        )
        for r in rows
    ]


def docs(folder: Path) -> list[Source]:
    acl = folder / "access.json"
    access = json.loads(acl.read_text()) if acl.exists() else {}
    out = []
    for f in sorted(folder.glob("*.md")):
        body = f.read_text()
        title = body.splitlines()[0].lstrip("# ").strip() if body else f.stem
        out.append(
            Source(
                f"doc:{f.stem}", "docs", title, body, f.resolve().as_uri(), frozenset(access.get(f.stem, []))
            )
        )
    return out


def live_github(repo: str, query: str, access: frozenset[str], token: str | None = None) -> list[Source]:
    """GitHub's issue search for one repository, every row readable by `access`. Public repositories work
    without a token."""
    q = urllib.parse.quote(f"repo:{repo} {query}")
    req = urllib.request.Request(  # noqa: S310 - fixed https host
        f"https://api.github.com/search/issues?q={q}&per_page=3",
        headers={
            "Accept": "application/vnd.github+json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    with urllib.request.urlopen(req, timeout=10) as res:  # noqa: S310 - fixed https host
        items = json.load(res)["items"]
    return [
        Source(
            f"GH#{i['number']}",
            "github",
            i["title"],
            f"{i['state']}. {i.get('body') or ''}"[:1500],
            i["html_url"],
            access,
        )
        for i in items
    ]


def people(tenant: str) -> dict[str, list[str]]:
    return json.loads((FIXTURES / tenant / "people.json").read_text())


@dataclass
class Readers:
    """One tenant's sources, searched as one user. from_env switches GitHub and docs to live ones when
    they are configured."""

    tenant: str = "acme"
    user: str = ""
    github_repo: str | None = None
    github_token: str | None = None
    github_access: frozenset[str] = frozenset({"engineering"})
    docs_dir: Path | None = None

    @classmethod
    def from_env(cls, tenant: str = "acme", user: str = "") -> Readers:
        d = os.environ.get("DOCS_DIR")
        return cls(
            tenant,
            user,
            os.environ.get("GITHUB_REPO") or None,
            os.environ.get("GITHUB_TOKEN") or None,
            frozenset((os.environ.get("GITHUB_ACCESS") or "engineering").split(",")),
            Path(d) if d else None,
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
