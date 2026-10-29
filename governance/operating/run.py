"""The week: every job on the board worked by its AI employee, under the operations director's policy.

`make demo` starts from a fresh board and prints each verdict as it lands: allowed, refused with its
rule, waiting for a named signer, or not started. The signer, signed in on the board, answers there; the
action is written only on a yes. `make decisions` checks every write in the systems of record against the
decision record: one row that allowed or signed it, or the write had no owner. `make naive` shows the
same write with the gate off.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from agents import Model, set_tracing_disabled

from operating import policy
from operating.cost import cost
from operating.crew import handle, model
from operating.hotels import HOTELS, JOBS, ROOT, Systems, describe, town
from operating.record import Record
from operating.scripted import price_under_the_floor
from workforce.board import Board, Card, Run, read_json, usd
from workforce.mission import narrate_to_terminal, paint

STATE = ROOT / ".hotels"
NAIVE_STATE = ROOT / ".hotels-naive"
# A terminal the room can read from the back holds about 76 columns.
WIDTH = 76
TONE = {
    "allowed": "1;32",
    "signed": "1;32",
    "asked": "1;33",
    "declined": "1;33",
    "refused": "1;31",
}
WORD = {"asked": "asks", "not started": "stopped"}


class NotAllowed(Exception):
    """A decision the policy does not let this person, or this employee, make."""


def fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def say(text: str, tone: str = "0") -> None:
    print(paint(fit(text, WIDTH), tone), flush=True)


class Desk(NamedTuple):
    board: Board
    systems: Systems
    record: Record


def opened(state: Path) -> Desk:
    return Desk(
        Board.open(state / "board.json"),
        Systems.open(state / "systems.json"),
        Record(state / "record.jsonl"),
    )


def seed(board: Board) -> None:
    if not board.cards:
        board.cards = [
            Card(
                spec["id"],
                f"{HOTELS[spec['hotel']]['name']}: {spec['says']}",
                HOTELS[spec["hotel"]]["name"],
                town(spec["hotel"]),
            )
            for spec in JOBS
        ]
        board.save()


def job(card_id: str) -> dict:
    return next(spec for spec in JOBS if spec["id"] == card_id)


def spent(board: Board, name: str) -> float:
    return sum(
        run.cost for card in board.cards for run in card.runs if run.employee == name
    )


def waiting(state: Path) -> dict[str, dict]:
    return read_json(state / "waiting.json", {})


def hold(state: Path, rows: dict[str, dict]) -> None:
    (state / "waiting.json").write_text(json.dumps(rows, indent=1))


def show(row: dict) -> None:
    verdict = row["verdict"]
    say(
        f"      {WORD.get(verdict, verdict):<9} {row['action']}",
        TONE.get(verdict, "1;31"),
    )
    if verdict != "allowed":
        say(f"          {row['rule']}", "2")


async def work(
    state: Path,
    desk: Desk,
    card: Card,
    the_model: Model | str,
    narrate: Callable[[str], None] | None = None,
) -> None:
    spec = job(card.card_id)
    key, name = spec["agent"], policy.AGENTS[spec["agent"]]["name"]
    card.employee = name
    stop = policy.start(key, spent(desk.board, name))
    if stop:
        desk.record.add(
            card.card_id, name, spec["says"], "not started", stop.rule, decider="policy"
        )
    else:
        desk.board.move(card, "In flight", "run started")
        result = await handle(spec, desk.systems, desk.record, the_model, narrate)
        for who, (tokens_in, tokens_out) in result.usage.items():
            note = result.did or result.error or "The employee did not act on it"
            steps = result.steps.get(who, [])
            card.runs.append(
                Run(
                    who,
                    tokens_in,
                    tokens_out,
                    cost(tokens_in, tokens_out),
                    bool(result.did),
                    note,
                    steps,
                )
            )
    say(
        f"{card.card_id}  {name}, {card.customer}".ljust(WIDTH - 9)
        + f"{usd(card.cost):>9}",
        "1;36",
    )
    for row in desk.record.for_card(card.card_id):
        show(row)
    if stop:
        desk.board.move(card, "Needs you", stop.rule)
    elif result.waiting:
        hold(state, waiting(state) | {card.card_id: result.waiting})
        desk.board.move(
            card,
            "Needs you",
            f"waiting for {result.waiting['signer']}: {result.waiting['rule']}",
        )
    elif desk.systems.written(card.card_id):
        held = desk.systems.written(card.card_id)
        card.issue = f"{town(spec['hotel'])}, {held['ref']}"
        desk.board.move(card, "Done", f"allowed by policy: {result.did}")
    else:
        desk.board.move(
            card, "Needs you", result.error or "the employee did not act on it"
        )


async def week(
    state: Path = STATE,
    the_model: Model | str | None = None,
    narrate: Callable[[str], None] | None = None,
) -> Board:
    desk = opened(state)
    seed(desk.board)
    the_model = the_model or model()
    for card in desk.board.cards:
        if card.column == "Backlog":
            await work(state, desk, card, the_model, narrate)
    rows = desk.record.rows()
    alone = sum(row["verdict"] == "allowed" for row in rows)
    asked = len(waiting(state))
    stopped = sum(row["verdict"] == "not started" for row in rows)
    say(
        f"{alone} done alone, {asked} waiting for a signature, {stopped} not started",
        "1;36",
    )
    say(
        f"AI spend this week {usd(sum(card.cost for card in desk.board.cards))}", "1;36"
    )
    return desk.board


def sign(state: Path, card_id: str, person: str, yes: bool) -> Card:
    """The named signer's answer. The action is written only on a yes, and only that person may give one."""
    if person in policy.NAMES:
        raise NotAllowed(f"{person} is an AI employee and may not sign")
    rows = waiting(state)
    if card_id not in rows:
        raise NotAllowed("nothing is waiting for a signature on this card")
    held = rows[card_id]
    desk = opened(state)
    card = desk.board.card(card_id)
    said = describe(held["action"])
    signer = policy.first(held["signer"])
    if person != signer:
        rule = policy.signs_for(policy.key_of(held["agent"]))
        refusal = f"{person} may not sign this: only {signer} signs {rule}"
        desk.record.add(
            card_id,
            held["agent"],
            said,
            "refused",
            refusal,
            decider=person,
            signer=held["signer"],
        )
        desk.board.move(card, card.column, f"refused: {refusal}")
        raise NotAllowed(refusal)
    del rows[card_id]
    verdict = "signed" if yes else "declined"
    desk.record.add(
        card_id,
        held["agent"],
        said,
        verdict,
        held["rule"],
        decider=person,
        signer=held["signer"],
    )
    hold(state, rows)
    if yes:
        ref, _ = desk.systems.apply(card_id, held["action"])
        card.issue = f"{town(held['action']['hotel'])}, {ref}"
        desk.board.move(card, "Done", f"signed for {signer} by {person}: {said}")
    else:
        desk.board.move(card, "Done", f"declined by {person}; nothing was written")
    return card


