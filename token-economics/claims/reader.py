"""The claims reader: an AI employee that reads each document on a KaaS Insurance claim into its claim file.

Documents arrive one at a time, photos and the body shop's quote, and after each one the reader returns
the claim file as it stands. Three levers change what each read costs and nothing about what it reads:

1. Cache the policy: the desk's long policy goes first, the same on every call, so the provider serves it
   from its cache. Off, a timestamp and the claim id sit on top of it and every call starts different.
2. Photos at low detail: a damage photo is judged on the whole car, not its pixels.
3. Think less: reading a photo into a claim file needs little reasoning, and reasoning tokens are billed
   as output, five times the input price. Off, the model reasons at its default effort.

`make passes` runs the same three claims four times, adding one lever each pass, each pass in its own state.
`make demo PASS=N` runs one of them live. A claim that hits its budget waits for the adjuster, who raises
that claim's budget on the board, and only that claim resumes from the document it stopped at.
`settle` asks the claims system, never the reader.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from agents import Agent, AgentsException, Model, ModelSettings, RunContextWrapper, Runner, TResponseInputItem
from openai import APIError
from openai.types.shared import Reasoning
from pydantic import BaseModel

from claims.system import DOCS, FIXTURES, Claims, Refused, quote_text
from workforce import default_model
from workforce.board import Board, Card, Role, Run, usd
from workforce.mission import paint

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / ".claims"
LIVE = ROOT / ".claims-live"
READER = "Claims reader"
POLICY = (FIXTURES / "policy.md").read_text()
TEAM = (
    Role(
        READER,
        "A claim file the claims system files: every damaged part, and the quote's total to the penny",
        "Cost per claim filed, from the provider's own token counts",
        "Put a total on a claim that no quote prints",
        (),
    ),
)
SEED = (
    Card("C-7", "Front damage, hit a tree", "Priya Shah", "CL-301"),
    Card("C-8", "Hit from behind at the lights", "Tom Brennan", "CL-302"),
    Card("C-9", "Hail damage at a car hire depot", "A car hire firm", "CL-303"),
)


@dataclass(frozen=True)
class Levers:
    cache: bool = False
    low_detail: bool = False
    think_less: bool = False


LADDER = (
    ("No levers", Levers()),
    ("Cache the policy", Levers(cache=True)),
    ("Photos at low detail", Levers(cache=True, low_detail=True)),
    ("Think less", Levers(cache=True, low_detail=True, think_less=True)),
)


class ClaimFile(BaseModel):
    parts: list[str]
    vehicles: int
    total: str | None
    note: str


@dataclass
class Read:
    claim: dict
    doc: str
    turn: int


def stamped(context: RunContextWrapper[Read], agent: Agent[Read]) -> str:
    # The anti-pattern this session removes: a line that differs on every call, above the long policy.
    current = context.context
    return (
        f"Claims desk run {datetime.now(UTC).isoformat()} for {current.claim['id']}, document "
        f"{current.turn} of {len(current.claim['documents'])}.\n\n{POLICY}"
    )


def reader(model: Model | str, levers: Levers) -> Agent[Read]:
    return Agent[Read](
        name=READER,
        instructions=POLICY if levers.cache else stamped,
        model=model,
        output_type=ClaimFile,
        model_settings=ModelSettings(reasoning=Reasoning(effort="low"))
        if levers.think_less
        else ModelSettings(),
    )


def about(claim: dict) -> str:
    return (
        f"Claim {claim['id']} on policy {claim['policy']}\nPolicyholder: {claim['holder']}\n"
        f"Vehicle: {claim['vehicle']}\nWhat happened: {claim['what_happened']}"
    )


def document(doc: str, detail: str) -> list[dict]:
    """One document as message content: a photo as an image, a quote as the text its PDF holds."""
    if doc.endswith(".pdf"):
        return [{"type": "input_text", "text": f"Repair quote {doc}:\n{quote_text(doc)}"}]
    url = "data:image/jpeg;base64," + base64.b64encode((DOCS / doc).read_bytes()).decode()
    return [
        {"type": "input_text", "text": f"Photo {doc}"},
        {"type": "input_image", "image_url": url, "detail": detail},
    ]


def ask(claim: dict, doc: str, levers: Levers, thread: list) -> list:
    """The claim's thread so far and the next document, the way a claim chat grows."""
    detail = "low" if levers.low_detail else "high"
    lead = [{"type": "input_text", "text": about(claim)}] if not thread else []
    return [*thread, {"role": "user", "content": [*lead, *document(doc, detail)]}]


