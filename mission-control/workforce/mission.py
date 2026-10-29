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
import os
import re
import shutil
import textwrap
from collections.abc import Callable
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
    set_tracing_disabled,
)
from agents.testing import ScriptedModel, assistant_message, function_call
from agents.tool_context import ToolContext
from openai import APIError

from workforce import default_model
from workforce.access import Access, may_post_to_customer
from workforce.analyst import INSTRUCTIONS as ANALYST_INSTRUCTIONS
from workforce.analyst import Desk, Reply, check_citations, search_docs, search_github, search_linear
from workforce.board import Board, Card, Role, Run, price, serve
from workforce.issues import Issues
from workforce.people import Person
from workforce.sources import Readers, Source

MANAGER = "Manager"
RESOLVER = "Support resolver"
ANALYST = "Engineering analyst"
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
NAIVE_STATE = STATE.parent / ".mission-naive"


class NotAllowed(Exception):
    pass


@dataclass
class Shift(Desk):
    """What one run on a card may touch: the readers, the customer's GitHub issue and the card itself."""

    issues: Issues | None = None
    card: Card | None = None


class Steps(RunHooks[Shift]):
    """Every handoff and tool call in one run, in order, for the card's page. Failed runs keep theirs."""

    def __init__(self, narrate: Callable[[str], None] | None = None) -> None:
        self.seen: list[str] = []
        self.usage = Usage()
        self.narrate = narrate

    def add(self, line: str) -> None:
        self.seen.append(line)
        if self.narrate:
            self.narrate(line)

    async def on_agent_start(self, context: RunContextWrapper[Shift], agent: Agent) -> None:
        # The run's running total, so a run the host cuts off still counts what it spent.
        self.usage = context.usage

    async def on_handoff(self, context: RunContextWrapper[Shift], from_agent: Agent, to_agent: Agent) -> None:
        self.add(f"{from_agent.name} handed the card to {to_agent.name}")

    async def on_tool_start(self, context: RunContextWrapper[Shift], agent: Agent, tool: object) -> None:
        self.add(f"{agent.name} called {getattr(tool, 'name', tool)}")


def for_customer(text: str) -> str:
    """The reply as the customer reads it: internal ids such as [EXP-41] are for the approver only."""
    return re.sub(r"[ \t]*\[[^\[\]]+\]", "", text).strip()


def decide(card: Card, issues: Issues, person: str, approve: bool) -> str:
    """The maker-checker gate. Only a human other than the maker settles a pending action."""
    if card.pending is None:
        raise NotAllowed("nothing is waiting for a decision")
    if person in EMPLOYEES or person == card.pending["maker"]:
        raise NotAllowed(f"{person} may not approve an action an AI employee made")
    action, card.pending = card.pending, None
    if not approve:
        return f"{person} declined the reply on GH#{action['issue']}"
    issues.reply(action["issue"], action["body"], card.card_id, approved_by=person)
    return f"{person} approved; the reply is posted on GH#{action['issue']}"


@function_tool
def read_issue(ctx: ToolContext[Shift], number: int) -> str:
    """Read a GitHub issue: what the customer wrote and every comment on it."""
    shift = ctx.context
    refused = shift.refused(ctx)
    if refused:
        return refused
    if shift.card and number != shift.card.github:
        # Issues carry their own readers; a run may open only the customer's issue on its card.
        return f"GH#{number} is not this card's issue."
    try:
        issue = shift.issues.issue(number)
    except KeyError:
        return f"There is no issue #{number}."
    shift.keep([Source(f"GH#{number}", "github", issue["title"], issue["body"], issue["url"])])
    said = "\n".join(f"{note['author']}: {note['body']}" for note in issue["comments"]) or "No comments yet."
    head = f"[GH#{number}] {issue['title']} ({issue['state']}, opened by {issue['author']})"
    return f"{head}\n{issue['body']}\n\n{said}"


RUN_SECONDS = 90


def has_read_a_source(ctx: RunContextWrapper[Shift], agent: Agent[Shift]) -> bool:
    # A small model sometimes drafts before it reads; the tool stays hidden until Linear or a doc was read.
    return any(not source_id.startswith("GH#") for source_id in ctx.context.read)


