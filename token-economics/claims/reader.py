"""The claims reader: an AI employee that reads each document on an Acme Insurance claim into its claim file.

Documents arrive one at a time, photos and the body shop's quote, and after each one the reader returns
the claim file as it stands. Three levers change what each read costs and nothing about what it reads:

1. Cache the policy: the desk's long policy goes first, the same on every call, so the provider serves it
   from its cache. Off, a timestamp and the claim id sit on top of it and every call starts different.
2. Photos at low detail: a damage photo is judged on the whole car, not its pixels.
3. Think less: reading a photo into a claim file needs little reasoning, and reasoning tokens are billed
   as output, five times the input price. Off, the model reasons at its default effort.

`make demo` runs the same three claims four times, adding one lever each pass, each pass in its own state.
`settle` asks the claims system, never the reader.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import shutil
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
    Card("C-9", "Hail at the Northgate depot", "Northgate Car Hire", "CL-303"),
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


def stamped(ctx: RunContextWrapper[Read], agent: Agent[Read]) -> str:
    # The anti-pattern this session removes: a line that differs on every call, above the long policy.
    r = ctx.context
    return (
        f"Claims desk run {datetime.now(UTC).isoformat()} for {r.claim['id']}, document {r.turn} of "
        f"{len(r.claim['documents'])}.\n\n{POLICY}"
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


async def read_claim(claims: Claims, cid: str, model: Model | str, levers: Levers) -> None:
    """Every document on one claim, one read each, each recorded in the ledger, then the claim file filed."""
    claim, agent = claims.claim(cid), reader(model, levers)
    thread: list[TResponseInputItem] = []
    for turn, doc in enumerate(claim["documents"], 1):
        claims.allow(cid, doc)
        result = await Runner.run(
            agent, ask(claim, doc, levers, thread), context=Read(claim, doc, turn), max_turns=1
        )
        claims.record(cid, doc, result.context_wrapper.usage)
        thread = result.to_input_list()
    claims.file(cid, result.final_output_as(ClaimFile).model_dump())


def steps(claims: Claims, cid: str) -> list[str]:
    return [
        f"Read {t['doc']}: {t['input']:,} in, {t['cached']:,} cached, {t['output']:,} out, {usd(t['cost'])}"
        for t in claims.ledger(cid)
    ]


def settle(board: Board, card: Card, claims: Claims) -> None:
    cid = card.issue
    stop, filed, refused = claims.stopped(cid), claims.filed(cid), claims.refused(cid)
    if stop:
        n = len(claims.claim(cid)["documents"])
        board.move(
            card,
            "Needs you",
            f"{usd(claims.budget)} budget hit, {stop['read']} of {n} read; {claims.adjuster} decides",
        )
    elif refused:
        board.move(card, "Backlog", f"refused: {refused}")
    elif filed is None:
        board.move(card, "Backlog", "the claims system holds no claim file")
    elif filed["total"]:
        board.move(card, "Done", f"filed, {filed['total']} matches the quote")
    else:
        board.move(card, "Done", f"filed, {filed['vehicles']} cars; an assessor prices it")


async def work(board: Board, card: Card, levers: Levers, model: Model | str | None = None) -> Card:
    model = model or default_model()
    claims = Claims.open(board.path.parent)
    board.move(card, "In flight", f"{READER} picked it up")
    card.employee = READER
    try:
        await read_claim(claims, card.issue, model, levers)
        ok, said = True, "claim file filed"
    except Refused as e:
        ok, said = True, str(e)
    except (AgentsException, APIError) as e:
        ok, said = False, f"run failed: {type(e).__name__}"
    rows = claims.ledger(card.issue)
    card.runs.append(
        Run(
            READER,
            sum(t["input"] for t in rows),
            sum(t["output"] for t in rows),
            claims.spent(card.issue),
            ok,
            said,
            steps(claims, card.issue),
            [t["doc"] for t in rows],
        )
    )
    settle(board, card, claims)
    return card


def report(board: Board, card: Card) -> None:
    """One claim as the room reads it off a projector: each read and its cost, where the card landed."""
    for line in card.runs[-1].steps:
        print("  " + line)
    tone = {"Done": "1;32", "Needs you": "1;33"}.get(card.column, "1;31")
    print(paint(f"{card.issue} {card.column}: {card.log[-1].split(': ', 1)[-1]}", tone))


@dataclass
class Pass:
    """One pass of the ladder, every number from its board and its claims system's ledger."""

    n: int
    name: str
    board: Board
    fresh: int
    cached: int
    output: int

    @property
    def filed(self) -> int:
        return sum(c.column == "Done" for c in self.board.cards)

    @property
    def per_claim(self) -> float | None:
        return self.board.cost_per_outcome

    @property
    def per_thousand(self) -> str:
        # A thousand claims is a budget line, so cents, not the ledger's fractions of a cent.
        return "" if self.per_claim is None else f"${self.per_claim * 1000:,.2f}"


def passes(state: Path = STATE) -> list[Pass]:
    out = []
    for n, (name, _) in enumerate(LADDER, 1):
        if not (f := state / f"pass-{n}" / "board.json").exists():
            continue
        board, claims = Board.open(f), Claims.open(f.parent)
        rows = [t for c in board.cards for t in claims.ledger(c.issue)]
        cached = sum(t["cached"] for t in rows)
        out.append(
            Pass(
                n, name, board, sum(t["input"] for t in rows) - cached, cached, sum(t["output"] for t in rows)
            )
        )
    return out


def ladder(state: Path = STATE) -> None:
    print(paint(f"{'':<21} {'fresh in':>8} {'cached':>7} {'out':>6} {'per claim':>9} {'per 1,000':>9}", "1"))
    for p in passes(state):
        cost = "none" if p.per_claim is None else usd(p.per_claim)
        print(f"{p.name:<21} {p.fresh:>8,} {p.cached:>7,} {p.output:>6,} {cost:>9} {p.per_thousand:>9}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--serve", action="store_true", help="serve the board from the last run")
    p.add_argument("--port", type=int, default=8092)
    a = p.parse_args()
    if a.serve:
        from claims.board import serve

        serve(STATE, TEAM, a.port)
        return
    asyncio.run(demo())
    ladder()


async def demo() -> None:
    # Every pass starts from the same three claims, so the only difference between passes is the levers.
    # One event loop and one client for every pass: a loop per card closed the client under its own calls.
    shutil.rmtree(STATE, ignore_errors=True)
    model = default_model()
    for n, (name, levers) in enumerate(LADDER, 1):
        print(paint(f"Pass {n}: {name}", "1;36"))
        board = Board(STATE / f"pass-{n}" / "board.json", [replace(c, runs=[], log=[]) for c in SEED])
        board.save()
        for card in board.cards:
            await work(board, card, levers, model)
            report(board, card)


if __name__ == "__main__":
    main()
