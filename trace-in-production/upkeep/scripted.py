"""A crew model that does what the prompts ask, with no network: the tests and `make naive` run on it.

Triage reads the sensor, then books, joins or dismisses as the plan names; the planner reads the manual
and books. Each reply carries the same token counts, so spend is a number the tests can assert.
"""

from __future__ import annotations

import re

from agents import Usage
from agents.testing import ModelStep, ScriptedModel, function_call
from agents.tracing import get_current_span

from upkeep.crew import PLANNER_PROMPT
from upkeep.plant import ALERTS

PLAN = {
    "A-01": ("book", "MP-F2-07", "high"),
    "A-02": ("book", "MP-P1-02", "critical"),
    "A-03": ("book", "MP-C3-04", "medium"),
    "A-04": ("book", "MP-K1-11", "high"),
    "A-05": ("attach",),
    "A-06": ("dismiss",),
    "A-07": ("book", "MP-R1-03", "medium"),
}
USAGE = Usage(requests=1, input_tokens=2000, output_tokens=80, total_tokens=2080)


def items(call) -> list[dict]:
    return [item for item in call.input if isinstance(item, dict)] if isinstance(call.input, list) else []


def text(call) -> str:
    if isinstance(call.input, str):
        return call.input
    parts = []
    for item in items(call):
        content = item.get("content") or item.get("output")
        if isinstance(content, str):
            parts.append(content)
        else:
            parts += [part.get("text", "") for part in content or [] if isinstance(part, dict)]
    return " ".join(parts)


def called(call, tool: str) -> bool:
    return any(item.get("type") == "function_call" and item.get("name") == tool for item in items(call))


def crew_model(plan: dict = PLAN) -> ScriptedModel:
    def respond(call) -> ModelStep:
        said = text(call)
        alert_id = re.search(r"Alert (A-\d+)", said).group(1)
        alert = next(row for row in ALERTS if row["id"] == alert_id)
        action = plan[alert_id]
        if call.system_instructions == PLANNER_PROMPT:
            if called(call, "look_up_manual"):
                arguments = {"procedure": action[1], "priority": action[2]}
                step = function_call("book_repair", arguments, call_id=f"b-{alert_id}")
            else:
                step = function_call("look_up_manual", {"machine": alert["machine"]}, call_id=f"m-{alert_id}")
        elif not called(call, "read_sensor"):
            step = function_call("read_sensor", {"machine": alert["machine"]}, call_id=f"r-{alert_id}")
        elif action[0] == "book":
            step = function_call("transfer_to_repair_planner", {}, call_id=f"h-{alert_id}")
        elif action[0] == "attach":
            order = re.search(r"Open orders: (WO-\d+)", said).group(1)
            step = function_call("attach_to_order", {"order_id": order}, call_id=f"j-{alert_id}")
        else:
            step = function_call("dismiss_alert", {"reason": "Back to 0 s blocked"}, call_id=f"d-{alert_id}")
        # The chat-completions model writes its usage onto its generation span; the scripted one does not.
        span = get_current_span()
        if span:
            span.span_data.usage = {"input_tokens": USAGE.input_tokens, "output_tokens": USAGE.output_tokens}
        return ModelStep(output=[step], usage=USAGE)

    return ScriptedModel([ModelStep.respond(respond) for _ in range(200)], emit_traces=True)
