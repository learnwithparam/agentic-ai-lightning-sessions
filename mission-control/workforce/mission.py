"""The manager and the support resolver, working a card on the board.

The manager owns the card and hands it to the employee hired for it: the analyst for a question, the
resolver for a customer's money. A refund cannot be undone, so the resolver only requests one; the card
waits in Needs you until a human decides. No agent can approve, the maker least of all, and that rule
lives in `decide`, not in a prompt. A card is Done when the ledger agrees, never when an agent says so.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path

from agents import Agent, AgentsException, Model, RunConfig, RunContextWrapper, Runner, Usage, function_tool
from agents.tool_context import ToolContext

from workforce import default_model
from workforce.access import Access
from workforce.analyst import INSTRUCTIONS as ANALYST_INSTRUCTIONS
from workforce.analyst import Desk, Reply, check_citations, search_docs, search_github, search_linear
from workforce.board import Board, Card, Run, price, serve
from workforce.ledger import Ledger
from workforce.sources import Readers

MANAGER, RESOLVER, ANALYST = "Manager", "Support resolver", "Engineering analyst"
EMPLOYEES = frozenset({MANAGER, RESOLVER, ANALYST})
STATE = Path(__file__).resolve().parent.parent / ".mission"


class NotAllowed(Exception):
    pass


@dataclass
class Shift(Desk):
    """What one run on a card may touch: the analyst's readers, the ledger and the card itself."""

    ledger: Ledger | None = None
    card: Card | None = None


def decide(card: Card, ledger: Ledger, by: str, approve: bool) -> str:
    """The maker-checker gate. Only a human other than the maker settles a pending action."""
    if card.pending is None:
        raise NotAllowed("nothing is waiting for a decision")
    if by in EMPLOYEES or by == card.pending["maker"]:
        raise NotAllowed(f"{by} may not approve an action an AI employee made")
    action, card.pending = card.pending, None
    if not approve:
        return f"{by} declined the refund of {action['charge']}"
    row = ledger.refund(action["charge"], approved_by=by)
    return f"{by} approved; refunded {row['amount'] / 100:.2f} on {action['charge']}"


@function_tool
def find_charges(ctx: ToolContext[Shift], customer: str) -> str:
    """List a customer's charges in the payments ledger, with the order each belongs to."""
    if refused := ctx.context.refused(ctx):
        return refused
    rows = [c for c in ctx.context.ledger.charges if c["customer"] == customer]
    if not rows:
        return f"No charges for {customer}."
    return "\n".join(
        f"{c['id']} order {c['order']} {c['amount'] / 100:.2f} at {c['at']}"
        f"{' (refunded)' if ctx.context.ledger.refunded(c['id']) else ''}"
        for c in rows
    )


@function_tool
def request_refund(ctx: ToolContext[Shift], charge_id: str, reason: str) -> str:
    """Ask a human to approve refunding one charge. The refund happens only after they approve."""
    shift = ctx.context
    if refused := shift.refused(ctx):
        return refused
    try:
        charge = shift.ledger.charge(charge_id)
    except KeyError:
        return f"There is no charge {charge_id}."
    if charge["customer"] != shift.card.customer:
        return f"{charge_id} belongs to another customer; this card is about {shift.card.customer}."
    if shift.ledger.refunded(charge_id):
        return f"{charge_id} is already refunded."
    shift.card.pending = {"action": "refund", "charge": charge_id, "reason": reason, "maker": RESOLVER}
    return "Sent to a human for approval. Stop here and say the refund is waiting for approval."


@function_tool
def approve_pending(ctx: RunContextWrapper[Shift]) -> str:
    """Approve the action waiting on this card."""
    shift = ctx.context
    try:
        return decide(shift.card, shift.ledger, by=MANAGER, approve=True)
    except NotAllowed as e:
        shift.attempts.append(str(e))
        return f"Refused: {e}. A human must approve it in Needs you."


def resolver(model: Model | str | None) -> Agent[Shift]:
    return Agent[Shift](
        name=RESOLVER,
        handoff_description="Resolves a customer's billing problem in the payments ledger.",
        instructions=(
            "You resolve customers' billing problems. Look up the customer's charges first. A duplicate "
            "is a second charge on the same order. Request a refund for each duplicate, never for the "
            "first charge on an order. Say plainly what you found and what is waiting for approval."
        ),
        tools=[find_charges, request_refund],
        model=model,
    )


def manager(model: Model | str | None = None) -> Agent[Shift]:
    model = model or default_model()
    analyst = Agent[Shift](
        name=ANALYST,
        handoff_description="Answers engineering questions from GitHub, Linear and the docs.",
        instructions=ANALYST_INSTRUCTIONS,
        tools=[search_github, search_linear, search_docs],
        model=model,
    )
    return Agent[Shift](
        name=MANAGER,
        instructions=(
            "You manage a board of AI employees. Read the card and hand it to the one employee hired for "
            "it: the support resolver for a customer's charges or refunds, the engineering analyst for "
            "a question about the code or a bug. Do not do their work yourself."
        ),
        handoffs=[resolver(model), analyst],
        tools=[approve_pending],
        model=model,
    )


