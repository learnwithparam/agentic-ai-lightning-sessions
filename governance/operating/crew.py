"""Halden Hotels' AI employees: a rate manager, a supplies buyer and a rota planner.

Each reads what it needs and asks for one write. The write goes through the gate in operating/policy.py
first: allowed, it lands in the system of record; above the employee's limit, it waits for the named
signer; refused, the rule goes back to the employee, which may try again inside it. The job's hotel,
night, item or shift comes from code, never a tool argument, and no employee has a tool to approve.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field

from agents import (
    Agent,
    AgentsException,
    FunctionToolResult,
    Model,
    OpenAIChatCompletionsModel,
    RunContextWrapper,
    RunHooks,
    Runner,
    ToolsToFinalOutputResult,
    function_tool,
)
from openai import APIError, AsyncOpenAI

from operating import policy
from operating.hotels import HOTELS, SUPPLIERS, Refused, Systems, clock, day, describe
from operating.record import Record


@dataclass
class Work:
    """One job's run: the systems of record, the decision record, and what the employee got done."""

    job: dict
    systems: Systems
    record: Record
    did: str = ""
    waiting: dict | None = None
    error: str = ""
    usage: dict[str, list[int]] = field(default_factory=dict)
    steps: dict[str, list[str]] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return policy.AGENTS[self.job["agent"]]["name"]


def gate(ctx: RunContextWrapper[Work], action: dict, shifts: list[list[str]] = ()) -> str:
    """Every write an employee asks for: the verdict, its line in the record, then the write if allowed."""
    w = ctx.context
    v = policy.check(w.job["agent"], action, shifts)
    said = describe(action)
    if v.kind == "refuse":
        w.record.add(w.job["id"], w.name, said, "refused", v.rule, by="policy")
        return f"Refused by policy. {v.rule}. Nothing was written."
    if v.kind == "sign":
        w.record.add(w.job["id"], w.name, said, "asked", v.rule, by="policy", signer=v.signer)
        w.waiting = {"action": action, "rule": v.rule, "signer": v.signer, "agent": w.name}
        w.did = f"asked {policy.first(v.signer)}"
        return f"Waiting for {v.signer} to sign. {v.rule}."
    try:
        ref, how = w.systems.apply(w.job["id"], action)
    except Refused as e:
        return f"The system would not take it: {e}."
    if how == "written":
        w.record.add(w.job["id"], w.name, said, "allowed", v.rule, by="policy")
    w.did = said
    return f"Done: {said}."


@function_tool
def read_demand(ctx: RunContextWrapper[Work]) -> str:
    """The night's price now, how full the hotel is, and what the competitors charge."""
    j = ctx.context.job
    n = HOTELS[j["hotel"]]["nights"][j["night"]]
    now = ctx.context.systems.price(j["hotel"], j["night"])
    rivals = ", ".join(f"EUR {p}" for p in n["competitors"])
    return (
        f"{HOTELS[j['hotel']]['name']}, {day(j['night'])}: EUR {now} a night now, {n['booked']}% booked. "
        f"Competitors: {rivals}. {n.get('note', '')}"
    ).strip()


@function_tool
def set_price(ctx: RunContextWrapper[Work], price: int) -> str:
    """Set the night's price in euros."""
    j = ctx.context.job
    was = ctx.context.systems.price(j["hotel"], j["night"])
    return gate(ctx, {"kind": "price", "hotel": j["hotel"], "night": j["night"], "was": was, "price": price})


@function_tool
def read_stock(ctx: RunContextWrapper[Work]) -> str:
    """The item's stock against par at the hotel, and each supplier's price."""
    j = ctx.context.job
    s = HOTELS[j["hotel"]]["stock"][j["item"]]
    offers = "; ".join(f"{name} EUR {p:.2f}" for name, p in SUPPLIERS[j["item"]].items())
    at = HOTELS[j["hotel"]]["name"]
    return f"{s['item']} at {at}: {s['on_hand']} on hand, par {s['par']}. Suppliers: {offers}."


@function_tool
def place_order(ctx: RunContextWrapper[Work], supplier: str, quantity: int) -> str:
    """Order the item from a supplier."""
    j = ctx.context.job
    prices = SUPPLIERS[j["item"]]
    name = next((s for s in prices if s.lower() == supplier.strip().lower()), supplier.strip())
    if name not in prices:
        return f"No supplier called {supplier} sells this. Suppliers: {', '.join(prices)}."
    total = round(prices[name] * quantity, 2)
    a = {"kind": "order", "hotel": j["hotel"], "item": j["item"], "supplier": name}
    return gate(ctx, a | {"quantity": quantity, "total": total})