def assistant_item(claim_file: dict) -> dict:
    return {
        "id": "claim-file",
        "role": "assistant",
        "type": "message",
        "status": "completed",
        "content": [{"type": "output_text", "text": json.dumps(claim_file), "annotations": []}],
    }


def thread_so_far(claim: dict, rows: list[dict], levers: Levers) -> list:
    """The chat a resumed claim carries on from: each document read and the claim file it got."""
    thread: list = []
    for row in rows:
        thread = ask(claim, row["doc"], levers, thread)
        thread.append(assistant_item(row["claim_file"]))
    return thread


def read_line(claim_id: str, row: dict) -> str:
    return (
        f"{claim_id}  {row['doc']} {row['input']:,} in {row['cached']:,} cached "
        f"{row['output']:,} out  {usd(row['cost'])}"
    )


async def read_claim(
    claims: Claims,
    claim_id: str,
    model: Model | str,
    levers: Levers,
    show: Callable[[str], None] | None = None,
) -> None:
    """The documents not yet in the ledger, one read each, then the claim file filed from the last read."""
    claim, agent = claims.claim(claim_id), reader(model, levers)
    already = claims.ledger(claim_id)
    thread: list[TResponseInputItem] = thread_so_far(claim, already, levers)
    for turn, doc in enumerate(claim["documents"], 1):
        if turn <= len(already):
            continue
        claims.allow(claim_id, doc)
        result = await Runner.run(
            agent, ask(claim, doc, levers, thread), context=Read(claim, doc, turn), max_turns=1
        )
        claim_file = result.final_output_as(ClaimFile).model_dump()
        row = claims.record(claim_id, doc, result.context_wrapper.usage, claim_file)
        if show:
            show(read_line(claim_id, row))
        thread = result.to_input_list()
    claims.file(claim_id, claims.ledger(claim_id)[-1]["claim_file"])


def steps(rows: list[dict]) -> list[str]:
    return [
        f"Read {row['doc']}: {row['input']:,} in, {row['cached']:,} cached, {row['output']:,} out, "
        f"{usd(row['cost'])}"
        for row in rows
    ]


def settle(board: Board, card: Card, claims: Claims) -> None:
    claim_id = card.issue
    total = len(claims.claim(claim_id)["documents"])
    stop = claims.stopped(claim_id)
    declined = claims.declined(claim_id)
    refused = claims.refused(claim_id)
    filed = claims.filed(claim_id)
    read = len(claims.ledger(claim_id))
    if stop:
        board.move(
            card,
            "Needs you",
            f"{usd(claims.budget_for(claim_id))} budget hit, {stop['read']} of {total} read; "
            f"raise the budget? {claims.adjuster} decides",
        )
    elif declined:
        board.move(
            card,
            "Backlog",
            f"{claims.adjuster} kept the budget; {read} of {total} read, an assessor visits",
        )
    elif refused:
        board.move(card, "Backlog", f"refused: {refused}")
    elif filed is None:
        board.move(card, "Backlog", "the claims system holds no claim file")
    elif filed["total"]:
        board.move(card, "Done", f"filed, {filed['total']} matches the quote")
    else:
        photos = len([doc for doc in claims.claim(claim_id)["documents"] if not doc.endswith(".pdf")])
        board.move(
            card, "Done", f"filed, {filed['vehicles']} cars from {photos} photos; an assessor prices it"
        )


async def work(
    board: Board,
    card: Card,
    levers: Levers,
    model: Model | str | None = None,
    show: Callable[[str], None] | None = None,
) -> Card:
    """One claim, from where its ledger left off: a new claim starts at its first document."""
    model = model or default_model()
    claims = Claims.open(board.path.parent)
    before = len(claims.ledger(card.issue))
    if before:
        resumed_at = claims.claim(card.issue)["documents"][before]
        board.move(card, "In flight", f"{READER} picked it up again at {resumed_at}")
    else:
        board.move(card, "In flight", f"{READER} picked it up")
    card.employee = READER
    try:
        await read_claim(claims, card.issue, model, levers, show)
        succeeded, said = True, "claim file filed"
    except Refused as refusal:
        succeeded, said = True, str(refusal)
    except (AgentsException, APIError) as error:
        succeeded, said = False, f"run failed: {type(error).__name__}"
    rows = claims.ledger(card.issue)[before:]
    card.runs.append(
        Run(
            READER,
            sum(row["input"] for row in rows),
            sum(row["output"] for row in rows),
            sum(row["cost"] for row in rows),
            succeeded,
            said,
            steps(rows),
            [row["doc"] for row in rows],
        )
    )
    settle(board, card, claims)
    return card


