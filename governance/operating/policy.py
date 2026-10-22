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
NAMES = {a["name"] for a in AGENTS.values()}


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


def start(key: str, spent: float) -> Verdict | None:
    """Whether this employee may run at all; None when it may."""
    a = AGENTS[key]
    if a["status"] != "running":
        return Verdict("refuse", f"RUN-1: paused by {first(a['owner'])}, {a['paused_because']}")
    if spent >= a["cap"]:
        return Verdict("refuse", f"RUN-2: the {a['name']} has spent its ${a['cap']:.2f} AI budget this month")
    return None


def check(key: str, a: dict, shifts: list[list[str]] = ()) -> Verdict:
    """The verdict on one action; `shifts` are the staff member's shifts already on the rota."""
    rule = AGENTS[key]
    if a["kind"] == "price":
        floor = HOTELS[a["hotel"]]["nights"][a["night"]]["floor"]
        if a["price"] < floor:
            return Verdict("refuse", f"RATE-1: EUR {a['price']} is under this night's floor of EUR {floor}")
        moved = abs(a["price"] - a["was"]) / a["was"] * 100
        if moved > rule["change_alone"]:
            who = signer(key, a["hotel"])
            why = (
                f"RATE-2: a move over {rule['change_alone']}% is signed by {first(who)}; this is {moved:.0f}%"
            )
            return Verdict("sign", why, who)
        return Verdict("allow", f"RATE-2: a move of {moved:.0f}% is within {rule['change_alone']}%")
    if a["kind"] == "order":
        if a["supplier"] not in rule["suppliers"]:
            return Verdict("refuse", f"BUY-1: {a['supplier']} is not an approved supplier")
        if a["total"] > rule["spend_alone"]:
            who = signer(key, a["hotel"])
            why = f"BUY-2: an order over EUR {rule['spend_alone']} is signed by {first(who)}"
            return Verdict("sign", why, who)
        return Verdict("allow", f"BUY-2: EUR {a['total']:,.0f} is within EUR {rule['spend_alone']}")
    start_at, end_at = datetime.fromisoformat(a["start"]), datetime.fromisoformat(a["end"])
    for s, e in shifts:
        before = (start_at - datetime.fromisoformat(e)).total_seconds() / 3600
        after = (datetime.fromisoformat(s) - end_at).total_seconds() / 3600
        rest = max(before, after)
        if rest < rule["rest_hours"]:
            hours = f"{max(rest, 0):.0f} h; the rule is {rule['rest_hours']} h"
            why = f"ROTA-1: {first(a['staff'])} would rest {hours}"
            return Verdict("refuse", why)
    who = signer(key, a["hotel"])
    return Verdict("sign", f"ROTA-2: a change to a published rota is signed by {first(who)}", who)


def alone(key: str) -> str:
    """What the employee may do without a signature, in the words the Team page prints."""
    a = AGENTS[key]
    if a["status"] != "running":
        return f"Nothing while paused: {a['paused_because']}"
    if key == "rates":
        return f"Move a night's price up to {a['change_alone']}%, never under the hotel's floor"
    if key == "supplies":
        return f"Order up to EUR {a['spend_alone']} from the {len(a['suppliers'])} approved suppliers"
    return f"Nothing: every rota change is signed, and {a['rest_hours']} h rest between shifts always holds"
