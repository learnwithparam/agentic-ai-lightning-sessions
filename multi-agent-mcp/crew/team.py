"""The crew: a trip planner, a booker and a messenger rebooking Priya's cancelled flight on Acme's board.

The planner reads the trip and the flights on sale, the booker books one, and the messenger tells Priya
and the client when she lands. Each reaches Acme's travel system only through its own gateway, which
knows who it serves. The card is Done when the travel system holds a ticket that lands in time and both
contacts were sent its flight number; `settle` asks the travel system, never the agents. A fare over the
cap waits for Priya's manager. The solo agent is the comparison: one agent with every tool, approval
included, which the travel system still refuses.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from contextlib import AsyncExitStack
from dataclasses import replace
from pathlib import Path

from agents import Agent, AgentsException, Model, ModelSettings, Runner, Usage
from agents.mcp import MCPServerStdio
from openai import APIError

from crew.gateway import BOOKER, MESSENGER, PLANNER, SCOPES, SOLO, calls
from crew.travel import Travel
from workforce import default_model
from workforce.board import Board, Card, Role, Run, price
from workforce.mission import Steps, paint

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / ".crew"
TEAM = (
    Role(
        PLANNER,
        "The flights that get the traveller to the meeting, cheapest first",
        "Trips rebooked before the traveller asked",
        "Book anything",
        SCOPES[PLANNER],
    ),
    Role(
        BOOKER,
        "One ticket that lands in time, inside the fare cap",
        "Cost per rebooked trip",
        "Approve a fare over the cap",
        SCOPES[BOOKER],
    ),
    Role(
        MESSENGER,
        "The traveller and the client told the new arrival time",
        "Trips where someone was not told",
        "Message anyone who is not on the trip",
        SCOPES[MESSENGER],
    ),
)
SEED = Card(
    "C-3", "Priya's 07:40 flight to Berlin was cancelled; she meets Bergmann at 14:00", "Priya Shah", "TR-208"
)


def gateway(agent: str, state: Path, card: Card) -> MCPServerStdio:
    """One gateway process per employee. It inherits no keys, so a tool cannot reach the model's account."""
    args = ["-m", "crew.gateway", "--as", agent, "--state", str(state), "--card", card.id]
    return MCPServerStdio(
        {"command": sys.executable, "args": args, "cwd": str(ROOT)},
        name=f"gateway:{agent}",
        client_session_timeout_seconds=150,
    )


def agent(name: str, server: MCPServerStdio, model: Model | str, instructions: str, **kw) -> Agent:
    # A small model handed a card may answer with its own name; its first move must be a tool.
    return Agent(
        name=name,
        instructions=instructions,
        mcp_servers=[server],
        model=model,
        model_settings=ModelSettings(tool_choice="required"),
        **kw,
    )


def crew(servers: dict[str, MCPServerStdio], model: Model | str) -> Agent:
    messenger = agent(
        MESSENGER,
        servers[MESSENGER],
        model,
        "You keep people on a trip informed. Read the ticket with read_ticket. If it is ticketed, send "
        "each contact on the trip one short message with the flight number and landing time. If it waits "
        "for approval, tell the traveller only that. Never message anyone who is not a contact.",
        handoff_description="Tells the traveller and the client about the new flight.",
    )
    booker = agent(
        BOOKER,
        servers[BOOKER],
        model,
        "You book flights for Acme's travellers. Book the flight the planner chose with book_flight. If "
        "the travel system refuses it, book the next option the planner listed. Then hand the card to the "
        "messenger.",
        handoff_description="Books one flight from the planner's options.",
        handoffs=[messenger],
        reset_tool_choice=False,
    )
    return agent(
        PLANNER,
        servers[PLANNER],
        model,
        "You rebook cancelled trips at Acme. Read the trip, then search the flights. Pick the cheapest "
        "flight with a seat that lands at least 90 minutes before the meeting, and inside the fare cap if "
        "one does. Hand the card to the booker with your pick and one fallback.",
        handoffs=[booker],
        # The planner and the booker pass the card on, so their last move is a hand-off, never a reply.
        reset_tool_choice=False,
    )