async def resume_after_raise(
    board: Board, card: Card, levers: Levers, model: Model | str | None = None
) -> Card:
    """The adjuster raised this claim's budget: read the documents it stopped before, and file."""
    return await work(board, card, levers, model)


def report(board: Board, card: Card, claims: Claims) -> None:
    """One claim as the room reads it off a projector: where it stopped, where the card landed."""
    stop = claims.stopped(card.issue)
    if stop:
        budget = usd(claims.budget_for(card.issue))
        print(
            paint(
                f"{card.issue} stopped before {stop['before']}: one more read would cross its {budget}",
                "33",
            )
        )
    tone = {"Done": "1;32", "Needs you": "1;33"}.get(card.column, "1;31")
    print(paint(f"{card.issue} {card.column}: {card.log[-1].split(': ', 1)[-1]}", tone))


@dataclass
class Pass:
    """One pass of the ladder, every number from its board and its claims system's ledger."""

    number: int
    name: str
    board: Board
    fresh: int
    cached: int
    output: int

    @property
    def filed(self) -> int:
        return sum(card.column == "Done" for card in self.board.cards)

    @property
    def per_claim(self) -> float | None:
        return self.board.cost_per_outcome

    @property
    def per_thousand(self) -> str:
        # A thousand claims is a budget line, so cents, not the ledger's fractions of a cent.
        return "" if self.per_claim is None else f"${self.per_claim * 1000:,.2f}"


def passes(state: Path = STATE) -> list[Pass]:
    found = []
    for number, (name, _) in enumerate(LADDER, 1):
        board_file = state / f"pass-{number}" / "board.json"
        if not board_file.exists():
            continue
        board, claims = Board.open(board_file), Claims.open(board_file.parent)
        rows = [row for card in board.cards for row in claims.ledger(card.issue)]
        cached = sum(row["cached"] for row in rows)
        fresh = sum(row["input"] for row in rows) - cached
        found.append(Pass(number, name, board, fresh, cached, sum(row["output"] for row in rows)))
    return found


def ladder(state: Path = STATE) -> None:
    print(paint(f"{'':<21} {'fresh in':>8} {'cached':>7} {'out':>6} {'per claim':>9} {'per 1,000':>9}", "1"))
    for result in passes(state):
        cost = "none" if result.per_claim is None else usd(result.per_claim)
        print(
            f"{result.name:<21} {result.fresh:>8,} {result.cached:>7,} {result.output:>6,} "
            f"{cost:>9} {result.per_thousand:>9}"
        )


async def run_pass(root: Path, number: int, model: Model | str) -> None:
    """Every pass starts from the same three claims, so the only difference between passes is the levers."""
    name, levers = LADDER[number - 1]
    print(paint(f"Pass {number}: {name}", "1;36"))
    board = Board(root / f"pass-{number}" / "board.json", [replace(card, runs=[], log=[]) for card in SEED])
    board.save()
    claims = Claims.open(board.path.parent)
    for card in board.cards:
        await work(board, card, levers, model, show=print)
        report(board, card, claims)
    filed = sum(card.column == "Done" for card in board.cards)
    cost = board.cost_per_outcome
    print(
        paint(f"{filed} of {len(SEED)} filed, {'none' if cost is None else usd(cost)} per claim filed", "1")
    )


async def run_passes() -> None:
    # One event loop and one client for every pass: a loop per card closed the client under its own calls.
    shutil.rmtree(STATE, ignore_errors=True)
    model = default_model()
    for number in range(1, len(LADDER) + 1):
        await run_pass(STATE, number, model)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true", help="serve the board: the newest live pass")
    parser.add_argument("--pass", dest="number", type=int, choices=range(1, len(LADDER) + 1))
    parser.add_argument("--passes", action="store_true", help="run all four passes into the stored ladder")
    parser.add_argument("--ladder", action="store_true", help="print the stored ladder; no model")
    parser.add_argument("--port", type=int, default=8092)
    args = parser.parse_args()
    if args.serve:
        from claims.board import Desk, serve

        serve(Desk(STATE, LIVE), TEAM, args.port)
    elif args.ladder:
        ladder()
    elif args.passes:
        asyncio.run(run_passes())
        ladder()
    else:
        number = args.number or len(LADDER)
        shutil.rmtree(LIVE / f"pass-{number}", ignore_errors=True)
        asyncio.run(run_pass(LIVE, number, default_model()))


if __name__ == "__main__":
    main()
