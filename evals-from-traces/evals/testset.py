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

from evals.desk import ACTIONS, FIXTURES, INQUIRIES, VARIANTS, Record, read_log

LABELS = tomllib.loads((FIXTURES / "labels.toml").read_text())["label"]

Get = Callable[[str], dict]


@dataclass
class Case:
    inquiry: dict
    label: dict
    record: Record

    @property
    def id(self) -> str:
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
    rec = Record(session)
    for o in sorted(observations, key=lambda o: o.get("startTime") or ""):
        name = o.get("name")
        if name == "search_approved":
            rec.searches.append(args(o.get("input")).get("query", ""))
        elif name in ACTIONS and rec.route is None:
            rec.act(name, args(o.get("input")))
        elif o.get("type") == "GENERATION":
            usage = o.get("usageDetails") or {}
            rec.tokens_in += int(usage.get("input") or 0)
            rec.tokens_out += int(usage.get("output") or 0)
    return rec


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


def pull(get: Get, name: str) -> dict[str, Record]:
    """The latest trace of each session with this name, rebuilt into what the run did.

    Langfuse v4 keeps observations, not traces: the root span carries the session, and a trace is the
    rows that share its traceId.
    """
    latest: dict[str, dict] = {}
    for root in sorted(pages(get, name=name, isRootObservation="true"), key=lambda o: o["startTime"]):
        if root.get("sessionId"):
            latest[root["sessionId"]] = root
    return {
        session: from_trace(session, pages(get, traceId=root["traceId"], fields="basic,io,usage"))
        for session, root in latest.items()
    }


def build(state: Path, get: Get | None = None) -> tuple[list[Case], str]:
    """Every labelled inquiry with its production run, and where the runs came from."""
    get = get or langfuse()
    if get:
        v = VARIANTS["production"]
        runs, source = pull(get, v.trace), "Langfuse"
        # Priced like the candidate's runs, from list price, so the two costs compare.
        for rec in runs.values():
            rec.cost = v.cost(rec.tokens_in, rec.tokens_out)
    else:
        runs, source = read_log(state, "production"), "the local run log"
    by_id = {q["id"]: q for q in INQUIRIES}
    cases = [Case(by_id[i], label, runs.get(i, Record(i))) for i, label in LABELS.items()]
    return cases, source
