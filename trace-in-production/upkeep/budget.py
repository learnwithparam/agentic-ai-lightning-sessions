"""The shift's budget: once the crew's spend passes it, one alert to the maintenance lead on the board.

Spend is every run on the board, killed runs included, priced from their traces. The alert is recorded
only after it is sent, so a worker killed in between sends it again on resume: twice is a nuisance,
never is a budget nobody watched.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from workforce.board import Board, read_json, usd

BUDGET = 0.001
LEAD = "Ingrid Halvorsen"

Send = Callable[[str], None]


def by_employee(board: Board) -> dict[str, list]:
    """Each employee's alerts worked, killed runs and spend, from the board's runs."""
    out: dict[str, list] = {}
    for c in board.cards:
        for who in {r.employee for r in c.runs}:
            out.setdefault(who, [0, 0, 0.0])[0] += 1
        for r in c.runs:
            row = out[r.employee]
            row[1] += r.note.startswith("Killed")
            row[2] += r.cost
    return dict(sorted(out.items(), key=lambda kv: -kv[1][2]))


def message(board: Board, spent: float) -> str:
    done = sum(c.column == "Done" for c in board.cards)
    lines = [
        f"Brekke Dairy Line 2: the crew has spent {usd(spent)} this shift, over its {usd(BUDGET)} budget."
    ]
    lines += [f"{who}: {usd(row[2])} on {row[0]} alerts" for who, row in by_employee(board).items()]
    lines.append(f"{done} of {len(board.cards)} alerts handled.")
    return "\n".join(lines)


def check(board: Board, state: Path, send: Send) -> str | None:
    """The alert's text when this call sent it, else None: under budget, or already sent this shift."""
    f = state / "alerts.json"
    sent = read_json(f, {})
    spent = sum(c.cost for c in board.cards)
    if spent <= BUDGET or "budget" in sent:
        return None
    text = message(board, spent)
    send(text)
    sent["budget"] = {"spent": spent, "text": text}
    f.write_text(json.dumps(sent))
    return text


def sent(state: Path) -> dict | None:
    f = state / "alerts.json"
    return read_json(f, {}).get("budget")
