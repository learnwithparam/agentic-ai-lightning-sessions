"""The shift: every alert on the board worked by the crew, killed once on the way, and resumed.

`make shift` starts the early shift fresh and the worker is killed the moment A-04's repair order is
written, as a deploy would kill it. `make resume` starts the worker again: the card a killed run left In
flight gets that run recorded as failed, its spend read from its trace, and is worked again; the
maintenance system hands back the order already there. `make traces` checks every card's runs against
Langfuse. The board is the checkpoint: a card moves to In flight before its run starts.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import signal
from collections.abc import Callable
from pathlib import Path

from agents import Model, set_tracing_disabled

from upkeep import budget
from upkeep.cost import cost
from upkeep.crew import TRIAGE, handle, model
from upkeep.ledger import Get, Traced, langfuse, traces
from upkeep.plant import ALERTS, MACHINES, ROOT, Cmms
from workforce.board import Board, Card, Run, usd
from workforce.mission import paint

STATE = ROOT / ".upkeep"
# A terminal the room can read from the back holds about 76 columns.
WIDTH = 76
KILLED = "Killed mid-run"


def fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def say(text: str, tone: str = "0") -> None:
    print(paint(fit(text, WIDTH), tone))


def seed(board: Board) -> None:
    if not board.cards:
        board.cards = [
            Card(a["id"], f"{MACHINES[a['machine']]['name']}: {a['says']}", "Line 2", a["machine"])
            for a in ALERTS
        ]
        board.save()


def attempts(card: Card) -> int:
    return sum(line.startswith("In flight:") for line in card.log)


async def trace_of(get: Get, card: str, attempt: int, wait: float) -> Traced | None:
    """This attempt's trace, waiting a little: Langfuse takes a few seconds to ingest what was posted."""
    for _ in range(max(int(wait / 2), 1)):
        found = [t for t in traces(get, card) if t.attempt == str(attempt)]
        if found:
            return found[-1]
        await asyncio.sleep(2 if wait else 0)
    return None


async def recover(board: Board, get: Get | None, wait: float = 30) -> None:
    """A card left In flight was being worked when the worker died: that run is recorded as failed."""
    for c in board.cards:
        if c.column != "In flight":
            continue
        n = attempts(c)
        found = await trace_of(get, c.id, n, wait) if get else None
        if found and found.usage:
            for who, (i, o) in found.usage.items():
                c.runs.append(Run(who, i, o, cost(i, o), False, f"{KILLED}; spend read from its trace"))
            spent = sum(cost(i, o) for i, o in found.usage.values())
            say(f"  {c.id}  run {n} was killed; its trace in Langfuse says it spent {usd(spent)}", "1;33")
        else:
            c.runs.append(Run(TRIAGE, 0, 0, 0.0, False, f"{KILLED}; no trace, so its spend is unknown"))
            say(f"  {c.id}  run {n} was killed, and no trace says what it spent", "1;31")
        board.move(c, "Backlog", f"run {n} was killed mid-run")


def sigkill() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


async def work(board: Board, cmms: Cmms, c: Card, m: Model | str, kill: Callable[[], None] | None) -> None:
    n = attempts(c) + 1
    # The checkpoint: from here until the card moves on, a killed worker leaves it In flight.
    board.move(c, "In flight", f"run {n} started")
    alert = next(a for a in ALERTS if a["id"] == c.id)
    shift = await handle(alert, cmms, m, n, kill)
    for who, (i, o) in shift.usage.items():
        note = shift.did or shift.error or "The crew did not act on it"
        c.runs.append(Run(who, i, o, cost(i, o), bool(shift.did), note, shift.steps.get(who, [])))
    c.employee = ", ".join(dict.fromkeys(r.employee for r in c.runs if not r.note.startswith(KILLED)))
    # Done only when the maintenance system holds the outcome, whatever the crew said it did.
    held = cmms.outcome(c.id)
    if held:
        c.issue = f"{alert['machine']}, {held}" if held.startswith("WO-") else alert["machine"]
        board.move(c, "Done", shift.did)
    else:
        board.move(c, "Needs you", shift.error or "the crew did not act on it")
    spent = sum(cost(i, o) for i, o in shift.usage.values())
    tone = "1;32" if held else "1;31"
    say(
        f"  {c.id}  {MACHINES[alert['machine']]['name']:<22} {shift.did or 'no action':<22} {usd(spent)}",
        tone,
    )


