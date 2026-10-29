"""KaaS Hotels' systems of record: the rate system, purchasing and the rota.

Every write carries its job's card id, and a second write for the same card hands back the first, so a
retried run changes nothing twice. `writes` lists every change in the order it landed; `make decisions`
checks it against the decision record, because a write the record does not hold is one no policy allowed.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from workforce.board import is_blank

ROOT = Path(__file__).resolve().parent.parent
_FIX = tomllib.loads((ROOT / "fixtures" / "hotels.toml").read_text())
HOTELS: dict[str, dict] = _FIX["hotels"]
SUPPLIERS: dict[str, dict[str, float]] = _FIX["suppliers"]
JOBS: list[dict] = tomllib.loads((ROOT / "fixtures" / "jobs.toml").read_text())["job"]


class Refused(Exception):
    """The system of record will not take this write, whatever the policy said."""


def day(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%a %-d %b")


def clock(start: str, end: str) -> str:
    begin, finish = datetime.fromisoformat(start), datetime.fromisoformat(end)
    return f"{begin:%a %-d %b} {begin:%H:%M} to {finish:%H:%M}"


def town(hotel: str) -> str:
    return HOTELS[hotel]["name"].replace("KaaS ", "")


def describe(action: dict) -> str:
    """One action as the record, the board and the terminal print it."""
    if action["kind"] == "price":
        return f"{town(action['hotel'])} {day(action['night'])}: EUR {action['was']} to {action['price']}"
    if action["kind"] == "order":
        item = HOTELS[action["hotel"]]["stock"][action["item"]]["item"].split(",")[0]
        return f"{action['quantity']} {item}, {action['supplier']}, EUR {action['total']:,.0f}"
    return f"{action['staff']}, {clock(action['start'], action['end'])}"


@dataclass
class Systems:
    path: Path
    prices: dict[str, int] = field(default_factory=dict)
    orders: dict[str, dict] = field(default_factory=dict)
    shifts: dict[str, list[list[str]]] = field(default_factory=dict)
    writes: list[dict] = field(default_factory=list)

    @classmethod
    def open(cls, path: Path) -> Systems:
        return (
            cls(path) if is_blank(path) else cls(path, **json.loads(path.read_text()))
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows = asdict(self)
        rows.pop("path")
        self.path.write_text(json.dumps(rows, indent=1))

    def price(self, hotel: str, night: str) -> int:
        return self.prices.get(
            f"{hotel} {night}", HOTELS[hotel]["nights"][night]["price"]
        )

    def staff(self, hotel: str) -> list[dict]:
        return [
            {
                **member,
                "shifts": member["shifts"]
                + self.shifts.get(f"{hotel} {member['name']}", []),
            }
            for member in HOTELS[hotel]["staff"]
        ]

    def written(self, card: str) -> dict | None:
        return next((write for write in self.writes if write["card"] == card), None)

    def apply(self, card: str, action: dict) -> tuple[str, str]:
        """Write the action; the card's first write is handed back if it already landed."""
        held = self.written(card)
        if held:
            return held["ref"], "already"
        if action["kind"] == "price":
            if action["price"] <= 0:
                raise Refused("a price must be above zero")
            self.prices[f"{action['hotel']} {action['night']}"] = action["price"]
            ref = f"Rate {day(action['night'])}"
        elif action["kind"] == "order":
            if action["supplier"] not in SUPPLIERS[action["item"]]:
                raise Refused(f"{action['supplier']} does not sell this item")
            ref = f"PO-{3001 + len(self.orders)}"
            self.orders[ref] = {"card": card, **action}
        else:
            self.shifts.setdefault(f"{action['hotel']} {action['staff']}", []).append(
                [action["start"], action["end"]]
            )
            ref = f"Rota {day(action['start'])}"
        self.writes.append(
            {
                "card": card,
                "system": SYSTEM[action["kind"]],
                "ref": ref,
                "action": action,
            }
        )
        self.save()
        return ref, "written"


SYSTEM = {"price": "rates", "order": "purchasing", "shift": "rota"}
