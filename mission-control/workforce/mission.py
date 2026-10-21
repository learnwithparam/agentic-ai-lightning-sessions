"""The manager and the support resolver, working a card on the board.

The manager owns the card and hands it to the employee hired for it: the analyst for an engineer's
question, the resolver for a customer's GitHub issue. A reply to a customer cannot be unsent, so the
resolver only drafts one; the card waits in Needs you until a human decides. No agent can approve, the
maker least of all, and that rule lives in `decide`, not in a prompt. A card is Done when GitHub shows
the reply, never when an agent says it replied.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import textwrap
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path

from agents import (
    Agent,
    AgentsException,
    Model,
    ModelSettings,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    Runner,
    Usage,
    function_tool,
)
from agents.tool_context import ToolContext
from openai import APIError

from workforce import default_model
from workforce.access import Access
from workforce.analyst import INSTRUCTIONS as ANALYST_INSTRUCTIONS
from workforce.analyst import Desk, Reply, check_citations, search_docs, search_github, search_linear
from workforce.board import Board, Card, Role, Run, price, serve, start
from workforce.issues import Issues
from workforce.sources import Readers, Source
from workforce.telegram import Telegram

MANAGER, RESOLVER, ANALYST = "Manager", "Support resolver", "Engineering analyst"
EMPLOYEES = frozenset({MANAGER, RESOLVER, ANALYST})
# Each employee is hired for one outcome, watched by one metric, and has one thing it must never do.
# The tests in tests/test_mission.py are those must-nevers; the board's Team page shows this table.
TEAM = (
    Role(
        MANAGER,
        "Every card owned by the one employee hired for it",
        "Cards that came back to Backlog",
        "Approve an action an AI employee made",
        ("hand off", "approve_pending"),
    ),
    Role(
        RESOLVER,
        "A customer's GitHub issue answered with the known fix",
        "Cost per resolution",
        "Post to a customer without a human",
        ("read_issue", "search_linear", "search_docs", "request_reply"),
    ),
    Role(
        ANALYST,
        "An engineer's question answered from sources they may open",
        "Answers closed on a source",
        "Cite a source it did not read",
        ("search_github", "search_linear", "search_docs"),
    ),
)
STATE = Path(__file__).resolve().parent.parent / ".mission"


class NotAllowed(Exception):
    pass


@dataclass
class Shift(Desk):
    """What one run on a card may touch: the readers, the customer's GitHub issue and the card itself."""

    issues: Issues | None = None
    card: Card | None = None


class Steps(RunHooks[Shift]):
    """Every handoff and tool call in one run, in order, for the card's page. Failed runs keep theirs."""

    def __init__(self) -> None:
        self.seen: list[str] = []
        self.usage = Usage()

    async def on_agent_start(self, context: RunContextWrapper[Shift], agent: Agent) -> None:
        # The run's running total, so a run the host cuts off still counts what it spent.
        self.usage = context.usage

    async def on_handoff(self, context: RunContextWrapper[Shift], from_agent: Agent, to_agent: Agent) -> None:
        self.seen.append(f"{from_agent.name} handed the card to {to_agent.name}")

    async def on_tool_start(self, context: RunContextWrapper[Shift], agent: Agent, tool: object) -> None:
        self.seen.append(f"{agent.name} called {getattr(tool, 'name', tool)}")


def for_customer(text: str) -> str:
    """The reply as the customer reads it: internal ids such as [EXP-41] are for the approver only."""
    return re.sub(r"[ \t]*\[[^\[\]]+\]", "", text).strip()


def decide(card: Card, issues: Issues, by: str, approve: bool) -> str:
    """The maker-checker gate. Only a human other than the maker settles a pending action."""
    if card.pending is None:
        raise NotAllowed("nothing is waiting for a decision")
    if by in EMPLOYEES or by == card.pending["maker"]:
        raise NotAllowed(f"{by} may not approve an action an AI employee made")
    action, card.pending = card.pending, None
    if not approve:
        return f"{by} declined the reply on GH#{action['issue']}"
    issues.reply(action["issue"], action["body"], card.id, approved_by=by)
    return f"{by} approved; the reply is posted on GH#{action['issue']}"


