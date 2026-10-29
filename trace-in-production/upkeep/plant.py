"""KaaS Dairy's Line 2 and its maintenance system, the truth outside the model.

A repair order dispatches a technician and reserves a part, so it must exist once whatever the crew
does. The alert id is the order's idempotency key: booking an alert twice, or a run killed after the
order was written and run again, returns the order already there. A second alert for a fault with an
open order joins that order instead of opening another. The file is replaced whole on every write, so a
kill mid-write leaves the last complete version.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from workforce.board import is_blank

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
PLANT = tomllib.loads((FIXTURES / "plant.toml").read_text())
MACHINES = {machine["id"]: machine for machine in PLANT["machine"]}
PROCEDURES = {procedure["id"]: procedure for procedure in PLANT["procedure"]}
ALERTS = tomllib.loads((FIXTURES / "alerts.toml").read_text())["alert"]
PRIORITIES = ("critical", "high", "medium")
FIRST_ORDER = 2001


class Refused(ValueError):
    """The maintenance system would not take this write; the message says why, for the agent to fix."""


@dataclass(frozen=True)
class Outcome:
    """What the maintenance system holds for an alert: an order id, or a dismissal."""

    kind: str
    order_id: str = ""

    def __str__(self) -> str:
        return self.order_id or self.kind


@dataclass
class Order:
    order_id: str
    machine: str
    procedure: str
    priority: str
    alerts: list[str] = field(default_factory=list)


@dataclass
class Cmms:
    path: Path
    orders: dict[str, Order] = field(default_factory=dict)
    dismissed: dict[str, str] = field(default_factory=dict)

    @classmethod
    def open(cls, path: Path) -> Cmms:
        if is_blank(path):
            return cls(path)
        raw = json.loads(path.read_text())
        orders = {row["order_id"]: Order(**row) for row in raw["orders"]}
        return cls(path, orders, raw["dismissed"])

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"orders": [asdict(order) for order in self.orders.values()], "dismissed": self.dismissed}
            )
        )
        os.replace(tmp, self.path)

    def open_on(self, machine: str) -> list[Order]:
        return [order for order in self.orders.values() if order.machine == machine]

    def book(self, alert: str, procedure: str, priority: str) -> tuple[Order, str]:
        """The order for this alert, and what the call did: booked it, joined an open one, or found it."""
        manual = PROCEDURES.get(procedure)
        if not manual:
            raise Refused(f"No procedure {procedure} in the maintenance manual.")
        if priority not in PRIORITIES:
            raise Refused(f"Priority must be one of {', '.join(PRIORITIES)}.")
        found = self.covering(alert)
        if found is not None:
            return found, "already booked"
        same = next(
            (order for order in self.open_on(manual["machine"]) if order.procedure == procedure), None
        )
        if same:
            same.alerts.append(alert)
            self.save()
            return same, "joined"
        order = Order(f"WO-{FIRST_ORDER + len(self.orders)}", manual["machine"], procedure, priority, [alert])
        self.orders[order.order_id] = order
        self.save()
        return order, "booked"

    def attach(self, alert: str, order_id: str) -> Order:
        order = self.orders.get(order_id)
        if not order:
            raise Refused(f"No open order {order_id}.")
        if order.machine != machine_of(alert):
            raise Refused(f"{order_id} is for {MACHINES[order.machine]['name']}, not this machine.")
        if alert not in order.alerts:
            order.alerts.append(alert)
            self.save()
        return order

    def dismiss(self, alert: str, reason: str) -> None:
        self.dismissed[alert] = reason
        self.save()

    def covering(self, alert: str) -> Order | None:
        return next((order for order in self.orders.values() if alert in order.alerts), None)

    def outcome(self, alert: str) -> Outcome | None:
        """What the maintenance system holds for this alert, or None: the only thing that closes a card."""
        order = self.covering(alert)
        if order:
            return Outcome("order", order.order_id)
        return Outcome("dismissed") if alert in self.dismissed else None


def machine_of(alert: str) -> str:
    return next(row["machine"] for row in ALERTS if row["id"] == alert)
