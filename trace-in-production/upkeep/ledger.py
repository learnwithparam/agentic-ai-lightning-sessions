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
    host = os.environ.get("LANGFUSE_HOST")
    public = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret = os.environ.get("LANGFUSE_SECRET_KEY")
    if not (host and public and secret):
        return None
    try:
        urllib.request.urlopen(f"{host}/api/public/health", timeout=3).close()  # noqa: S310
    except OSError:
        return None
    auth = base64.b64encode(f"{public}:{secret}".encode()).decode()

    def get(path: str) -> dict:
        request = urllib.request.Request(f"{host}{path}", headers={"Authorization": f"Basic {auth}"})  # noqa: S310
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - LANGFUSE_HOST from the env
            return json.load(response)

    return get


def pages(get: Get, **query) -> list[dict]:
    """Every row of Langfuse's v2 observations API for this query, following its cursor."""
    rows, cursor = [], None
    while True:
        params = {**query, "limit": 1000} | ({"cursor": cursor} if cursor else {})
        page = get(f"/api/public/v2/observations?{urllib.parse.urlencode(params)}")
        rows += page["data"]
        cursor = (page.get("meta") or {}).get("cursor")
        if not cursor:
            return rows


def employee(row: dict, by_id: dict[str, dict]) -> str | None:
    """The agent span a model call sits under: its employee."""
    parent = by_id.get(row.get("parentObservationId") or "")
    while parent:
        if parent.get("type") == "AGENT":
            return parent.get("name")
        parent = by_id.get(parent.get("parentObservationId") or "")
    return None


def usage(rows: list[dict]) -> dict[str, list[int]]:
    by_id = {row["id"]: row for row in rows}
    totals: dict[str, list[int]] = {}
    for row in rows:
        if row.get("type") != "GENERATION":
            continue
        counts = row.get("usageDetails") or {}
        tally = totals.setdefault(employee(row, by_id) or "Unknown", [0, 0])
        tally[0] += int(counts.get("input") or 0)
        tally[1] += int(counts.get("output") or 0)
    return totals


def parse_time(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def traces(get: Get, card: str, since: str | None = None) -> list[Traced]:
    """Every run on this card that Langfuse holds, oldest first; with `since`, only this shift's."""
    roots = pages(get, name=TRACE, sessionId=card, isRootObservation="true", fields="basic,metadata")
    # The card id is the session in every shift, so an earlier shift's runs share it.
    if since:
        roots = [
            root
            for root in roots
            if root.get("startTime") and parse_time(root["startTime"]) >= parse_time(since)
        ]
    # The root is posted when a run starts and again when it ends; both rows are one run.
    latest = {root["traceId"]: root for root in sorted(roots, key=lambda root: root.get("startTime") or "")}
    found = []
    for trace_id, root in latest.items():
        rows = pages(get, traceId=trace_id, fields="basic,usage")
        meta = root.get("metadata") or {}
        found.append(Traced(trace_id, root.get("startTime") or "", str(meta.get("attempt", "")), usage(rows)))
    return sorted(found, key=lambda traced: traced.started)
