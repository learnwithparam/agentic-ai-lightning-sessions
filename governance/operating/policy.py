"""The gate between an AI employee and a system of record, read from the operations director's policy.

Every write an agent asks for gets one verdict: allow it, wait for the named signer, or refuse it, and
each verdict cites the rule that decided it. The rules live in fixtures/policy.toml and here, never in a
prompt: an agent does not need to know the supplier list for the gate to hold it. A paused employee, or
one over its month's AI budget, is not started at all.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import datetime

from operating.hotels import HOTELS, ROOT

POLICY = tomllib.loads((ROOT / "fixtures" / "policy.toml").read_text())
AGENTS: dict[str, dict] = POLICY["agents"]
NAMES = {employee["name"] for employee in AGENTS.values()}


@dataclass(frozen=True)
class Verdict:
    kind: str  # "allow", "sign" or "refuse"
    rule: str
    signer: str = ""


def signer(key: str, hotel: str) -> str:
    named = AGENTS[key]["signer"]
    return HOTELS[hotel]["manager"] if named == "hotel manager" else named


def first(name: str) -> str:
    return name.split(",")[0]


def key_of(name: str) -> str:
    """The policy key of the AI employee with this display name."""
    for key, employee in AGENTS.items():
        if employee["name"] == name:
            return key
    raise KeyError(name)


def signs_for(key: str) -> str:
    """What this employee's signer signs, in the words a refusal prints."""
    rule = AGENTS[key]
    if key == "rates":
        return f"a rate change over {rule['change_alone']}%"
    if key == "supplies":
        return f"an order over EUR {rule['spend_alone']}"
    return "a change to a published rota"


def start(key: str, spent: float) -> Verdict | None:
    """Whether this employee may run at all; None when it may."""
    employee = AGENTS[key]
    if employee["status"] != "running":
        why = employee["paused_because"]
        return Verdict("refuse", f"RUN-1: paused by {first(employee['owner'])}, {why}")
    if spent >= employee["cap"]:
        budget = f"${employee['cap']:.2f}"
        return Verdict(
            "refuse",
            f"RUN-2: the {employee['name']} has spent its {budget} AI budget this month",
        )
    return None


def price_verdict(key: str, action: dict) -> Verdict:
    rule = AGENTS[key]
    floor = HOTELS[action["hotel"]]["nights"][action["night"]]["floor"]
    if action["price"] < floor:
        return Verdict(
            "refuse",
            f"RATE-1: EUR {action['price']} is under this night's floor of EUR {floor}",
        )
    moved = abs(action["price"] - action["was"]) / action["was"] * 100
    if moved > rule["change_alone"]:
        who = signer(key, action["hotel"])
        why = f"RATE-2: a move over {rule['change_alone']}% is signed by {first(who)}; this is {moved:.0f}%"
        return Verdict("sign", why, who)
    return Verdict(
        "allow", f"RATE-2: a move of {moved:.0f}% is within {rule['change_alone']}%"
    )


def order_verdict(key: str, action: dict) -> Verdict:
    rule = AGENTS[key]
    if action["supplier"] not in rule["suppliers"]:
        return Verdict(
            "refuse", f"BUY-1: {action['supplier']} is not an approved supplier"
        )
    if action["total"] > rule["spend_alone"]:
        who = signer(key, action["hotel"])
        return Verdict(
            "sign",
            f"BUY-2: an order over EUR {rule['spend_alone']} is signed by {first(who)}",
            who,
        )
    return Verdict(
        "allow",
        f"BUY-2: EUR {action['total']:,.0f} is within EUR {rule['spend_alone']}",
    )


def shift_verdict(key: str, action: dict, shifts: list[list[str]]) -> Verdict:
    rule = AGENTS[key]
    start_at = datetime.fromisoformat(action["start"])
    end_at = datetime.fromisoformat(action["end"])
    for shift_start, shift_end in shifts:
        before = (start_at - datetime.fromisoformat(shift_end)).total_seconds() / 3600
        after = (datetime.fromisoformat(shift_start) - end_at).total_seconds() / 3600
        rest = max(before, after)
        if rest < rule["rest_hours"]:
            hours = f"{max(rest, 0):.0f} h; the rule is {rule['rest_hours']} h"
            return Verdict(
                "refuse", f"ROTA-1: {first(action['staff'])} would rest {hours}"
            )
    who = signer(key, action["hotel"])
    return Verdict(
        "sign", f"ROTA-2: a change to a published rota is signed by {first(who)}", who
    )


def check(key: str, action: dict, shifts: list[list[str]] = ()) -> Verdict:
    """The verdict on one action; `shifts` are the staff member's shifts already on the rota."""
    if action["kind"] == "price":
        return price_verdict(key, action)
    if action["kind"] == "order":
        return order_verdict(key, action)
    return shift_verdict(key, action, shifts)


def alone(key: str) -> str:
    """What the employee may do without a signature, in the words the Team page prints."""
    employee = AGENTS[key]
    if employee["status"] != "running":
        return f"Nothing while paused: {employee['paused_because']}"
    if key == "rates":
        return f"Move a night's price up to {employee['change_alone']}%, never under the hotel's floor"
    if key == "supplies":
        return f"Order up to EUR {employee['spend_alone']} from the {len(employee['suppliers'])} approved suppliers"
    return f"Nothing: every rota change is signed, and {employee['rest_hours']} h rest between shifts always holds"
