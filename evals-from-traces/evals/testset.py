"""The test set: last week's real production runs, read back out of Langfuse, each joined to its label.

A trace holds the run's tool calls, and the action tool's input is what the desk did: the answer and its
cited sections, the safety summary, or the reason it declined. So a case is rebuilt from the trace alone,
and the local log is the fallback when the room has no Langfuse. A labelled inquiry with no run is still
a case: it fails, because the desk never acted on it.
"""

from __future__ import annotations

import base64
import json
import os
import tomllib
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from evals.desk import (
    ACTIONS,
    FIXTURES,
    INQUIRIES,
    Record,
    production_variant,
    read_log,
)

LABELS = tomllib.loads((FIXTURES / "labels.toml").read_text())["label"]

Get = Callable[[str], dict]


@dataclass
class Case:
    inquiry: dict
    label: dict
    record: Record

    @property
    def inquiry_id(self) -> str:
        return self.inquiry["id"]


def args(raw) -> dict:
    """A tool span's input: Langfuse returns the SDK's JSON arguments as a string, or as an object."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw if isinstance(raw, dict) else {}


def from_trace(session: str, observations: list[dict]) -> Record:
    record = Record(session)
    for span in sorted(observations, key=lambda span: span.get("startTime") or ""):
        name = span.get("name")
        if name == "search_approved":
            record.searches.append(args(span.get("input")).get("query", ""))
        elif name in ACTIONS and record.route is None:
            record.act(name, args(span.get("input")))
        elif span.get("type") == "GENERATION":
            usage = span.get("usageDetails") or {}
            record.tokens_in += int(usage.get("input") or 0)
            record.tokens_out += int(usage.get("output") or 0)
    return record


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
        req = urllib.request.Request(  # noqa: S310
            f"{host}{path}", headers={"Authorization": f"Basic {auth}"}
        )
        with urllib.request.urlopen(req, timeout=15) as response:  # noqa: S310 - LANGFUSE_HOST from the env
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


def pull(get: Get, name: str) -> dict[str, Record]:
    """The latest trace of each session with this name, rebuilt into what the run did.

    Langfuse v4 keeps observations, not traces: the root span carries the session, and a trace is the
    rows that share its traceId.
    """
    latest: dict[str, dict] = {}
    for root in sorted(
        pages(get, name=name, isRootObservation="true"),
        key=lambda span: span["startTime"],
    ):
        if root.get("sessionId"):
            latest[root["sessionId"]] = root
    return {
        session: from_trace(
            session, pages(get, traceId=root["traceId"], fields="basic,io,usage")
        )
        for session, root in latest.items()
    }


def build(state: Path, get: Get | None = None) -> tuple[list[Case], str]:
    """Every labelled inquiry with its production run, and where the runs came from."""
    get = get or langfuse()
    if get:
        variant = production_variant(state)
        runs, source = pull(get, variant.trace), "Langfuse"
        # Priced like the candidate's runs, from list price, so the two costs compare.
        for record in runs.values():
            record.cost = variant.cost(record.tokens_in, record.tokens_out)
    else:
        runs, source = read_log(state, "production"), "the run log"
    by_id = {inquiry["id"]: inquiry for inquiry in INQUIRIES}
    cases = [
        Case(by_id[inquiry_id], label, runs.get(inquiry_id, Record(inquiry_id)))
        for inquiry_id, label in LABELS.items()
    ]
    return cases, source