def solo(server: MCPServerStdio, model: Model | str) -> Agent:
    return agent(
        SOLO,
        server,
        model,
        "You rebook cancelled trips at Acme alone. Read the trip, search the flights, book the one that gets "
        "the traveller there soonest, approve its fare if it needs approval, and message every contact.",
    )


def settle(board: Board, card: Card, travel: Travel) -> None:
    ticket, trip = travel.ticket(card.id), travel.trip()
    if ticket is None:
        board.move(card, "Backlog", "no flight was booked")
    elif ticket["status"] != "ticketed":
        board.move(card, "Needs you", f"£{ticket['fare']} on {ticket['flight']} waits for {trip['manager']}")
    elif missing := sorted(set(trip["contacts"]) - set(travel.told(card.id))):
        board.move(card, "Needs you", f"{ticket['flight']} is ticketed but {', '.join(missing)} was not told")
    else:
        told = " and ".join(trip["contacts"])
        board.move(card, "Done", f"{ticket['flight']} ticketed, lands {ticket['lands']}, {told} told")


async def work(board: Board, card: Card, model: Model | str | None = None, alone: bool = False) -> Card:
    model = model or default_model()
    state = board.path.parent
    travel = Travel.open(state)
    board.move(card, "In flight", f"{SOLO if alone else PLANNER} picked it up")
    names = [SOLO] if alone else [PLANNER, BOOKER, MESSENGER]
    steps = Steps()
    prompt = f"Card {card.id}: {card.title}\nTraveller: {card.customer}\nTrip: {card.issue}"
    async with AsyncExitStack() as stack:
        servers = {n: await stack.enter_async_context(gateway(n, state, card)) for n in names}
        first = solo(servers[SOLO], model) if alone else crew(servers, model)
        try:
            result = await Runner.run(first, prompt, max_turns=30, hooks=steps)
            usage, ok, said = result.context_wrapper.usage, True, str(result.final_output)
        except AgentsException as e:
            run = e.run_data
            usage, ok, said = (run.context_wrapper.usage if run else Usage()), False, f"run failed: {e}"
        except APIError as e:
            usage, ok, said = steps.usage, False, f"run failed: {type(e).__name__}"
    card.employee = SOLO if alone else f"{PLANNER}, {BOOKER} and {MESSENGER}"
    refused = [
        f"Gateway refused {c['agent']}: {c['said'].removeprefix('Refused: ')}"
        for c in calls(state, card.id)
        if not c["allowed"]
    ]
    card.runs.append(
        Run(
            card.employee,
            usage.input_tokens,
            usage.output_tokens,
            price(usage),
            ok,
            said,
            steps.seen + refused,
        )
    )
    card.log.extend(f"Gate: {r.removeprefix('Gateway refused ')}" for r in refused)
    settle(board, card, travel)
    return card


def report(board: Board, card: Card) -> None:
    """The run as the room reads it off a projector: who did what, what the gateway refused, the result."""
    for line in card.runs[-1].steps:
        print(paint(line, "1;31") if line.startswith("Gateway refused") else line)
    ticket = Travel(board.path.parent).ticket(card.id)
    if ticket:
        tone = "1;32" if ticket["status"] == "ticketed" else "1;33"
        print(
            f"{ticket['flight']} lands {ticket['lands']}, £{ticket['fare']}: {paint(ticket['status'], tone)}"
        )
    run = card.runs[-1]
    print(f"{run.input_tokens + run.output_tokens:,} tokens")
    print(
        paint(
            f"{card.id} {card.column}: {card.log[-1].split(': ', 1)[-1]}",
            "1;32" if card.column == "Done" else "1;33",
        )
    )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--solo", action="store_true", help="one agent with every tool, in its own state")
    p.add_argument("--serve", action="store_true", help="serve the board from the last run")
    p.add_argument("--port", type=int, default=8090)
    a = p.parse_args()
    state = STATE.with_name(".crew-solo") if a.solo else STATE
    if a.serve:
        from crew.board import serve

        serve(STATE, TEAM, a.port)
        return
    # Every run starts from the cancelled trip, so the room sees it rebooked once.
    shutil.rmtree(state, ignore_errors=True)
    board = Board(state / "board.json", [replace(SEED, runs=[], log=[])])
    board.save()
    card = asyncio.run(work(board, board.cards[0], alone=a.solo))
    report(board, card)


if __name__ == "__main__":
    main()
