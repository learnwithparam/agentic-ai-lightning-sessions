"""The crew: a trip planner, a booker and a messenger rebooking Priya's cancelled flight for KaaS Travel.

The planner reads the trip and the flights on sale, the booker books one, and the messenger tells Priya
and the client when she lands. Each reaches KaaS Travel's travel system only through its own gateway,
which knows who it serves. The card is Done when the travel system holds a ticket that lands in time and
both contacts were sent its flight number; `settle` asks the travel system, never the agents. A fare over
the cap waits for Priya's manager, who approves it on the board; then only the messenger runs again. The
solo agent is the comparison: one agent with every tool, approval included, which the travel system still
refuses.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import shutil
import sys
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from agents import Agent, AgentsException, Model, ModelSettings, Runner, Usage
from agents.mcp import MCPServerStdio
from openai import APIError

from crew.gateway import TOOLS_EACH_AGENT_MAY_CALL, AgentName, read_call_log
from crew.travel import TicketStatus, Travel
from workforce import default_model
from workforce.board import Board, Card, Role, Run, price
from workforce.mission import Steps, paint

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / ".crew"
SOLO_STATE = ROOT / ".crew-solo"
MAX_TURNS_PER_RUN = 30
GATEWAY_TIMEOUT_SECONDS = 150
NARRATION_MAX_COLUMNS = 70  # a projected terminal shows about this many


class Column(StrEnum):
    BACKLOG = "Backlog"
    NEEDS_YOU = "Needs you"
    IN_FLIGHT = "In flight"
    DONE = "Done"


class Tone(StrEnum):
    RED = "1;31"
    GREEN = "1;32"
    YELLOW = "1;33"


TEAM = (
    Role(
        AgentName.TRIP_PLANNER,
        "The flights that get the traveller to the meeting, cheapest first",
        "Trips rebooked before the traveller asked",
        "Book anything",
        TOOLS_EACH_AGENT_MAY_CALL[AgentName.TRIP_PLANNER],
    ),
    Role(
        AgentName.BOOKER,
        "One ticket that lands in time, inside the fare cap",
        "Cost per rebooked trip",
        "Approve a fare over the cap",
        TOOLS_EACH_AGENT_MAY_CALL[AgentName.BOOKER],
    ),
    Role(
        AgentName.MESSENGER,
        "The traveller and the client told the new arrival time",
        "Trips where someone was not told",
        "Message anyone who is not on the trip",
        TOOLS_EACH_AGENT_MAY_CALL[AgentName.MESSENGER],
    ),
)
SEED = Card(
    "C-3",
    "Priya's 07:40 flight to Berlin was cancelled; she meets Bergmann at 14:00",
    "Priya Shah",
    "TR-208",
)


@dataclass(frozen=True)
class GatewayRefusal:
    agent: str
    reason: str


class LiveSteps(Steps):
    """Steps that also print one line per handoff and finished tool call, so the room watches it happen."""

    async def on_handoff(self, context, from_agent, to_agent) -> None:
        await super().on_handoff(context, from_agent, to_agent)
        print(
            paint(
                narration_line(from_agent.name, "hands to", to_agent.name), Tone.YELLOW
            ),
            flush=True,
        )

    async def on_tool_end(self, context, agent, tool, result) -> None:
        said = tool_result_text(result)
        tone = Tone.RED if said.startswith("Refused") else Tone.GREEN
        print(paint(narration_line(agent.name, tool.name, said), tone), flush=True)


def tool_result_text(result: object) -> str:
    """What the tool said, from the MCP content the SDK hands back (a dict or its printed form)."""
    content = result
    if isinstance(result, str):
        try:
            content = ast.literal_eval(result)
        except (ValueError, SyntaxError):
            return result
    if isinstance(content, dict) and "text" in content:
        return str(content["text"])
    return str(result)


def narration_line(agent: str, action: str, said: str) -> str:
    first_line = said.splitlines()[0] if said else ""
    return f"{agent:<13}{action:<15}{first_line}"[:NARRATION_MAX_COLUMNS]


def open_gateway(agent: AgentName, state: Path, card: Card) -> MCPServerStdio:
    """One gateway process per employee. It inherits no keys, so a tool cannot reach the model's account."""
    gateway_arguments = [
        "-m",
        "crew.gateway",
        "--as",
        agent,
        "--state",
        str(state),
        "--card",
        card.id,
    ]
    return MCPServerStdio(
        {"command": sys.executable, "args": gateway_arguments, "cwd": str(ROOT)},
        name=f"gateway:{agent}",
        client_session_timeout_seconds=GATEWAY_TIMEOUT_SECONDS,
    )