@function_tool
def read_issue(ctx: ToolContext[Shift], number: int) -> str:
    """Read a GitHub issue: what the customer wrote and every comment on it."""
    shift = ctx.context
    if refused := shift.refused(ctx):
        return refused
    if shift.card and number != shift.card.github:
        # Issues carry their own readers; a run may open only the customer's issue on its card.
        return f"GH#{number} is not this card's issue."
    try:
        i = shift.issues.issue(number)
    except KeyError:
        return f"There is no issue #{number}."
    shift.keep([Source(f"GH#{number}", "github", i["title"], i["body"], i["url"])])
    said = "\n".join(f"{c['author']}: {c['body']}" for c in i["comments"]) or "No comments yet."
    return f"[GH#{number}] {i['title']} ({i['state']}, opened by {i['author']})\n{i['body']}\n\n{said}"


@function_tool
def request_reply(ctx: ToolContext[Shift], body: str) -> str:
    """Draft the reply to the customer on their GitHub issue. A human approves it before it is posted.
    End each sentence with the id of the issue or doc it rests on, like [EXP-41]."""
    shift = ctx.context
    if refused := shift.refused(ctx):
        return refused
    number = shift.card.github
    if number is None:
        return "This card has no GitHub issue to reply on."
    # A promise to a customer rests on what the team wrote down; a draft citing nothing it read goes back.
    grounded = check_citations(body, shift.read)
    rests_on = [s.id for s in grounded.sources if s.id != f"GH#{number}"]
    if not rests_on:
        return "Refused: cite the Linear issue or doc each promise rests on, like [EXP-41]. Read them first."
    shift.card.pending = {
        "action": "reply",
        "issue": number,
        "body": for_customer(grounded.text),
        "rests_on": rests_on,
        "maker": RESOLVER,
    }
    return "Sent to a human for approval. Stop here and say the reply is waiting for approval."


@function_tool
def approve_pending(ctx: RunContextWrapper[Shift]) -> str:
    """Approve the action waiting on this card."""
    shift = ctx.context
    try:
        return decide(shift.card, shift.issues, by=MANAGER, approve=True)
    except NotAllowed as e:
        shift.attempts.append(str(e))
        return f"Refused: {e}. A human must approve it in Needs you."


def resolver(model: Model | str | None) -> Agent[Shift]:
    return Agent[Shift](
        name=RESOLVER,
        handoff_description="Answers a customer's GitHub issue with the fix the team already planned.",
        instructions=(
            "You answer customers on their GitHub issue. Read the issue first, then search Linear and "
            "the docs for the known cause, the planned fix and a workaround. Draft one short, friendly "
            "reply with request_reply: what is wrong, what they can do today, and what is planned. "
            "Promise only what Linear or the docs say, and end each sentence with its source id."
        ),
        tools=[read_issue, search_linear, search_docs, request_reply],
        model=model,
        # Handed a card, a small model may answer with its own name and draft nothing; its first move
        # must be a tool.
        model_settings=ModelSettings(tool_choice="required"),
    )


def manager(model: Model | str | None = None) -> Agent[Shift]:
    model = model or default_model()
    analyst = Agent[Shift](
        name=ANALYST,
        handoff_description="Answers engineers' questions from GitHub, Linear and the docs.",
        instructions=ANALYST_INSTRUCTIONS,
        tools=[search_github, search_linear, search_docs],
        model=model,
        # Handed a card, a small model may reply "forwarded to the analyst" without searching; its first
        # move must be a search.
        model_settings=ModelSettings(tool_choice="required"),
    )
    return Agent[Shift](
        name=MANAGER,
        instructions=(
            "You manage a board of AI employees. Read the card and hand it to the one employee hired for "
            "it: the support resolver for a customer's issue that needs a reply, the engineering analyst "
            "for an engineer's question about the code or a bug. Do not do their work yourself."
        ),
        handoffs=[resolver(model), analyst],
        tools=[approve_pending],
        model=model,
        # Small models sometimes answer a card with nothing; the manager's first move must be a handoff
        # or a tool. The SDK lifts this after one tool call, so the manager cannot loop on it.
        model_settings=ModelSettings(tool_choice="required"),
    )


