"""The week: every job on the board worked by its AI employee, under the operations director's policy.

`make week` starts from a fresh board and prints each verdict as it lands: allowed, refused with its
rule, waiting for a named signer, or not started. The signer answers on the board, which writes the
action only on a yes. `make record` checks every write in the systems of record against the decision
record: one row that allowed or signed it, or the write had no owner.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from pathlib import Path

from agents import Model, set_tracing_disabled

from operating import policy
from operating.cost import cost
from operating.crew import handle, model
from operating.hotels import HOTELS, JOBS, ROOT, Systems, describe, town
from operating.record import Record
from workforce.board import Board, Card, Run, usd
from workforce.mission import paint

STATE = ROOT / ".halden"
# A terminal the room can read from the back holds about 76 columns.
WIDTH = 76
TONE = {"allowed": "1;32", "signed": "1;32", "asked": "1;33", "declined": "1;33", "refused": "1;31"}
WORD = {"asked": "asks", "not started": "stopped"}


class NotAllowed(Exception):
    """A decision the policy does not let this person, or this employee, make."""


def fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def say(text: str, tone: str = "0") -> None:
    print(paint(fit(text, WIDTH), tone), flush=True)


def opened(state: Path) -> tuple[Board, Systems, Record]:
    return (
        Board.open(state / "board.json"),
        Systems.open(state / "systems.json"),
        Record(state / "record.jsonl"),
    )


def seed(board: Board) -> None:
    if not board.cards:
        board.cards = [
            Card(
                j["id"],
                f"{HOTELS[j['hotel']]['name']}: {j['says']}",
                HOTELS[j["hotel"]]["name"],
                town(j["hotel"]),
            )
            for j in JOBS
        ]
        board.save()


def job(card_id: str) -> dict:
    return next(j for j in JOBS if j["id"] == card_id)


def spent(board: Board, name: str) -> float:
    return sum(r.cost for c in board.cards for r in c.runs if r.employee == name)


def waiting(state: Path) -> dict[str, dict]:
    f = state / "waiting.json"
    return json.loads(f.read_text()) if f.exists() else {}


def hold(state: Path, rows: dict[str, dict]) -> None:
    (state / "waiting.json").write_text(json.dumps(rows, indent=1))


def show(row: dict) -> None:
    v = row["verdict"]
    say(f"      {WORD.get(v, v):<9} {row['action']}", TONE.get(v, "1;31"))
    if v != "allowed":
        say(f"          {row['rule']}", "2")


async def work(state: Path, board: Board, systems: Systems, record: Record, c: Card, m: Model | str) -> None:
    j = job(c.id)
    key, name = j["agent"], policy.AGENTS[j["agent"]]["name"]
    c.employee = name
    stop = policy.start(key, spent(board, name))
    if stop:
        record.add(c.id, name, j["says"], "not started", stop.rule, by="policy")
    else:
        board.move(c, "In flight", "run started")
        w = await handle(j, systems, record, m)
        for who, (i, o) in w.usage.items():
            note = w.did or w.error or "The employee did not act on it"
            c.runs.append(Run(who, i, o, cost(i, o), bool(w.did), note, w.steps.get(who, [])))
    say(f"{c.id}  {name}, {c.customer}".ljust(WIDTH - 9) + f"{usd(c.cost):>9}", "1;36")
    for row in record.of(c.id):
        show(row)
    if stop:
        board.move(c, "Needs you", stop.rule)
    elif w.waiting:
        hold(state, waiting(state) | {c.id: w.waiting})
        board.move(c, "Needs you", f"waiting for {w.waiting['signer']}: {w.waiting['rule']}")
    elif held := systems.written(c.id):
        c.issue = f"{town(j['hotel'])}, {held['ref']}"
        board.move(c, "Done", f"allowed by policy: {w.did}")
    else:
        board.move(c, "Needs you", w.error or "the employee did not act on it")


async def week(state: Path = STATE, m: Model | str | None = None) -> Board:
    board, systems, record = opened(state)
    seed(board)
    m = m or model()
    for c in board.cards:
        if c.column == "Backlog":
            await work(state, board, systems, record, c, m)
    rows = record.rows()
    alone = sum(r["verdict"] == "allowed" for r in rows)
    asked = len(waiting(state))
    stopped = sum(r["verdict"] == "not started" for r in rows)
    say(f"{alone} done alone, {asked} waiting for a signature, {stopped} not started", "1;36")
    say(f"AI spend this week {usd(sum(c.cost for c in board.cards))}", "1;36")
    return board


def sign(state: Path, card_id: str, by: str, yes: bool) -> Card:
    """The named signer's answer. The action is written only on a yes, and no AI employee may answer."""
    if by in policy.NAMES:
        raise NotAllowed(f"{by} is an AI employee and may not sign")
    rows = waiting(state)
    if card_id not in rows:
        raise NotAllowed("nothing is waiting for a signature on this card")
    w = rows.pop(card_id)
    board, systems, record = opened(state)
    c = board.card(card_id)
    said = describe(w["action"])
    record.add(
        card_id, w["agent"], said, "signed" if yes else "declined", w["rule"], by=by, signer=w["signer"]
    )
    hold(state, rows)
    who = policy.first(w["signer"])
    if yes:
        ref, _ = systems.apply(card_id, w["action"])
        c.issue = f"{town(w['action']['hotel'])}, {ref}"
        board.move(c, "Done", f"signed for {who} by {by}: {said}")
    else:
        board.move(c, "Backlog", f"declined for {who} by {by}; nothing was written")
    return c