def build_agent(
    name: AgentName,
    gateway: MCPServerStdio,
    model: Model | str,
    instructions: str,
    **options,
) -> Agent:
    # A small model handed a card may answer with its own name; its first move must be a tool.
    return Agent(
        name=name,
        instructions=instructions,
        mcp_servers=[gateway],
        model=model,
        model_settings=ModelSettings(tool_choice="required"),
        **options,
    )


def build_messenger(gateway: MCPServerStdio, model: Model | str) -> Agent:
    return build_agent(
        AgentName.MESSENGER,
        gateway,
        model,
        "You keep people on a trip informed. Read the ticket with read_ticket. If it is ticketed, send "
        "each contact on the trip one short message with the flight number and landing time. If it waits "
        "for approval, tell the traveller only that. Never message anyone who is not a contact.",
        handoff_description="Tells the traveller and the client about the new flight.",
    )


def build_crew(gateways: dict[AgentName, MCPServerStdio], model: Model | str) -> Agent:
    booker = build_agent(
        AgentName.BOOKER,
        gateways[AgentName.BOOKER],
        model,
        "You book flights for KaaS Travel's travellers. Book the flight the planner chose with "
        "book_flight. If the travel system refuses it, book the next option the planner listed. Hand the card "
        "to the messenger only after a booking went through or every option was refused.",
        handoff_description="Books one flight from the planner's options.",
        handoffs=[build_messenger(gateways[AgentName.MESSENGER], model)],
        reset_tool_choice=False,
    )
    return build_agent(
        AgentName.TRIP_PLANNER,
        gateways[AgentName.TRIP_PLANNER],
        model,
        "You rebook cancelled trips at KaaS Travel. Read the trip with read_trip: it holds the meeting, "
        "the landing buffer and the fare cap. Search the flights, pick the cheapest one with a seat that "
        "gets the traveller there in time, inside the cap if one does. Hand the card to the booker with "
        "your pick and one fallback.",
        handoffs=[booker],
        # The planner and the booker pass the card on, so their last move is a hand-off, never a reply.
        reset_tool_choice=False,
    )


def build_solo(gateway: MCPServerStdio, model: Model | str) -> Agent:
    return build_agent(
        AgentName.SOLO,
        gateway,
        model,
        "You rebook cancelled trips at KaaS Travel alone. Read the trip, search the flights, book the one "
        "that gets the traveller there soonest, approve its fare if it needs approval, and message every "
        "contact.",
    )


def settle(board: Board, card: Card, travel: Travel) -> None:
    ticket = travel.ticket(card.id)
    trip = travel.trip()
    if ticket is None:
        board.move(card, Column.BACKLOG, "no flight was booked")
        return
    if ticket["status"] != TicketStatus.TICKETED:
        waiting = f"£{ticket['fare']} on {ticket['flight']} waits for {trip['manager']}"
        board.move(card, Column.NEEDS_YOU, waiting)
        return
    not_told = sorted(set(trip["contacts"]) - set(travel.told(card.id)))
    if not_told:
        unsent = (
            f"{ticket['flight']} is ticketed but {', '.join(not_told)} was not told"
        )
        board.move(card, Column.NEEDS_YOU, unsent)
        return
    told = " and ".join(trip["contacts"])
    board.move(
        card,
        Column.DONE,
        f"{ticket['flight']} ticketed, lands {ticket['lands']}, {told} told",
    )


def gateway_refusals(state: Path, card: Card) -> list[GatewayRefusal]:
    return [
        GatewayRefusal(row["agent"], row["said"])
        for row in read_call_log(state, card.id)
        if not row["allowed"]
    ]


@dataclass
class Outcome:
    usage: Usage
    succeeded: bool
    said: str


async def run_to_the_end(first_agent: Agent, prompt: str, steps: Steps) -> Outcome:
    try:
        result = await Runner.run(
            first_agent, prompt, max_turns=MAX_TURNS_PER_RUN, hooks=steps
        )
    except AgentsException as failure:
        run_data = failure.run_data
        usage = run_data.context_wrapper.usage if run_data else Usage()
        return Outcome(usage, succeeded=False, said=f"run failed: {failure}")
    except APIError as failure:
        return Outcome(
            steps.usage, succeeded=False, said=f"run failed: {type(failure).__name__}"
        )
    return Outcome(
        result.context_wrapper.usage, succeeded=True, said=str(result.final_output)
    )


