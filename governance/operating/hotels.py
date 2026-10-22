"""Halden Hotels' systems of record: the rate system, purchasing and the rota.

Every write carries its job's card id, and a second write for the same card hands back the first, so a
retried run changes nothing twice. `writes` lists every change in the order it landed; `make record`
checks it against the decision record, because a write the record does not hold is one no policy allowed.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

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
    a, b = datetime.fromisoformat(start), datetime.fromisoformat(end)
    return f"{a:%a %-d %b} {a:%H:%M} to {b:%H:%M}"


def town(hotel: str) -> str:
    return HOTELS[hotel]["name"].removeprefix("Halden ")


def describe(a: dict) -> str:
    """One action as the record, the board and the terminal print it."""
    if a["kind"] == "price":
        return f"{town(a['hotel'])} {day(a['night'])}: EUR {a['was']} to {a['price']}"
    if a["kind"] == "order":
        item = HOTELS[a["hotel"]]["stock"][a["item"]]["item"].split(",")[0]
        return f"{a['quantity']} {item}, {a['supplier']}, EUR {a['total']:,.0f}"
    return f"{a['staff']}, {clock(a['start'], a['end'])}"


@dataclass
class Systems:
    path: Path
    prices: dict[str, int] = field(default_factory=dict)
    orders: dict[str, dict] = field(default_factory=dict)
    shifts: dict[str, list[list[str]]] = field(default_factory=dict)
    writes: list[dict] = field(default_factory=list)

    @classmethod
    def open(cls, path: Path) -> Systems:
        return cls(path, **json.loads(path.read_text())) if path.exists() else cls(path)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows = asdict(self)
        rows.pop("path")
        self.path.write_text(json.dumps(rows, indent=1))

    def price(self, hotel: str, night: str) -> int:
        return self.prices.get(f"{hotel} {night}", HOTELS[hotel]["nights"][night]["price"])

    def staff(self, hotel: str) -> list[dict]:
        return [
            {**s, "shifts": s["shifts"] + self.shifts.get(f"{hotel} {s['name']}", [])}
            for s in HOTELS[hotel]["staff"]
        ]

    def written(self, card: str) -> dict | None:
        return next((w for w in self.writes if w["card"] == card), None)

    def apply(self, card: str, a: dict) -> tuple[str, str]:
        """Write the action; the card's first write is handed back if it already landed."""
        if held := self.written(card):
            return held["ref"], "already"
        if a["kind"] == "price":
            if a["price"] <= 0:
                raise Refused("a price must be above zero")
            self.prices[f"{a['hotel']} {a['night']}"] = a["price"]
            ref = f"Rate {day(a['night'])}"
        elif a["kind"] == "order":
            if a["supplier"] not in SUPPLIERS[a["item"]]:
                raise Refused(f"{a['supplier']} does not sell this item")
            ref = f"PO-{3001 + len(self.orders)}"
            self.orders[ref] = {"card": card, **a}
        else:
            self.shifts.setdefault(f"{a['hotel']} {a['staff']}", []).append([a["start"], a["end"]])
            ref = f"Rota {day(a['start'])}"
        self.writes.append({"card": card, "system": SYSTEM[a["kind"]], "ref": ref, "action": a})
        self.save()
        return ref, "written"


SYSTEM = {"price": "rates", "order": "purchasing", "shift": "rota"}
