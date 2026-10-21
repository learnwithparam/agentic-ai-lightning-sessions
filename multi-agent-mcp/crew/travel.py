"""Acme's travel system: the trip, the flights on sale, the tickets and the messages sent. The truth a card
settles on.

The travel rules live here, so no prompt can move them. A ticket is only for the trip's route and day, on
a flight with a seat that lands at least 90 minutes before the meeting. A fare over the trip's cap waits
for the traveller's manager, and no AI employee can approve it. A message goes only to a contact on the
trip. A flight is booked only when this system says so.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
TO_THE_MEETING = 90  # minutes from landing at BER to a desk in Berlin Mitte


class Refused(Exception):
    pass


def minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


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
        trip, data = self.trip(), self._read()
        return [
            f
            for f in data["flights"]
            if (f["from"], f["to"], f["date"]) == (trip["from"], trip["to"], trip["date"])
        ]

    def ticket(self, card: str) -> dict | None:
        live = [t for t in self._read()["tickets"] if t["card"] == card and t["status"] != "declined"]
        return live[-1] if live else None

    def messages(self, card: str) -> list[dict]:
        return [m for m in self._read()["messages"] if m["card"] == card]

    def book(self, card: str, flight_id: str, by: str) -> dict:
        data = self._read()
        trip = data["trip"]
        flight = next((f for f in self.flights() if f["id"] == flight_id), None)
        if flight is None:
            raise Refused(
                f"{flight_id} is not a flight from {trip['from']} to {trip['to']} on {trip['date']}"
            )
        if flight["seats"] < 1:
            raise Refused(f"{flight_id} is full")
        if minutes(flight["lands"]) + TO_THE_MEETING > minutes(trip["meeting"]):
            raise Refused(f"{flight_id} lands {flight['lands']}, too late for the {trip['meeting']} meeting")
        if self.ticket(card):
            raise Refused(f"{card} already has a ticket")
        held = flight["fare"] > trip["fare_cap"]
        ticket = {
            "id": f"TK-{len(data['tickets']) + 1:03d}",
            "card": card,
            "flight": flight_id,
            "departs": flight["departs"],
            "lands": flight["lands"],
            "cabin": flight["cabin"],
            "fare": flight["fare"],
            "booked_by": by,
            "status": f"waiting for {trip['manager']}" if held else "ticketed",
            "approved_by": None,
        }
        next(f for f in data["flights"] if f["id"] == flight_id)["seats"] -= 1
        data["tickets"].append(ticket)
        self._write(data)
        return ticket

    def approve(self, ticket_id: str, by: str, yes: bool, agents: frozenset[str]) -> dict:
        """Only the traveller's manager, a human, settles a fare over the cap."""
        data = self._read()
        ticket = next((t for t in data["tickets"] if t["id"] == ticket_id), None)
        if ticket is None:
            raise Refused(f"there is no ticket {ticket_id}")
        if not ticket["status"].startswith("waiting"):
            raise Refused(f"{ticket_id} is already {ticket['status']}")
        if by in agents or by != data["trip"]["manager"]:
            raise Refused(f"{by} may not approve a fare over the cap; only {data['trip']['manager']} can")
        ticket["status"], ticket["approved_by"] = ("ticketed" if yes else "declined"), by
        self._write(data)
        return ticket

    def send(self, card: str, to: str, body: str, by: str) -> dict:
        data = self._read()
        if to not in data["trip"]["contacts"]:
            raise Refused(f"{to} is not a contact on {data['trip']['id']}")
        message = {"card": card, "to": to, "body": body.strip()[:500], "by": by}
        data["messages"].append(message)
        self._write(data)
        return message

    def told(self, card: str) -> list[str]:
        """The contacts sent the ticketed flight's number, the only message that counts as telling them."""
        ticket = self.ticket(card)
        if ticket is None:
            return []
        return sorted({m["to"] for m in self.messages(card) if ticket["flight"] in m["body"]})