def check(state: Path) -> list[tuple[str, bool]]:
    """Every write in the systems against the record: exactly one row allowed or signed it, and back."""
    desk = opened(state)
    owned = [
        row for row in desk.record.rows() if row["verdict"] in ("allowed", "signed")
    ]
    lines = []
    for write in desk.systems.writes:
        said = describe(write["action"])
        mine = [
            row
            for row in owned
            if row["card"] == write["card"] and row["action"] == said
        ]
        how = (
            mine[0]["verdict"]
            if len(mine) == 1
            else "NO RECORD"
            if not mine
            else "TWICE"
        )
        lines.append(
            (
                f"  {write['card']}  {write['system']:<10} {said:<40} {how}",
                len(mine) == 1,
            )
        )
    landed = {
        (write["card"], describe(write["action"])) for write in desk.systems.writes
    }
    for row in owned:
        if (row["card"], row["action"]) not in landed:
            lines.append(
                (
                    f"  {row['card']}  {'none':<10} {row['action']:<40} NOT WRITTEN",
                    False,
                )
            )
    declined = [row for row in desk.record.rows() if row["verdict"] == "declined"]
    for row in declined:
        if (row["card"], row["action"]) in landed:
            lines.append(
                (
                    f"  {row['card']}  {'none':<10} {row['action']:<40} DECLINED BUT WRITTEN",
                    False,
                )
            )
    return lines


def month(state: Path) -> list[dict]:
    """Per employee: what it did alone, what was signed, declined or refused, its AI spend and time saved."""
    desk = opened(state)
    rows = desk.record.rows()
    out = []
    for key, agent in policy.AGENTS.items():
        mine = [row for row in rows if row["agent"] == agent["name"]]
        counts = {
            kind: sum(row["verdict"] == kind for row in mine)
            for kind in ("allowed", "asked", "signed", "declined", "refused")
        }
        done = counts["allowed"] + counts["signed"]
        cash = spent(desk.board, agent["name"])
        out.append(
            {
                "key": key,
                "name": agent["name"],
                "status": agent["status"],
                "owner": agent["owner"],
                "done": done,
            }
            | counts
            | {
                "spent": cash,
                "cap": agent["cap"],
                "saved": done * agent["minutes"],
                "per": cash / done if done else None,
            }
        )
    return out


def naive() -> None:
    """The rate manager's job with the gate off: the prompt says never under the floor, nothing checks."""
    shutil.rmtree(NAIVE_STATE, ignore_errors=True)
    spec = job("J-02")
    night = HOTELS[spec["hotel"]]["nights"][spec["night"]]
    desk = opened(NAIVE_STATE)
    say("Without the gate: the limits live only in the prompt", "1;36")
    say(
        f"Rate manager asks for EUR 89 at {HOTELS[spec['hotel']]['name']} on {spec['night']}",
        "1;33",
    )
    asyncio.run(
        handle(spec, desk.systems, desk.record, price_under_the_floor(89), gated=False)
    )
    held = desk.systems.price(spec["hotel"], spec["night"])
    say(
        f"Written: the rate system holds EUR {held}; this night's floor is EUR {night['floor']}",
        "1;31",
    )
    for line, good in check(NAIVE_STATE):
        say(line, "1;32" if good else "1;31")


def decisions() -> None:
    lines = check(STATE)
    for line, good in lines:
        say(line, "1;32" if good else "1;31")
    matched = sum(good for _, good in lines)
    say(
        f"{matched} of {len(lines)} writes have exactly one decision behind them",
        "1;36",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["demo", "naive", "decisions", "serve"])
    parser.add_argument("--port", type=int, default=8096)
    args = parser.parse_args()
    if args.command == "serve":
        from operating.board import serve

        serve(STATE, args.port)
    elif args.command == "decisions":
        decisions()
    elif args.command == "naive":
        set_tracing_disabled(True)
        naive()
    else:
        set_tracing_disabled(True)
        shutil.rmtree(STATE, ignore_errors=True)
        say("KaaS Hotels: seven jobs for four AI employees", "1;36")
        asyncio.run(week(narrate=narrate_to_terminal))


if __name__ == "__main__":
    main()