@function_tool(is_enabled=has_read_a_source)
def request_reply(ctx: ToolContext[Shift], body: str) -> str:
    """Draft the reply to the customer on their GitHub issue. A human approves it before it is posted.
    End each sentence with the id of the issue or doc it rests on, like [EXP-41]."""
    shift = ctx.context
    refused = shift.refused(ctx)
    if refused:
        return refused
    number = shift.card.github
    if number is None:
        return "This card has no GitHub issue to reply on."
    # A promise to a customer rests on what the team wrote down; a draft citing nothing it read goes back.
    grounded = check_citations(body, shift.read)
    rests_on = [source.source_id for source in grounded.sources if source.source_id != f"GH#{number}"]
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
        return decide(shift.card, shift.issues, person=MANAGER, approve=True)
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
            "Promise only what Linear or the docs say, and end each sentence with its source id. "
            "Reading and searching are not the job: finish by calling request_reply, never with a message."
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
    number = card.github
    if card.pending:
        board.move(card, "Needs you", f"the reply on GH#{number} waits for a human")
    elif number is not None and issues.replied(number, card.card_id):
        board.move(card, "Done", f"GitHub shows the reply on GH#{number}")
    else:
        board.move(card, "Backlog", f"agent said: {said[:80]!r}; GitHub shows no reply on GH#{number}")


async def work(
    board: Board,
    card: Card,
    issues: Issues,
    readers: Readers,
    model: Model | str | None = None,
    access: Access | None = None,
    narrate: Callable[[str], None] | None = None,
    close: Callable[[Board, Card, Issues, str], None] | None = None,
) -> Card:
    board.move(card, "In flight", f"{MANAGER} picked it up")
    shift, steps = Shift(readers, access=access, issues=issues, card=card), Steps(narrate)
    kind = f"a customer issue from {card.customer}" if card.customer else "an engineer's question"
    prompt = f"Card {card.card_id}: {card.title}\nKind: {kind}\nLinked: {card.issue}"
    # The card is the trace's group, so every run on one card reads as one session in Langfuse.
    traced = RunConfig(
        workflow_name=f"{card.card_id} {card.title}",
        group_id=card.card_id,
        trace_metadata={"card": card.card_id, "user": access.user if access else readers.user},
    )
    try:
        # A host that stalls mid-stream outlasts the client timeout; a run is cut off so the demo moves on.
        result = await asyncio.wait_for(
            Runner.run(manager(model), prompt, context=shift, max_turns=12, hooks=steps, run_config=traced),
            timeout=RUN_SECONDS,
        )
        usage, succeeded = result.context_wrapper.usage, True
        said = str(result.final_output)
        card.employee = result.last_agent.name
        if not said.strip():
            # A host that fails mid-answer can still return 200 with nothing in it: a failed run, not a reply.
            succeeded, said = False, "run failed: the model returned nothing"
    except AgentsException as failure:
        run = failure.run_data
        usage, succeeded = (run.context_wrapper.usage if run else Usage()), False
        said = f"run failed: {failure}"
    except TimeoutError:
        usage, succeeded = steps.usage, False
        said = f"run failed: no answer in {RUN_SECONDS} seconds"
    except APIError as failure:
        # A host that is down or rate limited fails this run; the card goes back, the board keeps going.
        usage, succeeded = steps.usage, False
        said = f"run failed: {type(failure).__name__}"
    card.runs.append(
        Run(
            card.employee or MANAGER,
            usage.input_tokens,
            usage.output_tokens,
            price(usage),
            succeeded,
            said,
            steps.seen + [f"Gate refused: {attempt}" for attempt in shift.attempts],
            sorted(shift.read),
        )
    )
    card.log.extend(f"Gate: {attempt}" for attempt in shift.attempts)
    # The card's kind picks the check, not who took it: a question has no customer and no reply to read
    # back, so only its sources can close it.
    if card.employee in ("", MANAGER) and not card.pending:
        board.move(card, "Backlog", "no employee took the card" if succeeded else said[:80])
    elif card.customer:
        (close or settle)(board, card, issues, said)
    else:
        answer(board, card, check_citations(said, shift.read))
    return card


def answer(board: Board, card: Card, reply: Reply) -> None:
    """A question is Done when its answer cites a source the analyst read in this run.

    A cited id no tool returned is cut first, so an invented source never closes a card.
    """
    card.answer, card.sources = reply.text, [source.source_id for source in reply.sources]
    if card.sources:
        board.move(card, "Done", f"answered from {', '.join(card.sources)}")
    else:
        board.move(card, "Backlog", "the answer cited no source the analyst read")


def sign(
    board: Board, card: Card, issues: Issues, person: Person, approve: bool, access: Access | None = None
) -> None:
    """A signed-in person answers the reply waiting on a card.

    A refusal is written to the card, so the board shows it. A yes posts the reply and GitHub then closes the
    card; a no posts nothing and the card is Done, so the next run does not ask again.
    """
    refusal = may_post_to_customer(person, access)
    if refusal:
        board.move(card, card.column, f"refused: {refusal}")
        raise NotAllowed(refusal)
    said = decide(card, issues, person=person.name, approve=approve)
    if approve:
        board.move(card, "In flight", said)
        settle(board, card, issues, "approved")
    else:
        board.move(card, "Done", f"declined by {person.name}; nothing was posted")


