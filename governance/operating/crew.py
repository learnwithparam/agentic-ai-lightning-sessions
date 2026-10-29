"""KaaS Hotels' AI employees: a rate manager, a supplies buyer and a rota planner.

Each reads what it needs and asks for one write. The write goes through the gate in operating/policy.py
first: allowed, it lands in the system of record; above the employee's limit, it waits for the named
signer; refused, the rule goes back to the employee, which may try again inside it. The job's hotel,
night, item or shift comes from code, never a tool argument, and no employee has a tool to approve.
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
    # Off only for `make naive`, which shows what the prompt alone lets through.
    gate: bool = True

    @property
    def name(self) -> str:
        return policy.AGENTS[self.job["agent"]]["name"]


def gate(
    ctx: RunContextWrapper[Work], action: dict, shifts: list[list[str]] = ()
) -> str:
    """Every write an employee asks for: the verdict, its line in the record, then the write if allowed."""
    work = ctx.context
    said = describe(action)
    if not work.gate:
        work.systems.apply(work.job["id"], action)
        work.did = said
        return f"Done: {said}."
    verdict = policy.check(work.job["agent"], action, shifts)
    if verdict.kind == "refuse":
        work.record.add(
            work.job["id"], work.name, said, "refused", verdict.rule, decider="policy"
        )
        return f"Refused by policy. {verdict.rule}. Nothing was written."
    if verdict.kind == "sign":
        work.record.add(
            work.job["id"],
            work.name,
            said,
            "asked",
            verdict.rule,
            decider="policy",
            signer=verdict.signer,
        )
        work.waiting = {
            "action": action,
            "rule": verdict.rule,
            "signer": verdict.signer,
            "agent": work.name,
        }
        work.did = f"asked {policy.first(verdict.signer)}"
        return f"Waiting for {verdict.signer} to sign. {verdict.rule}."
    try:
        ref, how = work.systems.apply(work.job["id"], action)
    except Refused as e:
        return f"The system would not take it: {e}."
    if how == "written":
        work.record.add(
            work.job["id"], work.name, said, "allowed", verdict.rule, decider="policy"
        )
    work.did = said
    return f"Done: {said}."


@function_tool
def read_demand(ctx: RunContextWrapper[Work]) -> str:
    """The night's price now, how full the hotel is, and what the competitors charge."""
    job = ctx.context.job
    night = HOTELS[job["hotel"]]["nights"][job["night"]]
    now = ctx.context.systems.price(job["hotel"], job["night"])
    rivals = ", ".join(f"EUR {price}" for price in night["competitors"])
    return (
        f"{HOTELS[job['hotel']]['name']}, {day(job['night'])}: EUR {now} a night now, {night['booked']}% booked. "
        f"Competitors: {rivals}. {night.get('note', '')}"
    ).strip()


@function_tool
def set_price(ctx: RunContextWrapper[Work], price: int) -> str:
    """Set the night's price in euros."""
    job = ctx.context.job
    was = ctx.context.systems.price(job["hotel"], job["night"])
    return gate(
        ctx,
        {
            "kind": "price",
            "hotel": job["hotel"],
            "night": job["night"],
            "was": was,
            "price": price,
        },
    )


@function_tool
def read_stock(ctx: RunContextWrapper[Work]) -> str:
    """The item's stock against par hotel the hotel, and each supplier's price."""
    job = ctx.context.job
    stock = HOTELS[job["hotel"]]["stock"][job["item"]]
    offers = "; ".join(
        f"{name} EUR {price:.2f}" for name, price in SUPPLIERS[job["item"]].items()
    )
    hotel = HOTELS[job["hotel"]]["name"]
    return f"{stock['item']} hotel {hotel}: {stock['on_hand']} on hand, par {stock['par']}. Suppliers: {offers}."


@function_tool
def place_order(ctx: RunContextWrapper[Work], supplier: str, quantity: int) -> str:
    """Order the item from request supplier."""
    job = ctx.context.job
    prices = SUPPLIERS[job["item"]]
    name = next(
        (known for known in prices if known.lower() == supplier.strip().lower()),
        supplier.strip(),
    )
    if name not in prices:
        return (
            f"No supplier called {supplier} sells this. Suppliers: {', '.join(prices)}."
        )
    total = round(prices[name] * quantity, 2)
    request = {
        "kind": "order",
        "hotel": job["hotel"],
        "item": job["item"],
        "supplier": name,
    }
    return gate(ctx, request | {"quantity": quantity, "total": total})


