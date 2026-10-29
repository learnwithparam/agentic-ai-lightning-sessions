"""The shift: every alert on the board worked by the crew, killed once on the way, and resumed.

`make shift` starts the early shift fresh and the worker is killed the moment A-04's repair order is
written, as a deploy would kill it. `make resume` starts the worker again: the card a killed run left In
flight gets that run recorded as failed, its spend read from its trace, and is worked again; the
maintenance system hands back the order already there. A shift that passes its budget stops and waits on
the board for the maintenance lead. `make traces` checks every card's runs against Langfuse. The board is
the checkpoint: a card moves to In flight before its run starts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from agents import Model, set_tracing_disabled

from upkeep import budget
from upkeep.cost import cost
from upkeep.crew import TRIAGE, handle, model
from upkeep.ledger import Get, Traced, langfuse, traces
from upkeep.plant import ALERTS, MACHINES, ROOT, Cmms, Refused
from workforce.board import Board, Card, Run, is_blank, usd
from workforce.mission import paint

STATE = ROOT / ".upkeep"
PORT = 8095
# A terminal the room can read from the back holds about 76 columns.
WIDTH = 76
KILLED = "Killed mid-run"
PAUSED = "pause.json"
PAUSE_CARD = "P-1"
ALERT_IDS = [alert["id"] for alert in ALERTS]
LANGFUSE_KEYS = ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")


def fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def say(text: str, tone: str = "0") -> None:
    # Flushed per line: a SIGKILL takes whatever is still buffered with it.
    print(paint(fit(text, WIDTH), tone), flush=True)


def seed(board: Board) -> None:
    if not board.cards:
        board.cards = [
            Card(
                alert["id"],
                f"{MACHINES[alert['machine']]['name']}: {alert['says']}",
                "Line 2",
                alert["machine"],
            )
            for alert in ALERTS
        ]
        board.save()


def began(state: Path) -> str | None:
    """When this shift's board was seeded: Langfuse keeps every earlier shift's traces too."""
    target = state / "shift.json"
    return None if is_blank(target) else json.loads(target.read_text())["started"]


def attempts(card: Card) -> int:
    return sum(line.startswith("In flight:") for line in card.log)


async def trace_of(get: Get, card: str, attempt: int, wait: float, since: str | None) -> Traced | None:
    """This attempt's trace, waiting a little: Langfuse takes a few seconds to ingest what was posted."""
    for _ in range(max(int(wait / 2), 1)):
        found = [trace for trace in traces(get, card, since) if trace.attempt == str(attempt)]
        if found:
            return found[-1]
        await asyncio.sleep(2 if wait else 0)
    return None


async def recover(board: Board, get: Get | None, since: str | None, wait: float = 30) -> None:
    """A card left In flight was being worked when the worker died: that run is recorded as failed."""
    for card in board.cards:
        if card.column != "In flight":
            continue
        number = attempts(card)
        found = await trace_of(get, card.card_id, number, wait, since) if get else None
        if found and found.usage:
            for who, (tokens_in, tokens_out) in found.usage.items():
                note = f"{KILLED}; spend read from its trace"
                card.runs.append(
                    Run(who, tokens_in, tokens_out, cost(tokens_in, tokens_out), False, note, killed=True)
                )
            spent = sum(cost(*tokens) for tokens in found.usage.values())
            say(
                f"  {card.card_id}  run {number} was killed; "
                f"its trace in Langfuse says it spent {usd(spent)}",
                "1;33",
            )
        else:
            note = f"{KILLED}; no trace, so its spend is unknown"
            card.runs.append(Run(TRIAGE, 0, 0, 0.0, False, note, killed=True))
            say(f"  {card.card_id}  run {number} was killed, and no trace says what it spent", "1;31")
        board.move(card, "Backlog", f"run {number} was killed mid-run")


def sigkill() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


async def work(
    board: Board, cmms: Cmms, card: Card, crew: Model | str, kill: Callable[[], None] | None
) -> None:
    number = attempts(card) + 1
    # The checkpoint: from here until the card moves on, a killed worker leaves it In flight.
    board.move(card, "In flight", f"run {number} started")
    alert = next(row for row in ALERTS if row["id"] == card.card_id)
    result = await handle(alert, cmms, crew, number, kill)
    note = result.did or result.error or "The crew did not act on it"
    for who, (tokens_in, tokens_out) in result.usage.items():
        steps = result.steps.get(who, [])
        card.runs.append(
            Run(who, tokens_in, tokens_out, cost(tokens_in, tokens_out), bool(result.did), note, steps)
        )
    card.employee = ", ".join(dict.fromkeys(run.employee for run in card.runs if not run.killed))
    # Done only when the maintenance system holds the outcome, whatever the crew said it did.
    held = cmms.outcome(card.card_id)
    if held:
        card.issue = f"{alert['machine']}, {held.order_id}" if held.order_id else alert["machine"]
        board.move(card, "Done", result.did)
    else:
        board.move(card, "Needs you", result.error or "the crew did not act on it")
    spent = sum(cost(*tokens) for tokens in result.usage.values())
    name = MACHINES[alert["machine"]]["name"]
    say(
        f"  {card.card_id}  {name:<22} {result.did or 'no action':<22} {usd(spent)}",
        "1;32" if held else "1;31",
    )


