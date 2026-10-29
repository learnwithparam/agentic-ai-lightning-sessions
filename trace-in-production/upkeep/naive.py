"""The shift without the fixes, offline: traces posted only at the end, and a blind retry.

A deploy kills the worker right after A-04's repair order is written. Nothing was posted for that run,
so its spend is unknown, and the retry books the compressor again because the order has no key.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from agents import set_tracing_disabled

from upkeep.crew import handle
from upkeep.plant import ALERTS, FIRST_ORDER, PROCEDURES, Cmms, Order
from upkeep.run import say
from upkeep.scripted import crew_model

KILLED_ALERT = "A-04"
NUMBER_WORDS = {5: "five", 6: "six", 7: "seven"}


class Killed(BaseException):
    """What a SIGKILL is to a run: nothing after it runs, so nothing catches it."""


class PlainOrders(Cmms):
    """A maintenance system with no idempotency key: every booking is a new order."""

    def book(self, alert: str, procedure: str, priority: str) -> tuple[Order, str]:
        machine = PROCEDURES[procedure]["machine"]
        order = Order(f"WO-{FIRST_ORDER + len(self.orders)}", machine, procedure, priority, [alert])
        self.orders[order.order_id] = order
        self.save()
        return order, "booked"


def die() -> None:
    raise Killed


async def shift(state: Path) -> PlainOrders:
    cmms = PlainOrders(state / "cmms.json")
    crew = crew_model()
    for alert in ALERTS:
        if alert["id"] != KILLED_ALERT:
            await handle(alert, cmms, crew, 1)
            continue
        try:
            await handle(alert, cmms, crew, 1, die)
        except Killed:
            booked = cmms.covering(KILLED_ALERT).order_id
            say(f"{KILLED_ALERT}  run 1 booked {booked}, then the worker was killed", "1;33")
            say(f"{KILLED_ALERT}  no trace was posted: the run's spend is unknown", "1;31")
        await handle(alert, cmms, crew, 2)
        say(f"{KILLED_ALERT}  retried blindly: booked {list(cmms.orders)[-1]}", "1;31")
    return cmms


def main() -> None:
    set_tracing_disabled(True)
    with tempfile.TemporaryDirectory() as folder:
        cmms = asyncio.run(shift(Path(folder)))
    orders = NUMBER_WORDS[len(cmms.orders)]
    say(f"Seven alerts, {orders} orders: the compressor is booked twice", "1;31")


if __name__ == "__main__":
    main()
