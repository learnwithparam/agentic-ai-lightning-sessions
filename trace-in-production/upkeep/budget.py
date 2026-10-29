"""The shift's budget: once the crew's spend passes it, the shift pauses and the maintenance lead decides.

Spend is every run on the board, killed runs included, priced from their traces. The pause is a card on
the board, so the person who owns the budget answers there, signed in, and the shift carries on only
when they do.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from workforce.board import Board, usd

LEAD = "Ingrid Halvorsen"
DEFAULT_BUDGET = 0.01


def budget_usd() -> float:
    return float(os.environ.get("BUDGET_USD") or DEFAULT_BUDGET)


def spent(board: Board) -> float:
    return sum(card.cost for card in board.cards)


def by_employee(board: Board) -> dict[str, list]:
    """Each employee's alerts worked, killed runs and spend, from the board's runs."""
    rows: dict[str, list] = {}
    for card in board.cards:
        for employee in {run.employee for run in card.runs}:
            rows.setdefault(employee, [0, 0, 0.0])[0] += 1
        for run in card.runs:
            row = rows[run.employee]
            row[1] += run.killed
            row[2] += run.cost
    return dict(sorted(rows.items(), key=lambda item: -item[1][2]))


def over(board: Board) -> bool:
    return spent(board) > budget_usd()


def question(board: Board) -> str:
    return (
        f"The crew has spent {usd(spent(board))} of its {usd(budget_usd())} shift budget. "
        f"{LEAD} decides whether the shift goes on."
    )


def record(state: Path, person: str, decision: str, said: str) -> None:
    """One line per attempt on the pause card, refusals included: the board shows them as the call log."""
    row = {"by": person, "decision": decision, "said": said, "at": datetime.now(UTC).strftime("%H:%M")}
    with (state / "decisions.jsonl").open("a") as handle:
        handle.write(json.dumps(row) + "\n")


def decisions(state: Path) -> list[dict]:
    target = state / "decisions.jsonl"
    return [json.loads(line) for line in target.read_text().splitlines()] if target.exists() else []