def settle(board: Board, card: Card, issues: Issues, said: str) -> None:
    n = card.github
    if card.pending:
        board.move(card, "Needs you", f"the reply on GH#{n} waits for a human")
    elif n is not None and issues.replied(n, card.id):
        board.move(card, "Done", f"GitHub shows the reply on GH#{n}")
    else:
        board.move(card, "Backlog", f"agent said: {said[:80]!r}; GitHub shows no reply on GH#{n}")


async def work(
    board: Board,
    card: Card,
    issues: Issues,
    readers: Readers,
    model: Model | str | None = None,
    access: Access | None = None,
) -> Card:
    board.move(card, "In flight", f"{MANAGER} picked it up")
    shift, steps = Shift(readers, access=access, issues=issues, card=card), Steps()
    kind = f"a customer issue from {card.customer}" if card.customer else "an engineer's question"
    prompt = f"Card {card.id}: {card.title}\nKind: {kind}\nLinked: {card.issue}"
    # The card is the trace's group, so every run on one card reads as one session in Langfuse.
    traced = RunConfig(
        workflow_name=f"{card.id} {card.title}",
        group_id=card.id,
        trace_metadata={"card": card.id, "user": access.user if access else readers.user},
    )
    try:
        result = await Runner.run(
            manager(model), prompt, context=shift, max_turns=12, hooks=steps, run_config=traced
        )
        usage, ok, said = result.context_wrapper.usage, True, str(result.final_output)
        card.employee = result.last_agent.name
        if not said.strip():
            # A host that fails mid-answer can still return 200 with nothing in it: a failed run, not a reply.
            ok, said = False, "run failed: the model returned nothing"
    except AgentsException as e:
        run = e.run_data
        usage, ok, said = (run.context_wrapper.usage if run else Usage()), False, f"run failed: {e}"
    except APIError as e:
        # A host that is down or rate limited fails this run; the card goes back, the board keeps going.
        usage, ok, said = steps.usage, False, f"run failed: {type(e).__name__}"
    card.runs.append(
        Run(
            card.employee or MANAGER,
            usage.input_tokens,
            usage.output_tokens,
            price(usage),
            ok,
            said,
            steps.seen + [f"Gate refused: {a}" for a in shift.attempts],
            sorted(shift.read),
        )
    )
    card.log.extend(f"Gate: {a}" for a in shift.attempts)
    # The card's kind picks the check, not who took it: a question has no customer and no reply to read
    # back, so only its sources can close it.
    if card.employee in ("", MANAGER) and not card.pending:
        board.move(card, "Backlog", "no employee took the card" if ok else said[:80])
    elif card.customer:
        settle(board, card, issues, said)
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
    board: Board, card: Card, issues: Issues, approver: Approver, access: Access | None = None
) -> Card:
    by, yes = await approver(card)
    # Being a human is not enough with the stack on: OpenFGA must say this person approves support replies.
    if access and not access.may_approve(by):
        board.move(card, "Needs you", f"{by} may not approve replies on the support desk")
        return card
    board.move(card, "In flight", decide(card, issues, by=by, approve=yes))
    settle(board, card, issues, "approved" if yes else "declined")
    return card


SEED = Card("C-1", "Globex: CSV export times out on large workspaces", "Globex", "GH#412")
QUESTION = Card("C-2", "Is the export timeout a known bug, and who owns the fix?", "", "GH#412")


# The demo is read off a projector, so its lines are short and each column has one colour.
TONE = {"Backlog": "2", "Needs you": "1;33", "In flight": "1;36", "Done": "1;32", "Gate": "1;31"}


