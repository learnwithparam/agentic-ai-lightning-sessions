"""The MCP gateway every AI agent reaches KaaS Travel's travel system through.

One gateway process runs per agent. It knows who it serves from how it was started, never from what the
model says. It lists only that agent's tools, refuses any other call, and writes every call, allowed or
refused, to the card's call log. The travel system's own refusals land in the same log.
"""

from __future__ import annotations

import argparse
import functools
import json
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from crew.travel import Refused, Travel

ARGUMENT_MAX_CHARACTERS = 80
SAID_MAX_CHARACTERS = 160


class AgentName(StrEnum):
    TRIP_PLANNER = "Trip planner"
    BOOKER = "Booker"
    MESSENGER = "Messenger"
    SOLO = "Solo agent"


# The solo agent exists to show what one agent with every tool can do, so it also holds approve_fare,
# which no agent on the crew has.
TOOLS_EACH_AGENT_MAY_CALL = {
    AgentName.TRIP_PLANNER: ("read_trip", "search_flights"),
    AgentName.BOOKER: ("read_trip", "book_flight"),
    AgentName.MESSENGER: ("read_trip", "read_ticket", "send_message"),
    AgentName.SOLO: (
        "read_trip",
        "search_flights",
        "book_flight",
        "read_ticket",
        "send_message",
        "approve_fare",
    ),
}


def write_call_log_row(
    call_log: Path,
    card: str,
    who: str,
    tool: str,
    arguments: dict,
    allowed: bool,
    said: str,
) -> None:
    """One line per call. People acting on the board write here too, so one log tells the whole story."""
    row = {
        "at": time.time(),
        "card": card,
        "agent": who,
        "tool": tool,
        "args": {
            name: str(value)[:ARGUMENT_MAX_CHARACTERS]
            for name, value in arguments.items()
        },
        "allowed": allowed,
        "said": said[:SAID_MAX_CHARACTERS],
    }
    with call_log.open("a") as log_file:
        log_file.write(json.dumps(row) + "\n")


class Gateway(MCPServer):
    def __init__(self, agent: AgentName, state: Path, card: str) -> None:
        super().__init__(
            name="kaas-travel", instructions=f"KaaS Travel's travel system, as {agent}"
        )
        self.agent = agent
        self.card = card
        self.travel = Travel.open(state)
        self.call_log = state / "calls.jsonl"
        register_travel_tools(self)

    async def list_tools(self):
        allowed_names = TOOLS_EACH_AGENT_MAY_CALL[self.agent]
        return [
            tool for tool in await super().list_tools() if tool.name in allowed_names
        ]

    def log_call(self, tool: str, arguments: dict, allowed: bool, said: str) -> None:
        write_call_log_row(
            self.call_log, self.card, self.agent, tool, arguments, allowed, said
        )

    def run_if_allowed(
        self, tool: str, arguments: dict, action: Callable[[], str]
    ) -> str:
        """Run the tool only when this agent may call it; log the call and return what the model sees."""
        if tool not in TOOLS_EACH_AGENT_MAY_CALL[self.agent]:
            reason = f"{self.agent} may not call {tool}"
            self.log_call(tool, arguments, allowed=False, said=reason)
            return f"Refused: {reason}"
        try:
            said = action()
        except Refused as refusal_by_the_travel_system:
            self.log_call(
                tool, arguments, allowed=False, said=str(refusal_by_the_travel_system)
            )
            return f"Refused: {refusal_by_the_travel_system}"
        self.log_call(tool, arguments, allowed=True, said=said)
        return said


def read_call_log(state: Path, card: str | None = None) -> list[dict]:
    call_log = state / "calls.jsonl"
    if not call_log.exists():
        return []
    rows = [json.loads(line) for line in call_log.read_text().splitlines()]
    return [row for row in rows if card is None or row["card"] == card]


def describe_flight(flight: dict, lands_in_time: bool) -> str:
    verdict = "lands in time" if lands_in_time else "lands too late for the meeting"
    return (
        f"{flight['id']} {flight['departs']} to {flight['lands']}, {flight['cabin']}, "
        f"£{flight['fare']}, {flight['seats']} seats, {verdict}"
    )


def scoped_tool(gateway: Gateway):
    """Register a function as an MCP tool whose every call goes through the gateway's scope check."""

    def register(action):
        @functools.wraps(action)
        def checked_call(**arguments) -> str:
            return gateway.run_if_allowed(
                action.__name__, arguments, lambda: action(**arguments)
            )

        return gateway.tool()(checked_call)

    return register


def register_travel_tools(gateway: Gateway) -> None:
    travel = gateway.travel

    @scoped_tool(gateway)
    def read_trip() -> str:
        """Read the trip: the traveller, the cancelled flight, the meeting, the fare cap and the contacts."""
        trip = travel.trip()
        return (
            f"{trip['id']} for {trip['traveller']}: {trip['cancelled']} was cancelled.\n"
            f"Meeting {trip['date']} at {trip['meeting']} with {trip['meeting_with']}. "
            f"Hotel: {trip['hotel']}.\n"
            f"Fare cap £{trip['fare_cap']}; over it waits for {trip['manager']}. "
            f"Contacts: {', '.join(trip['contacts'])}."
        )

    @scoped_tool(gateway)
    def search_flights() -> str:
        """List the flights on the trip's route and day, with times, cabin, fare and seats left."""
        return "\n".join(
            describe_flight(flight, travel.lands_in_time(flight))
            for flight in travel.flights()
        )

    @scoped_tool(gateway)
    def book_flight(flight_id: str) -> str:
        """Book a seat on one flight for this card's trip. The travel system checks the rules."""
        ticket = travel.book(gateway.card, flight_id, booked_by=gateway.agent)
        return (
            f"{ticket['id']} on {ticket['flight']}, lands {ticket['lands']}, "
            f"£{ticket['fare']}: {ticket['status']}"
        )

    @scoped_tool(gateway)
    def read_ticket() -> str:
        """Read this card's ticket as the travel system holds it."""
        ticket = travel.ticket(gateway.card)
        if ticket is None:
            return "No ticket yet."
        return (
            f"{ticket['id']} on {ticket['flight']}, departs {ticket['departs']}, "
            f"lands {ticket['lands']}: {ticket['status']}"
        )

    @scoped_tool(gateway)
    def send_message(recipient: str, body: str) -> str:
        """Send a short message to one contact on the trip."""
        message = travel.send(gateway.card, recipient, body, sent_by=gateway.agent)
        return f"Sent to {message['to']}"

    @scoped_tool(gateway)
    def approve_fare(ticket_id: str) -> str:
        """Approve a fare over the trip's cap."""
        ticket = travel.approve(ticket_id, approved_by=gateway.agent, approve=True)
        return f"{ticket['id']} {ticket['status']}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--as", dest="agent", type=AgentName, choices=list(AgentName), required=True
    )
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--card", required=True)
    args = parser.parse_args()
    Gateway(args.agent, args.state, args.card).run("stdio")


if __name__ == "__main__":
    main()
