"""KaaS Travel's travel system: the trip, the flights on sale, the tickets and the messages sent.
It is the truth a card settles on.

The travel rules live here, so no prompt can move them. A ticket is only for the trip's route and day,
on a flight with a seat that lands at least 90 minutes before the meeting. A fare over the trip's cap
waits for the traveller's manager, and only that manager can approve it. A message goes only to a
contact on the trip. A flight is booked only when this system says so.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
MINUTES_FROM_LANDING_TO_THE_MEETING = 90  # landing at BER to a desk in Berlin Mitte
MESSAGE_MAX_CHARACTERS = 500


class TicketStatus(StrEnum):
    TICKETED = "ticketed"
    WAITING_FOR_MANAGER = "waiting for the manager"
    DECLINED = "declined"


class Refused(Exception):
    pass


def minutes_after_midnight(clock_time: str) -> int:
    hours, minutes = clock_time.split(":")
    return int(hours) * 60 + int(minutes)


@dataclass
class Travel:
    state: Path

    @classmethod
    def open(cls, state: Path) -> Travel:
        state.mkdir(parents=True, exist_ok=True)
        if not (state / "travel.json").exists():
            shutil.copy(FIXTURES / "travel.json", state / "travel.json")
        return cls(state)

    def _read(self) -> dict:
        return json.loads((self.state / "travel.json").read_text())

    def _write(self, data: dict) -> None:
        (self.state / "travel.json").write_text(json.dumps(data, indent=1))

    def trip(self) -> dict:
        return self._read()["trip"]

    def flights(self) -> list[dict]:
        data = self._read()
        trip = data["trip"]
        route_and_day = (trip["from"], trip["to"], trip["date"])
        return [
            flight
            for flight in data["flights"]
            if (flight["from"], flight["to"], flight["date"]) == route_and_day
        ]

    def lands_in_time(self, flight: dict) -> bool:
        lands_at = minutes_after_midnight(flight["lands"])
        meeting_at = minutes_after_midnight(self.trip()["meeting"])
        return lands_at + MINUTES_FROM_LANDING_TO_THE_MEETING <= meeting_at

    def mark_full(self, flight_id: str) -> None:
        """Sell out one flight, so the room can watch the crew cope with it."""
        data = self._read()
        next(flight for flight in data["flights"] if flight["id"] == flight_id)[
            "seats"
        ] = 0
        self._write(data)

    def ticket(self, card: str) -> dict | None:
        tickets = self._read()["tickets"]
        live = [
            ticket
            for ticket in tickets
            if ticket["card"] == card and ticket["status"] != TicketStatus.DECLINED
        ]
        return live[-1] if live else None

    def messages(self, card: str) -> list[dict]:
        return [
            message for message in self._read()["messages"] if message["card"] == card
        ]

    def book(self, card: str, flight_id: str, booked_by: str) -> dict:
        data = self._read()
        trip = data["trip"]
        flight = next(
            (each for each in self.flights() if each["id"] == flight_id), None
        )
        if flight is None:
            raise Refused(
                f"{flight_id} is not a flight from {trip['from']} to {trip['to']} on {trip['date']}"
            )
        if flight["seats"] < 1:
            raise Refused(f"{flight_id} is full")
        if not self.lands_in_time(flight):
            raise Refused(
                f"{flight_id} lands {flight['lands']}, too late for the {trip['meeting']} meeting"
            )
        if self.ticket(card):
            raise Refused(f"{card} already has a ticket")
        fare_is_over_the_cap = flight["fare"] > trip["fare_cap"]
        status = (
            TicketStatus.WAITING_FOR_MANAGER
            if fare_is_over_the_cap
            else TicketStatus.TICKETED
        )
        ticket = {
            "id": f"TK-{len(data['tickets']) + 1:03d}",
            "card": card,
            "flight": flight_id,
            "departs": flight["departs"],
            "lands": flight["lands"],
            "cabin": flight["cabin"],
            "fare": flight["fare"],
            "booked_by": booked_by,
            "status": status,
            "approved_by": None,
            "approved_at": None,
        }
        next(each for each in data["flights"] if each["id"] == flight_id)["seats"] -= 1
        data["tickets"].append(ticket)
        self._write(data)
        return ticket

    def approve(self, ticket_id: str, *, approved_by: str, approve: bool) -> dict:
        """Only the traveller's manager, a signed-in human, settles a fare over the cap."""
        data = self._read()
        manager = data["trip"]["manager"]
        ticket = next(
            (each for each in data["tickets"] if each["id"] == ticket_id), None
        )
        if ticket is None:
            raise Refused(f"there is no ticket {ticket_id}")
        if ticket["status"] != TicketStatus.WAITING_FOR_MANAGER:
            raise Refused(f"{ticket_id} is already {ticket['status']}")
        if approved_by != manager:
            raise Refused(
                f"{approved_by} may not approve a fare over the cap; only {manager} can"
            )
        ticket["status"] = TicketStatus.TICKETED if approve else TicketStatus.DECLINED
        ticket["approved_by"] = approved_by
        ticket["approved_at"] = datetime.now().strftime("%H:%M")
        self._write(data)
        return ticket

    def send(self, card: str, recipient: str, body: str, sent_by: str) -> dict:
        data = self._read()
        if recipient not in data["trip"]["contacts"]:
            raise Refused(f"{recipient} is not a contact on {data['trip']['id']}")
        message = {
            "card": card,
            "to": recipient,
            "body": body.strip()[:MESSAGE_MAX_CHARACTERS],
            "by": sent_by,
        }
        data["messages"].append(message)
        self._write(data)
        return message

    def told(self, card: str) -> list[str]:
        """The contacts sent the ticketed flight's number, the only message that counts as telling them."""
        ticket = self.ticket(card)
        if ticket is None:
            return []
        return sorted(
            {
                message["to"]
                for message in self.messages(card)
                if ticket["flight"] in message["body"]
            }
        )