async def run_employees(
    board: Board,
    card: Card,
    employees: list[AgentName],
    build_first_agent: Callable[[dict[AgentName, MCPServerStdio], Model | str], Agent],
    model: Model | str | None,
    live: bool,
) -> Card:
    model = model or default_model()
    state = board.path.parent
    board.move(card, Column.IN_FLIGHT, f"{employees[0]} picked it up")
    steps = LiveSteps() if live else Steps()
    prompt = (
        f"Card {card.id}: {card.title}\nTraveller: {card.customer}\nTrip: {card.issue}"
    )
    refused_before = len(gateway_refusals(state, card))
    async with AsyncExitStack() as stack:
        gateways = {
            employee: await stack.enter_async_context(
                open_gateway(employee, state, card)
            )
            for employee in employees
        }
        outcome = await run_to_the_end(
            build_first_agent(gateways, model), prompt, steps
        )
    # A comma list, so the team page credits each employee on the crew with the card's outcome.
    card.employee = card.employee or ", ".join(employees)
    new_refusals = gateway_refusals(state, card)[refused_before:]
    refusal_steps = [
        f"Gateway refused {each.agent}: {each.reason}" for each in new_refusals
    ]
    card.runs.append(
        Run(
            card.employee,
            outcome.usage.input_tokens,
            outcome.usage.output_tokens,
            price(outcome.usage),
            outcome.succeeded,
            outcome.said,
            steps.seen + refusal_steps,
        )
    )
    card.log.extend(f"Gate: {each.agent}: {each.reason}" for each in new_refusals)
    settle(board, card, Travel(state))
    return card


async def work(
    board: Board,
    card: Card,
    model: Model | str | None = None,
    alone: bool = False,
    live: bool = False,
) -> Card:
    if alone:
        employees = [AgentName.SOLO]

        def build_first_agent(gateways, chosen_model):
            return build_solo(gateways[AgentName.SOLO], chosen_model)
    else:
        employees = [AgentName.TRIP_PLANNER, AgentName.BOOKER, AgentName.MESSENGER]
        build_first_agent = build_crew
    card = await run_employees(board, card, employees, build_first_agent, model, live)
    # A real model sometimes hands over before booking; the card has no ticket, so the crew tries once more.
    if not alone and Travel(board.path.parent).ticket(card.id) is None:
        card = await run_employees(
            board, card, employees, build_first_agent, model, live
        )
    return card


async def resume_after_approval(
    board: Board, card: Card, model: Model | str | None = None, live: bool = False
) -> Card:
    """The manager approved the held fare on the board: only the messenger runs, then the card settles."""

    def build_first_agent(gateways, chosen_model):
        return build_messenger(gateways[AgentName.MESSENGER], chosen_model)

    return await run_employees(
        board, card, [AgentName.MESSENGER], build_first_agent, model, live
    )


def report(board: Board, card: Card) -> None:
    """The result as the room reads it off a projector, after the live lines."""
    ticket = Travel(board.path.parent).ticket(card.id)
    if ticket:
        is_ticketed = ticket["status"] == TicketStatus.TICKETED
        status = paint(ticket["status"], Tone.GREEN if is_ticketed else Tone.YELLOW)
        print(
            f"{ticket['flight']} lands {ticket['lands']}, £{ticket['fare']}: {status}"
        )
    last_run = card.runs[-1]
    print(f"{last_run.input_tokens + last_run.output_tokens:,} tokens")
    outcome = card.log[-1].split(": ", 1)[-1]
    print(
        paint(
            f"{card.id} {card.column}: {outcome}",
            Tone.GREEN if card.column == Column.DONE else Tone.YELLOW,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--solo",
        action="store_true",
        help="one agent with every tool, in its own state",
    )
    parser.add_argument(
        "--serve", action="store_true", help="serve the board for the run in progress"
    )
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument(
        "--sold-out", metavar="FLIGHT", help="sell out one flight before the run starts"
    )
    args = parser.parse_args()
    state = SOLO_STATE if args.solo else STATE
    if args.serve:
        from crew.board import serve

        serve(state, TEAM, args.port)
        return
    # Every run starts from the cancelled trip, so the room sees it rebooked once.
    shutil.rmtree(state, ignore_errors=True)
    board = Board(state / "board.json", [replace(SEED, runs=[], log=[])])
    board.save()
    if args.sold_out:
        Travel.open(state).mark_full(args.sold_out)
    card = asyncio.run(work(board, board.cards[0], alone=args.solo, live=True))
    report(board, card)


if __name__ == "__main__":
    main()
