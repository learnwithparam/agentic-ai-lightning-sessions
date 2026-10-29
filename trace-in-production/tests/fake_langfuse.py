"""A Langfuse that keeps the OTLP rows it is posted the way Langfuse does, for the shift's tests.

One observation per span id with the latest post winning, answered through the v2 observations API.
Once `alive` is False it records nothing, as a SIGKILL ends every post.
"""

import urllib.parse
from datetime import UTC, datetime


class FakeLangfuse:
    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.alive = True

    def post(self, body: dict) -> None:
        if not self.alive:
            return
        for resource in body["resourceSpans"]:
            for scope in resource["scopeSpans"]:
                for span in scope["spans"]:
                    self.rows[span["spanId"]] = self.observation(span)

    @staticmethod
    def observation(span: dict) -> dict:
        attributes = {item["key"]: next(iter(item["value"].values())) for item in span["attributes"]}
        started = datetime.fromtimestamp(int(span["startTimeUnixNano"]) / 1e9, UTC).isoformat()
        attempt = attributes.get("langfuse.trace.metadata.attempt")
        return {
            "id": span["spanId"],
            "traceId": span["traceId"],
            "parentObservationId": span.get("parentSpanId"),
            "name": span["name"],
            "type": attributes.get("langfuse.observation.type", "span").upper(),
            "startTime": started,
            "sessionId": attributes.get("langfuse.session.id"),
            "metadata": {"attempt": attempt} if attempt is not None else {},
            "usageDetails": {
                "input": int(attributes.get("gen_ai.usage.input_tokens", 0)),
                "output": int(attributes.get("gen_ai.usage.output_tokens", 0)),
            },
        }

    def get(self, path: str) -> dict:
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(path).query))
        rows = list(self.rows.values())
        if "traceId" in query:
            rows = [row for row in rows if row["traceId"] == query["traceId"]]
        if query.get("isRootObservation") == "true":
            rows = [row for row in rows if not row["parentObservationId"]]
        for key in ("name", "sessionId"):
            if key in query:
                rows = [row for row in rows if row[key] == query[key]]
        return {"data": rows, "meta": {}}