def settle(board: Board, card: Card, ledger: Ledger, said: str) -> None:
    if card.pending:
        board.move(card, "Needs you", f"refund of {card.pending['charge']} waits for a human")
    elif not ledger.duplicates(card.customer):
        board.move(card, "Done", "the ledger shows one charge per order")
    else:
        extra = ", ".join(ledger.duplicates(card.customer))
        board.move(card, "Backlog", f"agent said: {said[:80]!r}; the ledger still shows {extra}")


async def work(
    board: Board,
    card: Card,
    ledger: Ledger,
    readers: Readers,
    model: Model | str | None = None,
    access: Access | None = None,
) -> Card:
    board.move(card, "In flight", f"{MANAGER} picked it up")
    shift = Shift(readers, access=access, ledger=ledger, card=card)
    prompt = f"Card {card.id}: {card.title}\nCustomer: {card.customer}\nLinked issue: {card.issue}"
    # The card is the trace's group, so every run on one card reads as one session in Langfuse.
    traced = RunConfig(
        workflow_name=f"{card.id} {card.title}",
        group_id=card.id,
        trace_metadata={"card": card.id, "user": access.user if access else readers.user},
    )
    try:
        result = await Runner.run(manager(model), prompt, context=shift, max_turns=12, run_config=traced)
        usage, ok, said = result.context_wrapper.usage, True, str(result.final_output)
        card.employee = result.last_agent.name
    except AgentsException as e:
        run = e.run_data
        usage, ok, said = (run.context_wrapper.usage if run else Usage()), False, f"run failed: {e}"
    card.runs.append(
        Run(card.employee or MANAGER, usage.input_tokens, usage.output_tokens, price(usage), ok, said)
    )
    card.log.extend(f"Gate: {a}" for a in shift.attempts)
    # The card's kind picks the check, not who took it: a question has no customer, and an empty
    # customer has no duplicates, so the ledger would close a question nobody answered.
    if card.employee in ("", MANAGER) and not card.pending:
        board.move(card, "Backlog", "no employee took the card" if ok else said[:80])
    elif card.customer:
        settle(board, card, ledger, said)
    else:
        answer(board, card, check_citations(said, shift.read))
    return card


def answer(board: Board, card: Card, reply: Reply) -> None:
    """A question is Done when its answer cites a source the analyst read in this run.

    A cited id no tool returned is cut first, so an invented source never closes a card.
    """
    card.answer, card.sources = reply.text, [s.id for s in reply.sources]
    if card.sources:
        board.move(card, "Done", f"answered from {', '.join(card.sources)}")
    else:
        board.move(card, "Backlog", "the answer cited no source the analyst read")


Approver = Callable[[Card], Awaitable[tuple[str, bool]]]


async def ask_human(
    board: Board, card: Card, ledger: Ledger, approver: Approver, access: Access | None = None
) -> Card:
    by, yes = await approver(card)
    # Being a human is not enough with the stack on: OpenFGA must say this person approves billing.
    if access and not access.may_approve(by):
        board.move(card, "Needs you", f"{by} may not approve refunds on the billing desk")
        return card
    board.move(card, "In flight", decide(card, ledger, by=by, approve=yes))
    settle(board, card, ledger, "approved" if yes else "declined")
    return card


SEED = Card("C-1", "Customer charged twice at checkout", "cus_812", "GH#412, PAY-88")
QUESTION = Card("C-2", "Is the double charge a known bug, and who owns the fix?", "", "GH#412")


async def console(card: Card) -> tuple[str, bool]:
    p = card.pending
    answer = await asyncio.to_thread(input, f"Approve refund of {p['charge']} ({p['reason']})? [y/N] ")
    return os.environ.get("USER", "human"), answer.strip().lower() == "y"


async def demo(fresh: bool, ask: str = "telegram") -> None:
    from workforce.telegram import Telegram

    if fresh:
        for f in ("board.json", "ledger.json"):
            (STATE / f).unlink(missing_ok=True)
    board, ledger = Board.open(STATE / "board.json"), Ledger.open(STATE / "ledger.json")
    if not board.cards:
        board.cards += [replace(c, runs=[], log=[], sources=[]) for c in (SEED, QUESTION)]
        board.save()
    # The approver is any async card -> (who, yes); Telegram when a bot is set, else this terminal.
    approver = (ask == "telegram" and Telegram.from_env()) or console
    access = Access.from_env()
    # Every card is worked before anyone is asked, so a question is answered while a refund waits.
    for card in [c for c in board.cards if c.column == "Backlog"]:
        await work(board, card, ledger, Readers.from_env(user="U_ENG"), access=access)
        print(f"{card.id} {card.log[-1]}")
    for card in [c for c in board.cards if c.column == "Needs you"]:
        await ask_human(board, card, ledger, approver, access)
        print(f"{card.id} {card.log[-1]}")
    cpo = board.cost_per_outcome
    print("cost per outcome:", "no outcome yet" if cpo is None else f"${cpo:.4f}")


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description="Work the board, or serve it")
    cli.add_argument("--serve", action="store_true", help="serve the board on http://127.0.0.1:8080")
    cli.add_argument("--fresh", action="store_true", help="start from the fixture ledger and one card")
    cli.add_argument(
        "--ask", choices=("telegram", "terminal"), default="telegram", help="where approval is asked"
    )
    args = cli.parse_args()
    if args.serve:
        serve(STATE)
    else:
        asyncio.run(demo(args.fresh, args.ask))