def telegram() -> budget.Send:
    from workforce.telegram import Telegram

    tg = Telegram.from_env()
    if not tg:
        return lambda _: say("  No Telegram keys: the budget alert is on the board", "2")

    def send(text: str) -> None:
        tg.call("sendMessage", {"chat_id": tg.chat_id, "text": text})

    return send


async def shift(
    state: Path = STATE,
    m: Model | str | None = None,
    kill_at: str | None = None,
    kill: Callable[[], None] = sigkill,
    get: Get | None = None,
    send: budget.Send | None = None,
    wait: float = 30,
) -> Board:
    board, cmms = Board.open(state / "board.json"), Cmms.open(state / "cmms.json")
    seed(board)
    await recover(board, get, wait)
    m, send = m or model(), send or telegram()
    for c in board.cards:
        if c.column == "Done":
            continue
        await work(board, cmms, c, m, kill if c.id == kill_at else None)
        if text := budget.check(board, state, send):
            say(f"  Budget alert sent to {budget.LEAD}:", "1;35")
            for line in text.splitlines()[1:]:
                say(f"    {line}", "1;35")
    done = sum(c.column == "Done" for c in board.cards)
    say(f"{done} of {len(board.cards)} alerts handled, {len(cmms.orders)} repair orders", "1;36")
    return board


def reconcile(state: Path, get: Get) -> list[str]:
    """Each card's runs against its traces: one trace per run, and the same tokens per employee."""
    board, out = Board.open(state / "board.json"), []
    for c in board.cards:
        found = traces(get, c.id)
        mine: dict[str, list[int]] = {}
        for r in c.runs:
            tally = mine.setdefault(r.employee, [0, 0])
            tally[0], tally[1] = tally[0] + r.input_tokens, tally[1] + r.output_tokens
        theirs: dict[str, list[int]] = {}
        for t in found:
            for who, (i, o) in t.usage.items():
                tally = theirs.setdefault(who, [0, 0])
                tally[0], tally[1] = tally[0] + i, tally[1] + o
        runs = attempts(c)
        ok = len(found) == runs and {k: v for k, v in mine.items() if any(v)} == theirs
        line = f"  {c.id}  {runs} {'run' if runs == 1 else 'runs'} on the board, {len(found)} in Langfuse"
        tokens = sum(i + o for i, o in theirs.values())
        out.append(f"{line:<46} {tokens:>6,} tokens  {'match' if ok else 'MISMATCH'}")
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["shift", "resume", "traces", "serve"])
    p.add_argument("--kill-at")
    p.add_argument("--port", type=int, default=8095)
    a = p.parse_args()
    if a.command == "serve":
        from upkeep.board import serve

        serve(STATE, a.port)
        return
    get = langfuse()
    if a.command == "traces":
        if not get:
            raise SystemExit("Langfuse is not running: make -C ../mission-control stack")
        rows = reconcile(STATE, get)
        for row in rows:
            say(row, "1;32" if row.endswith("match") else "1;31")
        return
    if get:
        from upkeep.stream import install

        install(*(os.environ[k] for k in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")))
    else:
        set_tracing_disabled(True)
    if a.command == "shift":
        shutil.rmtree(STATE, ignore_errors=True)
        say("Early shift on Line 2: seven alerts for the maintenance crew", "1;36")
    else:
        say("The worker is back: picking up the shift where it stopped", "1;36")
    asyncio.run(shift(kill_at=a.kill_at, get=get))


if __name__ == "__main__":
    main()
