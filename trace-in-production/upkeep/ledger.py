"""What Langfuse holds for a card: one trace per run, and each employee's tokens in it.

Langfuse v4 keeps observations, not traces: a run's root span carries the card as its session, and its
steps are the rows that share the root's traceId. A model call belongs to the employee whose agent span
it sits under. A run killed mid-way is a trace like any other, so its spend is read back from here.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from upkeep.crew import TRACE

Get = Callable[[str], dict]


@dataclass
class Traced:
    """One run as Langfuse holds it."""

    trace: str
    started: str
    attempt: str
    usage: dict[str, list[int]] = field(default_factory=dict)


def langfuse() -> Get | None:
    """Langfuse's API when its keys are set and it answers its health check, else None."""
    host, public, secret = (
        os.environ.get(k) for k in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
    )
    if not (host and public and secret):
        return None
    try:
        urllib.request.urlopen(f"{host}/api/public/health", timeout=3).close()  # noqa: S310
    except OSError:
        return None
    auth = base64.b64encode(f"{public}:{secret}".encode()).decode()

    def get(path: str) -> dict:
        req = urllib.request.Request(f"{host}{path}", headers={"Authorization": f"Basic {auth}"})  # noqa: S310
        with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 - LANGFUSE_HOST from the env
            return json.load(r)

    return get


def pages(get: Get, **query) -> list[dict]:
    """Every row of Langfuse's v2 observations API for this query, following its cursor."""
    rows, cursor = [], None
    while True:
        q = {**query, "limit": 1000} | ({"cursor": cursor} if cursor else {})
        page = get(f"/api/public/v2/observations?{urllib.parse.urlencode(q)}")
        rows += page["data"]
        if not (cursor := (page.get("meta") or {}).get("cursor")):
            return rows


def employee(row: dict, by_id: dict[str, dict]) -> str | None:
    """The agent span a model call sits under: its employee."""
    seen = row
    while seen := by_id.get(seen.get("parentObservationId") or ""):
        if seen.get("type") == "AGENT":
            return seen.get("name")
    return None


def usage(rows: list[dict]) -> dict[str, list[int]]:
    by_id = {r["id"]: r for r in rows}
    out: dict[str, list[int]] = {}
    for r in rows:
        if r.get("type") != "GENERATION":
            continue
        u, who = r.get("usageDetails") or {}, employee(r, by_id) or "Unknown"
        tally = out.setdefault(who, [0, 0])
        tally[0] += int(u.get("input") or 0)
        tally[1] += int(u.get("output") or 0)
    return out


def at(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def traces(get: Get, card: str, since: str | None = None) -> list[Traced]:
    """Every run on this card that Langfuse holds, oldest first; with `since`, only this shift's."""
    roots = pages(get, name=TRACE, sessionId=card, isRootObservation="true", fields="basic,metadata")
    # The card id is the session in every shift, so an earlier shift's runs share it.
    if since:
        roots = [r for r in roots if r.get("startTime") and at(r["startTime"]) >= at(since)]
    # The root is posted when a run starts and again when it ends; both rows are one run.
    latest = {r["traceId"]: r for r in sorted(roots, key=lambda r: r.get("startTime") or "")}
    out = []
    for tid, r in latest.items():
        rows = pages(get, traceId=tid, fields="basic,usage")
        meta = r.get("metadata") or {}
        out.append(Traced(tid, r.get("startTime") or "", str(meta.get("attempt", "")), usage(rows)))
    return sorted(out, key=lambda t: t.started)
