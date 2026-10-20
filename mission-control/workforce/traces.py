"""Every run on a card, kept in Langfuse: each model call with its tokens and cost, each tool and handoff.

The Agents SDK's own exporter posts to OpenAI, so this processor sends the same spans to Langfuse's
OpenTelemetry endpoint instead, one POST per finished trace. The card id is the Langfuse session, so
every run on one card reads as one thread. A failed POST is logged and dropped; it never fails a card.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import urllib.request
from collections.abc import Callable
from datetime import datetime
from typing import Any

from agents import Usage, set_trace_processors, set_tracing_disabled
from agents.tracing import Span, Trace, TracingProcessor

from workforce.board import price

log = logging.getLogger(__name__)
KINDS = {"generation": "generation", "function": "tool", "agent": "agent", "handoff": "event"}

Post = Callable[[dict], None]


def ids(raw: str, size: int) -> str:
    """OpenTelemetry wants 32 hex characters for a trace and 16 for a span; the SDK's ids are longer."""
    return hashlib.sha256(raw.encode()).hexdigest()[:size]


def nanos(at: str | None) -> str:
    return str(int(datetime.fromisoformat(at).timestamp() * 1e9)) if at else "0"


def attrs(values: dict[str, Any]) -> list[dict]:
    out = []
    for k, v in values.items():
        if v is None:
            continue
        if isinstance(v, bool | str):
            value = {"boolValue": v} if isinstance(v, bool) else {"stringValue": v}
        elif isinstance(v, int):
            value = {"intValue": str(v)}
        elif isinstance(v, float):
            value = {"doubleValue": v}
        else:
            value = {"stringValue": json.dumps(v, default=str)}
        out.append({"key": k, "value": value})
    return out


def observation(span: Span[Any]) -> dict[str, Any]:
    data = span.span_data.export()
    kind = data["type"]
    found: dict[str, Any] = {"langfuse.observation.type": KINDS.get(kind, "span")}
    if kind == "generation":
        usage = data.get("usage") or {}
        tokens_in, tokens_out = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
        found |= {
            "gen_ai.request.model": data.get("model"),
            "gen_ai.usage.input_tokens": tokens_in,
            "gen_ai.usage.output_tokens": tokens_out,
            "gen_ai.usage.cost": price(Usage(input_tokens=tokens_in, output_tokens=tokens_out)),
            "langfuse.observation.input": data.get("input"),
            "langfuse.observation.output": data.get("output"),
        }
    elif kind == "function":
        found |= {
            "langfuse.observation.input": data.get("input"),
            "langfuse.observation.output": data.get("output"),
        }
    elif kind == "handoff":
        found |= {
            "langfuse.observation.input": data.get("from_agent"),
            "langfuse.observation.output": data.get("to_agent"),
        }
    return found


def name(span: Span[Any]) -> str:
    data = span.span_data
    return getattr(data, "name", None) or (data.model if data.type == "generation" else None) or data.type


def otlp(trace: Trace, spans: list[Span[Any]]) -> dict:
    """One trace as OTLP JSON: a root span for the run, the SDK's spans under it."""
    head = trace.export() or {}
    tid, root = ids(trace.trace_id, 32), ids(trace.trace_id, 16)
    meta = head.get("metadata") or {}
    starts = [s.started_at for s in spans if s.started_at] or [None]
    ends = [s.ended_at for s in spans if s.ended_at] or [None]
    rows = [
        {
            "traceId": tid,
            "spanId": root,
            "name": trace.name,
            "kind": 1,
            "startTimeUnixNano": nanos(min(starts)) if starts[0] else "0",
            "endTimeUnixNano": nanos(max(ends)) if ends[0] else "0",
            "attributes": attrs(
                {
                    "langfuse.trace.name": trace.name,
                    "langfuse.session.id": head.get("group_id"),
                    "langfuse.user.id": meta.get("user"),
                    "langfuse.trace.metadata.card": meta.get("card"),
                }
            ),
        }
    ]
    for s in spans:
        err = s.error
        rows.append(
            {
                "traceId": tid,
                "spanId": ids(s.span_id, 16),
                "parentSpanId": ids(s.parent_id, 16) if s.parent_id else root,
                "name": name(s),
                "kind": 1,
                "startTimeUnixNano": nanos(s.started_at),
                "endTimeUnixNano": nanos(s.ended_at),
                "attributes": attrs(observation(s)),
                "status": {"code": 2, "message": err["message"]} if err else {"code": 1},
            }
        )
    resource = {"attributes": attrs({"service.name": "mission-control"})}
    return {
        "resourceSpans": [
            {"resource": resource, "scopeSpans": [{"scope": {"name": "openai-agents"}, "spans": rows}]}
        ]
    }


def poster(host: str, public: str, secret: str) -> Post:
    auth = base64.b64encode(f"{public}:{secret}".encode()).decode()

    def post(body: dict) -> None:
        req = urllib.request.Request(  # noqa: S310 - LANGFUSE_HOST from the env
            f"{host}/api/public/otel/v1/traces",
            data=json.dumps(body, default=str).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"},
        )
        urllib.request.urlopen(req, timeout=10).close()  # noqa: S310

    return post


class Langfuse(TracingProcessor):
    def __init__(self, post: Post):
        self.post, self.open = post, {}

    def on_trace_start(self, trace: Trace) -> None:
        self.open[trace.trace_id] = []

    def on_span_start(self, span: Span[Any]) -> None:
        pass

    def on_span_end(self, span: Span[Any]) -> None:
        self.open.setdefault(span.trace_id, []).append(span)

    def on_trace_end(self, trace: Trace) -> None:
        spans = self.open.pop(trace.trace_id, [])
        try:
            self.post(otlp(trace, spans))
        except Exception as e:  # a trace is evidence, not a dependency: the card goes on without it
            log.warning("Langfuse dropped trace %s: %s", trace.name, e)

    def shutdown(self) -> None:
        pass

    def force_flush(self) -> None:
        pass


def install() -> bool:
    """Send traces to Langfuse when its keys are set. The caller decides what happens otherwise."""
    host, public, secret = (
        os.environ.get(k) for k in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
    )
    if host and public and secret:
        set_trace_processors([Langfuse(poster(host, public, secret))])
        set_tracing_disabled(False)
        return True
    return False