@function_tool
def read_rota(ctx: RunContextWrapper[Work]) -> str:
    """The shift to cover, and the hotel's staff with their hours this week and their shifts."""
    j = ctx.context.job
    staff = sorted(ctx.context.systems.staff(j["hotel"]), key=lambda s: s["hours"])
    rows = "\n".join(
        f"- {s['name']}, {s['hours']} h this week: " + "; ".join(clock(a, b) for a, b in s["shifts"])
        for s in staff
    )
    return (
        f"{j['out']} is off sick. Shift to cover: {clock(*j['shift'])}.\nStaff, fewest hours first:\n{rows}"
    )


@function_tool
def assign_shift(ctx: RunContextWrapper[Work], staff: str) -> str:
    """Give the shift to one member of staff."""
    j = ctx.context.job
    found = next(
        (s for s in ctx.context.systems.staff(j["hotel"]) if s["name"].lower() == staff.strip().lower()), None
    )
    if not found:
        return f"No one called {staff} works at {HOTELS[j['hotel']]['name']}."
    a = {
        "kind": "shift",
        "hotel": j["hotel"],
        "staff": found["name"],
        "start": j["shift"][0],
        "end": j["shift"][1],
    }
    return gate(ctx, a, found["shifts"])


PROMPTS = {
    "rates": """You set room prices for Halden Hotels. Read the night's demand, then set_price once: at or
just under the cheapest competitor when the hotel is under 85% booked, otherwise hold the price.""",
    "supplies": """You buy supplies for Halden Hotels. Read the stock, then place_order once for enough to
bring the item back to par, from the cheapest supplier. If an order is refused, read why and try again
inside the rule.""",
    "rota": """You plan housekeeping rotas for Halden Hotels. Read the rota, then assign_shift to cover the
shift, preferring whoever has the fewest hours this week. If an assignment is refused, read why and give
it to someone else.""",
}
TOOLS = {
    "rates": [read_demand, set_price],
    "supplies": [read_stock, place_order],
    "rota": [read_rota, assign_shift],
}


def acted(ctx: RunContextWrapper[Work], _: list[FunctionToolResult]) -> ToolsToFinalOutputResult:
    """A run ends once the write landed or waits for a signer; a refusal goes back to the employee."""
    return ToolsToFinalOutputResult(
        is_final_output=bool(ctx.context.did), final_output=ctx.context.did or None
    )


def employee(key: str, model: Model | str) -> Agent[Work]:
    return Agent[Work](
        name=policy.AGENTS[key]["name"],
        instructions=PROMPTS[key],
        model=model,
        tools=TOOLS[key],
        tool_use_behavior=acted,
    )


class Ledger(RunHooks[Work]):
    """The employee's tokens and steps, counted as the run goes, so the card can credit them."""

    async def on_llm_end(self, context, agent, response) -> None:
        u = context.context.usage.setdefault(agent.name, [0, 0])
        u[0] += response.usage.input_tokens
        u[1] += response.usage.output_tokens

    async def on_tool_start(self, context, agent, tool) -> None:
        context.context.steps.setdefault(agent.name, []).append(tool.name)


# OpenRouter keeps a stalled request alive with keep-alive bytes, so the client's read timeout never fires.
RUN_SECONDS = 120


def model() -> Model | str:
    name = os.environ.get("CREW_MODEL", "openai/gpt-6-luna")
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return name
    client = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=key, timeout=60, max_retries=0)
    return OpenAIChatCompletionsModel(model=name, openai_client=client)


def ask(job: dict) -> str:
    return f"Job {job['id']} at {HOTELS[job['hotel']]['name']}: {job['says']}."


async def handle(job: dict, systems: Systems, record: Record, m: Model | str) -> Work:
    work = Work(job, systems, record)
    try:
        await asyncio.wait_for(
            Runner.run(employee(job["agent"], m), ask(job), context=work, max_turns=8, hooks=Ledger()),
            RUN_SECONDS,
        )
    except (AgentsException, APIError, TimeoutError) as e:
        work.error = type(e).__name__
    return work