SEED = Card("C-1", "CSV export times out on a 61,000-task workspace", "a customer", "GH#412")
QUESTION = Card("C-2", "Is the export timeout a known bug, and who owns the fix?", "", "GH#412")
REFUND = Card("C-3", "Refund the annual plan after the export timeout", "a customer", "GH#415")
CARDS = (SEED, QUESTION, REFUND)


# The demo is read off a projector, so its lines are short and each column has one colour.
TONE = {"Backlog": "2", "Needs you": "1;33", "In flight": "1;36", "Done": "1;32", "Gate": "1;31"}


def paint(text: str, tone: str) -> str:
    return f"\033[{tone}m{text}\033[0m" if os.isatty(1) else text


def narrate_to_terminal(line: str) -> None:
    """One step as it happens, dim, so a room watching a long run sees it move."""
    print(f"  {paint(line, '2')}", flush=True)


def show(card: Card) -> None:
    """One card as the room reads it: the id and title, then where it went and why, wrapped short."""
    last = card.log[-1].partition(": ")
    column, why = last[0], last[2]
    print(f"\n{paint(card.card_id, '1')}  {card.title}")
    print(f"    {card.employee or MANAGER}  {paint(column, TONE[column])}")
    for line in textwrap.wrap(why, 60):
        print(f"    {paint(line, '2')}")
    gates = [line.replace("Gate: ", "", 1) for line in card.log if line.startswith("Gate: ")]
    for gate in gates[-1:]:
        print(f"    {paint('Gate refused: ' + gate, TONE['Gate'])}")


def fresh_card(card: Card) -> Card:
    return replace(card, runs=[], log=[], sources=[])


async def demo(fresh: bool) -> None:
    if fresh:
        for name in ("board.json", "github.json"):
            (STATE / name).unlink(missing_ok=True)
    board, issues = Board.open(STATE / "board.json"), Issues.open(STATE / "github.json")
    if not board.cards:
        board.cards += [fresh_card(card) for card in CARDS]
        board.save()
    print(f"{paint('Mission Control', '1')} for KaaS: {len(board.cards)} cards")
    access = Access.from_env()
    # Every card is worked before anyone is asked, so a question is answered while a reply waits.
    for card in [card for card in board.cards if card.column == "Backlog"]:
        # The resolver reads as support staff and the analyst as an engineer, each seeing only their rows.
        user = "U_SUPPORT" if card.customer else "U_ENG"
        for _ in range(2):
            await work(
                board, card, issues, Readers.from_env(user=user), access=access, narrate=narrate_to_terminal
            )
            # A failed run is the host, not the card: work it once more. Both runs count in the cost.
            if card.runs[-1].succeeded:
                break
        show(card)
    outcome = board.cost_per_outcome
    print(f"\ncost per outcome {paint('no outcome yet' if outcome is None else f'${outcome:.4f}', '1')}")
    waiting = [card.card_id for card in board.cards if card.column == "Needs you"]
    if waiting:
        print(
            f"{', '.join(waiting)} wait for a support approver: make board, then open http://127.0.0.1:8080"
        )


def naive() -> None:
    """The same customer issue with the check off: the agent's word closes the card."""
    shutil.rmtree(NAIVE_STATE, ignore_errors=True)
    board, issues = Board.open(NAIVE_STATE / "board.json"), Issues.open(NAIVE_STATE / "github.json")
    card = fresh_card(SEED)
    board.cards.append(card)
    claim = "I replied to the customer on GH#412 with the docs workaround."
    model = ScriptedModel(
        [[function_call("transfer_to_support_resolver", {}, call_id="hand")], [assistant_message(claim)]],
        default_usage=Usage(requests=1, input_tokens=1500, output_tokens=60, total_tokens=1560),
    )

    def trust_the_word(board: Board, card: Card, issues: Issues, said: str) -> None:
        board.move(card, "Done", f"agent said: {said[:80]!r}")

    asyncio.run(work(board, card, issues, Readers("kaas", "U_SUPPORT"), model=model, close=trust_the_word))
    print(paint("Without a check: the card is Done because the agent said so", "1;36"))
    print(paint(f"  Support resolver: {claim}", "1;33"))
    print(paint(f"  {card.card_id} is in {card.column}", "1;32"))
    print(paint(f"  GitHub shows no reply on GH#{card.github}", "1;31"))


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description="Work the board, or serve it")
    cli.add_argument("--serve", action="store_true", help="serve the board on http://127.0.0.1:8080")
    cli.add_argument(
        "--fresh", action="store_true", help="start over from the fixture issues and three cards"
    )
    cli.add_argument("--naive", action="store_true", help="show the card closed on the agent's word")
    cli.add_argument("--port", type=int, default=8080)
    args = cli.parse_args()
    if args.serve:
        serve(STATE, TEAM, args.port)
    elif args.naive:
        set_tracing_disabled(True)
        naive()
    else:
        asyncio.run(demo(args.fresh))