@function_tool
def read_rota(ctx: RunContextWrapper[Work]) -> str:
    """The shift to cover, and the hotel's staff with their hours this week and their shifts."""
    job = ctx.context.job
    staff = sorted(
        ctx.context.systems.staff(job["hotel"]), key=lambda member: member["hours"]
    )
    rows = "\n".join(
        f"- {member['name']}, {member['hours']} h this week: "
        + "; ".join(clock(begin, finish) for begin, finish in member["shifts"])
        for member in staff
    )
    return f"{job['out']} is off sick. Shift to cover: {clock(*job['shift'])}.\nStaff, fewest hours first:\n{rows}"


@function_tool
def assign_shift(ctx: RunContextWrapper[Work], staff: str) -> str:
    """Give the shift to one member of staff."""
    job = ctx.context.job
    found = next(
        (
            member
            for member in ctx.context.systems.staff(job["hotel"])
            if member["name"].lower() == staff.strip().lower()
        ),
        None,
    )
    if not found:
        return f"No one called {staff} works at {HOTELS[job['hotel']]['name']}."
    request = {
        "kind": "shift",
        "hotel": job["hotel"],
        "staff": found["name"],
        "start": job["shift"][0],
        "end": job["shift"][1],
    }
    return gate(ctx, request, found["shifts"])


PROMPTS = {
    "rates": """You set room prices for KaaS Hotels. Read the night's demand, then set_price once: at or
just under the cheapest competitor when the hotel is under 85% booked, otherwise hold the price.""",
    "supplies": """You buy supplies for KaaS Hotels. Read the stock, then place_order once for enough to
bring the item back to par, from the cheapest supplier. If an order is refused, read why and try again
inside the rule.""",
    "rota": """You plan housekeeping rotas for KaaS Hotels. Read the rota, then assign_shift to cover the
shift, preferring whoever has the fewest hours this week. If an assignment is refused, read why and give
it to someone else.""",
}
TOOLS = {
    "rates": [read_demand, set_price],
    "supplies": [read_stock, place_order],
    "rota": [read_rota, assign_shift],
}


def acted(
    ctx: RunContextWrapper[Work], _: list[FunctionToolResult]
) -> ToolsToFinalOutputResult:
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

    def __init__(self, narrate: Callable[[str], None] | None = None) -> None:
        self.narrate = narrate

    async def on_llm_end(self, context, agent, response) -> None:
        used = context.context.usage.setdefault(agent.name, [0, 0])
        used[0] += response.usage.input_tokens
        used[1] += response.usage.output_tokens

    async def on_tool_start(self, context, agent, tool) -> None:
        context.context.steps.setdefault(agent.name, []).append(tool.name)
        if self.narrate:
            self.narrate(f"{agent.name} called {tool.name}")


# OpenRouter keeps a stalled request alive with keep-alive bytes, so the client's read timeout never fires.
RUN_SECONDS = 120


def model() -> Model | str:
    name = os.environ.get("CREW_MODEL", "openai/gpt-6-luna")
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return name
    client = AsyncOpenAI(
        base_url="https://openrouter.ai/api/v1", api_key=key, timeout=60, max_retries=0
    )
    return OpenAIChatCompletionsModel(model=name, openai_client=client)


def ask(job: dict) -> str:
    return f"Job {job['id']} at {HOTELS[job['hotel']]['name']}: {job['says']}."


async def handle(
    job: dict,
    systems: Systems,
    record: Record,
    llm: Model | str,
    narrate: Callable[[str], None] | None = None,
    gated: bool = True,
) -> Work:
    work = Work(job, systems, record, gate=gated)
    try:
        await asyncio.wait_for(
            Runner.run(
                employee(job["agent"], llm),
                ask(job),
                context=work,
                max_turns=8,
                hooks=Ledger(narrate),
            ),
            RUN_SECONDS,
        )
    except (AgentsException, APIError, TimeoutError) as e:
        work.error = type(e).__name__
    return work
