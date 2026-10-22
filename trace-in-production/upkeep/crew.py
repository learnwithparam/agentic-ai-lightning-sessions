"""Brekke Dairy's maintenance crew: two AI employees who turn a machine alert into a booked repair.

Alert triage reads the machine's sensor and its open orders, then joins the alert to an open order,
dismisses one that cleared by itself, or hands it to the Repair planner, who books the manual's
procedure with a priority. The alert id is never a tool argument: the code passes it, so the model
cannot book under a key it made up. Each run is one trace with the alert id as its Langfuse session,
and the hooks count each employee's tokens, so the board can say what each one costs.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass, field

from agents import (
    Agent,
    AgentsException,
    FunctionToolResult,
    Model,
    OpenAIChatCompletionsModel,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    Runner,
    ToolsToFinalOutputResult,
    function_tool,
)
from openai import APIError, AsyncOpenAI

from upkeep.plant import MACHINES, PROCEDURES, Cmms, Refused

TRIAGE, PLANNER = "Alert triage", "Repair planner"
TRACE = "Machine alert"

TRIAGE_PROMPT = """You watch the machine alerts on Line 2 of Brekke Dairy's bottling plant. For each alert,
read the machine's sensor first, then do exactly one thing:
- the machine already has an open order for this fault: attach_to_order with that order's id;
- the latest reading is back inside the limit and the alert cleared by itself: dismiss_alert;
- otherwise hand the alert to the Repair planner to book a repair."""

PLANNER_PROMPT = """You book repairs for Brekke Dairy's maintenance team. Look up the machine in the
maintenance manual, then book_repair once with the procedure id for this fault and a priority: critical
for anything that touches food safety, such as pasteurisation temperature; high when the fault will stop
the line or a compressor within the shift; medium otherwise."""


@dataclass
class Shift:
    """One alert's run: the maintenance system, what the crew did in it, and what each employee spent."""

    cmms: Cmms
    alert: dict
    # Fault injection for the demo: called once the order is written, as a deploy killing the worker would.
    kill: Callable[[], None] | None = None
    did: str = ""
    error: str = ""
    usage: dict[str, list[int]] = field(default_factory=dict)
    steps: dict[str, list[str]] = field(default_factory=dict)


def readings(m: dict) -> str:
    return ", ".join(f"{r:g}" for r in m["readings"])


@function_tool
def read_sensor(ctx: RunContextWrapper[Shift], machine: str) -> str:
    """The machine's sensor: its limit, its last five readings oldest first, and its open repair orders."""
    m = MACHINES.get(machine.strip().upper())
    if not m:
        return f"No machine {machine} on Line 2. Machines: {', '.join(MACHINES)}."
    orders = ctx.context.cmms.open_on(m["id"])
    held = "; ".join(f"{o.id} for {PROCEDURES[o.procedure]['fault']}" for o in orders) or "none"
    return (
        f"{m['name']}. {m['sensor']}, limit {m['limit']} {m['unit']}. "
        f"Last five readings: {readings(m)} {m['unit']}. Open orders: {held}."
    )


@function_tool
def attach_to_order(ctx: RunContextWrapper[Shift], order_id: str) -> str:
    """Join this alert to a repair order already open for the same fault."""
    try:
        order = ctx.context.cmms.attach(ctx.context.alert["id"], order_id.strip().upper())
    except Refused as e:
        return str(e)
    ctx.context.did = f"joined {order.id}"
    return f"Joined {order.id}."


@function_tool
def dismiss_alert(ctx: RunContextWrapper[Shift], reason: str) -> str:
    """Dismiss an alert that cleared by itself, with the reading that shows it."""
    ctx.context.cmms.dismiss(ctx.context.alert["id"], reason)
    ctx.context.did = "dismissed"
    return "Dismissed."


@function_tool
def look_up_manual(ctx: RunContextWrapper[Shift], machine: str) -> str:
    """The maintenance manual's procedures for a machine, each with its id, part, skill and hours."""
    rows = [p for p in PROCEDURES.values() if p["machine"] == machine.strip().upper()]
    return (
        "\n".join(
            f"{p['id']}: {p['fault']}. {p['steps']}. Part {p['part']}, {p['skill']}, {p['hours']:g} h."
            for p in rows
        )
        or f"No procedures for {machine}."
    )


@function_tool
def book_repair(ctx: RunContextWrapper[Shift], procedure: str, priority: str) -> str:
    """Book the repair in the maintenance system: the manual's procedure id and critical, high or medium."""
    try:
        order, how = ctx.context.cmms.book(
            ctx.context.alert["id"], procedure.strip().upper(), priority.strip().lower()
        )
    except Refused as e:
        return str(e)
    ctx.context.did = f"{how} {order.id}"
    if how == "booked" and ctx.context.kill:
        ctx.context.kill()
    if how == "already booked":
        return f"{order.id} was already booked for this alert; nothing new was booked."
    return f"{how.capitalize()} {order.id}."


def acted(ctx: RunContextWrapper[Shift], _: list[FunctionToolResult]) -> ToolsToFinalOutputResult:
    """A run ends once the maintenance system holds what the crew did; a refused write goes back to it."""
    return ToolsToFinalOutputResult(
        is_final_output=bool(ctx.context.did), final_output=ctx.context.did or None
    )


def crew(model: Model | str) -> Agent[Shift]:
    planner = Agent[Shift](
        name=PLANNER,
        instructions=PLANNER_PROMPT,
        model=model,
        tools=[look_up_manual, book_repair],
        tool_use_behavior=acted,
    )
    return Agent[Shift](
        name=TRIAGE,
        instructions=TRIAGE_PROMPT,
        model=model,
        tools=[read_sensor, attach_to_order, dismiss_alert],
        handoffs=[planner],
        tool_use_behavior=acted,
    )


class Ledger(RunHooks[Shift]):
    """Each employee's tokens and steps, counted as the run goes, so the card can credit each one."""

    async def on_llm_end(self, context, agent, response) -> None:
        u = context.context.usage.setdefault(agent.name, [0, 0])
        u[0] += response.usage.input_tokens
        u[1] += response.usage.output_tokens

    async def on_tool_start(self, context, agent, tool) -> None:
        context.context.steps.setdefault(agent.name, []).append(tool.name)

    async def on_handoff(self, context, from_agent, to_agent) -> None:
        context.context.steps.setdefault(from_agent.name, []).append(f"handed to {to_agent.name}")


# OpenRouter keeps a stalled request alive with keep-alive bytes, so the client's read timeout never fires.
RUN_SECONDS = 120


def model() -> Model | str:
    name = os.environ.get("CREW_MODEL", "openai/gpt-6-luna")
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return name
    client = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=key, timeout=60, max_retries=0)
    return OpenAIChatCompletionsModel(model=name, openai_client=client)


def ask(alert: dict) -> str:
    m = MACHINES[alert["machine"]]
    return f"Alert {alert['id']} at {alert['at']} on {m['name']} (machine {m['id']}): {alert['says']}."


async def handle(
    alert: dict, cmms: Cmms, m: Model | str, attempt: int, kill: Callable[[], None] | None = None
) -> Shift:
    shift = Shift(cmms, alert, kill)
    config = RunConfig(
        workflow_name=TRACE,
        group_id=alert["id"],
        trace_metadata={"card": alert["id"], "attempt": str(attempt)},
    )
    try:
        await asyncio.wait_for(
            Runner.run(crew(m), ask(alert), context=shift, max_turns=10, run_config=config, hooks=Ledger()),
            RUN_SECONDS,
        )
    except (AgentsException, APIError, TimeoutError) as e:
        shift.error = type(e).__name__
    return shift