def paint(text: str, tone: str) -> str:
    return f"\033[{tone}m{text}\033[0m" if os.isatty(1) else text


def show(card: Card) -> None:
    """One card as the room reads it: the id and title, then where it went and why, wrapped short."""
    column, _, why = card.log[-1].partition(": ")
    print(f"\n{paint(card.id, '1')}  {card.title}")
    print(f"    {card.employee or MANAGER}  {paint(column, TONE[column])}")
    for line in textwrap.wrap(why, 60):
        print(f"    {paint(line, '2')}")
    for gate in [x.removeprefix("Gate: ") for x in card.log if x.startswith("Gate: ")][-1:]:
        print(f"    {paint('Gate refused: ' + gate, TONE['Gate'])}")


async def console(card: Card) -> tuple[str, bool]:
    p = card.pending
    print(f"\n{paint(f'Reply to {card.customer} on GH#{p["issue"]}', TONE['Needs you'])}")
    print(f"    rests on {paint(', '.join(p['rests_on']), '1')}")
    for line in textwrap.wrap(p["body"], 60):
        print(f"    {line}")
    answer = await asyncio.to_thread(input, "\nPost this reply? [y/N] ")
    return f"terminal:{os.environ.get('USER', 'human')}", answer.strip().lower() == "y"


def on_board(state: Path) -> Approver:
    """The Approve and Decline buttons on the board: the page writes the decision, this reads it."""

    async def wait(card: Card) -> tuple[str, bool]:
        f = state / "decisions" / f"{card.id}.json"
        while not f.exists():
            await asyncio.sleep(0.5)
        d = json.loads(f.read_text())
        f.unlink()
        return d["by"], d["yes"]

    return wait


async def demo(fresh: bool, ask: str = "telegram", port: int = 8080) -> None:
    if fresh:
        for f in ("board.json", "github.json"):
            (STATE / f).unlink(missing_ok=True)
    board, issues = Board.open(STATE / "board.json"), Issues.open(STATE / "github.json")
    if not board.cards:
        board.cards += [replace(c, runs=[], log=[], sources=[]) for c in (SEED, QUESTION)]
        board.save()
    choices = {"telegram": Telegram.from_env(), "terminal": console, "board": on_board(STATE)}
    approver = choices[ask] or console
    if approver is console:
        ask = "terminal"
    await start(STATE, TEAM, port)
    print(f"{paint('Mission Control', '1')} on {paint(f'http://127.0.0.1:{port}', '4')}")
    access = Access.from_env()
    # Every card is worked before anyone is asked, so a question is answered while a reply waits.
    for card in [c for c in board.cards if c.column == "Backlog"]:
        # The resolver reads as support staff and the analyst as an engineer, each seeing only their rows.
        user = "U_SUPPORT" if card.customer else "U_ENG"
        await work(board, card, issues, Readers.from_env(user=user), access=access)
        if not card.runs[-1].ok:
            # A failed run is the host, not the card: work it once more. Both runs count in the cost.
            await work(board, card, issues, Readers.from_env(user=user), access=access)
        if card.pending:
            card.pending["asked"] = ask
            board.save()
        show(card)
    for card in [c for c in board.cards if c.column == "Needs you"]:
        await ask_human(board, card, issues, approver, access)
        show(card)
    cpo = board.cost_per_outcome
    print(f"\ncost per outcome {paint('no outcome yet' if cpo is None else f'${cpo:.4f}', '1')}")
    if ask == "board":
        await asyncio.Event().wait()


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description="Work the board, or serve it")
    cli.add_argument("--serve", action="store_true", help="serve the board on http://127.0.0.1:8080")
    cli.add_argument("--fresh", action="store_true", help="start over from the fixture issues and two cards")
    cli.add_argument(
        "--ask", choices=("telegram", "terminal", "board"), default="telegram", help="where approval is asked"
    )
    args = cli.parse_args()
    if args.serve:
        serve(STATE, TEAM)
    else:
        asyncio.run(demo(args.fresh, args.ask))
