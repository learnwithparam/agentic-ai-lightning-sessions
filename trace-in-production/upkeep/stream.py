"""Traces that survive a kill: every span goes to Langfuse the moment it starts, and again when it ends.

Mission Control's processor posts a trace once it finishes, so a worker killed mid-run takes its whole
trace with it, and the spend of that run with it. Here the run, each employee's turn and each step are
posted as they start and again, with their end time, as they end. A killed run leaves every model call
it finished, with its tokens, under its card's session, and the step it died in shows as unfinished.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from agents import set_trace_processors, set_tracing_disabled
from agents.tracing import Span, Trace, TracingProcessor

from upkeep.cost import cost
from workforce.traces import Post, attrs, ids, name, nanos, observation, poster

log = logging.getLogger(__name__)


def batch(rows: list[dict]) -> dict:
    resource = {"attributes": attrs({"service.name": "brekke-maintenance"})}
    return {
        "resourceSpans": [
            {"resource": resource, "scopeSpans": [{"scope": {"name": "openai-agents"}, "spans": rows}]}
        ]
    }


def root(trace: Trace, start: str, end: str) -> dict:
    head = trace.export() or {}
    meta = head.get("metadata") or {}
    return {
        "traceId": ids(trace.trace_id, 32),
        "spanId": ids(trace.trace_id, 16),
        "name": trace.name,
        "kind": 1,
        "startTimeUnixNano": nanos(start),
        "endTimeUnixNano": nanos(end),
        "attributes": attrs(
            {
                "langfuse.trace.name": trace.name,
                "langfuse.session.id": head.get("group_id"),
                "langfuse.trace.metadata.card": meta.get("card"),
                "langfuse.trace.metadata.attempt": meta.get("attempt"),
            }
        ),
    }


def step(span: Span[Any], ended: bool = True) -> dict:
    found = observation(span)
    usage = (span.span_data.export().get("usage") or {}) if span.span_data.type == "generation" else {}
    if usage:
        # Priced here, from the same list price the board uses, so the trace and the card agree.
        found["gen_ai.usage.cost"] = cost(usage.get("input_tokens", 0), usage.get("output_tokens", 0))
    err = span.error
    return {
        "traceId": ids(span.trace_id, 32),
        "spanId": ids(span.span_id, 16),
        "parentSpanId": ids(span.parent_id or span.trace_id, 16),
        "name": name(span),
        "kind": 1,
        "startTimeUnixNano": nanos(span.started_at),
        "endTimeUnixNano": nanos(span.ended_at if ended else span.started_at),
        "attributes": attrs(found),
        "status": {"code": 2, "message": err["message"]} if err else {"code": 1},
    }


def now() -> str:
    return datetime.now(UTC).isoformat()


class Streaming(TracingProcessor):
    def __init__(self, post: Post):
        self.post, self.started = post, {}

    def send(self, rows: list[dict], what: str) -> None:
        try:
            self.post(batch(rows))
        except Exception as e:  # a trace is evidence, not a dependency: the card goes on without it
            log.warning("Langfuse dropped %s: %s", what, e)

    def on_trace_start(self, trace: Trace) -> None:
        at = self.started[trace.trace_id] = now()
        self.send([root(trace, at, at)], trace.name)

    def on_span_start(self, span: Span[Any]) -> None:
        # A model call hangs under its turn and its employee's agent span, which end only after it: posted
        # now, a killed run's calls still say whose they were.
        self.send([step(span, ended=False)], name(span))

    def on_span_end(self, span: Span[Any]) -> None:
        self.send([step(span)], name(span))

    def on_trace_end(self, trace: Trace) -> None:
        self.send([root(trace, self.started.pop(trace.trace_id, now()), now())], trace.name)

    def shutdown(self) -> None:
        pass

    def force_flush(self) -> None:
        pass


def install(host: str, public: str, secret: str) -> None:
    set_trace_processors([Streaming(poster(host, public, secret))])
    set_tracing_disabled(False)