def check(state: Path) -> list[tuple[str, bool]]:
    """Every write in the systems against the record: exactly one row allowed or signed it, and back."""
    _, systems, record = opened(state)
    owned = [r for r in record.rows() if r["verdict"] in ("allowed", "signed")]
    out = []
    for w in systems.writes:
        said = describe(w["action"])
        mine = [r for r in owned if r["card"] == w["card"] and r["action"] == said]
        how = mine[0]["verdict"] if len(mine) == 1 else "NO RECORD" if not mine else "TWICE"
        out.append((f"  {w['card']}  {w['system']:<10} {said:<40} {how}", len(mine) == 1))
    landed = {(w["card"], describe(w["action"])) for w in systems.writes}
    for r in owned:
        if (r["card"], r["action"]) not in landed:
            out.append((f"  {r['card']}  {'none':<10} {r['action']:<40} NOT WRITTEN", False))
    return out


def month(state: Path) -> list[dict]:
    """Per employee: what it did alone, what was signed, declined or refused, its AI spend and time saved."""
    board, _, record = opened(state)
    rows = record.rows()
    out = []
    for key, a in policy.AGENTS.items():
        mine = [r for r in rows if r["agent"] == a["name"]]
        n = {
            v: sum(r["verdict"] == v for r in mine)
            for v in ("allowed", "asked", "signed", "declined", "refused")
        }
        done = n["allowed"] + n["signed"]
        cash = spent(board, a["name"])
        out.append(
            {"key": key, "name": a["name"], "status": a["status"], "owner": a["owner"], "done": done}
            | n
            | {
                "spent": cash,
                "cap": a["cap"],
                "saved": done * a["minutes"],
                "per": cash / done if done else None,
            }
        )
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["week", "record", "serve"])
    p.add_argument("--port", type=int, default=8096)
    a = p.parse_args()
    if a.command == "serve":
        from operating.board import serve

        serve(STATE, a.port)
        return
    if a.command == "record":
        rows = check(STATE)
        for row, ok in rows:
            say(row, "1;32" if ok else "1;31")
        good = sum(ok for _, ok in rows)
        say(f"{good} of {len(rows)} writes have exactly one decision behind them", "1;36")
        return
    set_tracing_disabled(True)
    shutil.rmtree(STATE, ignore_errors=True)
    say("Halden Hotels, week 51: seven jobs for four AI employees", "1;36")
    asyncio.run(week())


if __name__ == "__main__":
    main()