def pause(board: Board, state: Path) -> None:
    """Over budget: the shift stops and a card asks the maintenance lead, on the board, signed in."""
    remaining = [card.card_id for card in board.cards if card.card_id in ALERT_IDS and card.column != "Done"]
    summary = {
        "shift": began(state),
        "spent": budget.spent(board),
        "budget": budget.budget_usd(),
        "remaining": remaining,
    }
    (state / PAUSED).write_text(json.dumps(summary))
    card = Card(PAUSE_CARD, "Continue the shift past its budget?", "KaaS Dairy", "shift")
    board.cards.append(card)
    board.move(card, "Needs you", budget.question(board))
    say(
        f"Paused: the crew has spent {usd(budget.spent(board))} of its {usd(budget.budget_usd())} budget",
        "1;35",
    )
    say(f"{budget.LEAD} decides on the board: http://127.0.0.1:{PORT}/cards/{PAUSE_CARD}", "1;35")


async def shift(
    state: Path = STATE,
    crew: Model | str | None = None,
    kill_at: str | None = None,
    kill: Callable[[], None] = sigkill,
    get: Get | None = None,
    wait: float = 30,
    limit: bool = True,
) -> Board:
    board, cmms = Board.open(state / "board.json"), Cmms.open(state / "cmms.json")
    if not board.cards:
        state.mkdir(parents=True, exist_ok=True)
        (state / "shift.json").write_text(json.dumps({"started": datetime.now(UTC).isoformat()}))
    seed(board)
    await recover(board, get, began(state), wait)
    crew = crew or model()
    worked = 0
    for card in board.cards:
        if card.card_id not in ALERT_IDS or card.column == "Done":
            continue
        if limit and worked and budget.over(board):
            pause(board, state)
            return board
        await work(board, cmms, card, crew, kill if card.card_id == kill_at else None)
        worked += 1
    alerts = [card for card in board.cards if card.card_id in ALERT_IDS]
    done = sum(card.column == "Done" for card in alerts)
    say(f"{done} of {len(alerts)} alerts handled, {len(cmms.orders)} repair orders", "1;36")
    return board


def allow_continue(state: Path, person: str) -> None:
    """Only the maintenance lead raises the shift's budget; every refusal is logged before it is raised."""
    if is_blank(state / PAUSED):
        said = "No shift is paused"
    elif person != budget.LEAD:
        said = f"{person} may not continue the shift; only {budget.LEAD} can raise the shift budget"
    else:
        return
    budget.record(state, person, "refused", said)
    raise Refused(said)


async def continue_shift(
    state: Path, approved_by: str, crew: Model | str | None = None, get: Get | None = None, wait: float = 30
) -> Board:
    """Ingrid's approval: the pause card is Done and only the alerts the pause left run, with no kill."""
    allow_continue(state, approved_by)
    board = Board.open(state / "board.json")
    (state / PAUSED).unlink()
    budget.record(state, approved_by, "approved", "Continued past budget")
    board.move(board.card(PAUSE_CARD), "Done", f"Continued past budget; approved by {approved_by}")
    return await shift(state, crew, None, get=get, wait=wait, limit=False)


@dataclass
class Reconciled:
    card: str
    runs: int
    found: int
    tokens: int
    matches: bool

    def line(self) -> str:
        noun = "run" if self.runs == 1 else "runs"
        text = f"  {self.card}  {self.runs} {noun} on the board, {self.found} in Langfuse"
        return f"{text:<46} {self.tokens:>6,} tokens  {'match' if self.matches else 'MISMATCH'}"


def add_tokens(tally: dict[str, list[int]], who: str, tokens_in: int, tokens_out: int) -> None:
    row = tally.setdefault(who, [0, 0])
    row[0] += tokens_in
    row[1] += tokens_out


def reconcile(state: Path, get: Get) -> list[Reconciled]:
    """Each card's runs against its traces: one trace per run, and the same tokens per employee."""
    board, rows = Board.open(state / "board.json"), []
    for card in board.cards:
        if card.card_id not in ALERT_IDS:
            continue
        found = traces(get, card.card_id, began(state))
        mine: dict[str, list[int]] = {}
        for run in card.runs:
            add_tokens(mine, run.employee, run.input_tokens, run.output_tokens)
        theirs: dict[str, list[int]] = {}
        for trace in found:
            for who, (tokens_in, tokens_out) in trace.usage.items():
                add_tokens(theirs, who, tokens_in, tokens_out)
        runs = attempts(card)
        same = {who: tally for who, tally in mine.items() if any(tally)} == theirs
        tokens = sum(tokens_in + tokens_out for tokens_in, tokens_out in theirs.values())
        rows.append(Reconciled(card.card_id, runs, len(found), tokens, len(found) == runs and same))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["shift", "resume", "traces", "serve"])
    parser.add_argument("--kill-at", default=os.environ.get("KILL_AT"))
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    get = langfuse()
    if args.command == "traces":
        if not get:
            raise SystemExit("Langfuse is not running: make -C ../mission-control stack")
        for row in reconcile(STATE, get):
            say(row.line(), "1;32" if row.matches else "1;31")
        return
    if get:
        from upkeep.stream import install

        install(*(os.environ[name] for name in LANGFUSE_KEYS))
    else:
        set_tracing_disabled(True)
    if args.command == "serve":
        from upkeep.board import serve

        serve(STATE, args.port)
        return
    if args.command == "shift":
        shutil.rmtree(STATE, ignore_errors=True)
        say("Early shift on Line 2: seven alerts for the maintenance crew", "1;36")
    else:
        say("The worker is back: picking up the shift where it stopped", "1;36")
    kill_at = args.kill_at if args.command == "shift" and args.kill_at not in (None, "", "none") else None
    asyncio.run(shift(kill_at=kill_at, get=get))


if __name__ == "__main__":
    main()
