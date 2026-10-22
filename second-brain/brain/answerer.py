"""The researcher: an AI employee that answers questions on Hannah's client work from her brain alone.

Owen, Leila and Marcus ask on the board. The researcher reaches the brain only through its MCP server:
it searches the notes, reads the ones that match, and cites each fact its answer uses. The card is Done
only when the brain accepted at least one citation, and every accepted citation is a quote in a note
whose source Hannah captured. A question the brain cannot answer waits for Hannah. `settle` reads the
server's call log, never the model's answer. `--naive` asks the same questions with no brain.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import shutil
import sys
from dataclasses import replace
from pathlib import Path

from agents import Agent, AgentsException, Model, ModelSettings, Runner, Usage
from agents.mcp import MCPServerStdio
from openai import APIError

from brain.server import TOOLS, calls, citations
from brain.vault import BRAIN, Vault
from workforce import default_model
from workforce.board import Board, Card, Role, Run, price
from workforce.mission import Steps, paint

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / ".board"
RESEARCHER = "Researcher"
OWNER = "Hannah Moreau"
TEAM = (
    Role(
        RESEARCHER,
        "An answer to a client question in which every fact cites a note in Hannah's brain",
        "Answers whose citations all lead to a file Hannah captured",
        "Answer from anything but the brain",
        TOOLS,
    ),
)
SEED = (
    Card(
        "C-1",
        "Which stores are in the self-checkout pilot, and who signs each one off?",
        "Owen Pryce",
        "Pilot",
    ),
    Card(
        "C-2", "What payback does Marcus need before the rollout goes to the board?", "Leila Haddad", "Pilot"
    ),
    Card("C-3", "What is Fernwood's IT budget for 2027?", "Marcus Bell", "Budget"),
)
INSTRUCTIONS = (
    f"You answer questions on {OWNER}'s client work for Fernwood Grocers from her brain, and from nothing "
    "else. Search the brain once with search_notes. Cite every fact your answer uses with cite, copying the "
    "fact exactly as the note quotes it. Then answer in two or three plain sentences. If no fact answers the "
    "question, say the brain does not hold it and cite nothing."
)


def server(brain: Path, state: Path, card: Card) -> MCPServerStdio:
    """The brain's MCP server, one process per card. It inherits no keys, so a tool cannot spend any."""
    args = ["-m", "brain.server", "--brain", str(brain), "--state", str(state), "--card", card.id]
    return MCPServerStdio(
        {"command": sys.executable, "args": args, "cwd": str(ROOT)},
        name="brain",
        client_session_timeout_seconds=150,
    )


def researcher(brain: MCPServerStdio, model: Model | str) -> Agent:
    # A small model handed a question may answer from memory; its first move must be a search. One call a
    # turn: allowed parallel calls, it once sent 547 searches in a single reply.
    return Agent(
        name=RESEARCHER,
        instructions=INSTRUCTIONS,
        mcp_servers=[brain],
        model=model,
        model_settings=ModelSettings(tool_choice="required", parallel_tool_calls=False),
    )


def tidy(text: str) -> str:
    """The answer as the board prints it: models space numbers with narrow and no-break spaces, and some
    mark their citations inline, which the board already shows under the answer."""
    text = re.sub(r"\s*(【[^】]*】|\[[a-z0-9-]+\]|\(cite\))", "", text)
    for odd in ("\u202f", "\u00a0", "\u2009"):
        text = text.replace(odd, " ")
    return text.replace("\u2011", "-").replace(" \u2014 ", ", ").replace("\u2014", ", ").strip()


def settle(board: Board, card: Card, state: Path) -> None:
    cited = citations(state, card.id)
    refused = [c for c in calls(state, card.id) if c["tool"] == "cite" and not c["allowed"]]
    if cited:
        notes = list(dict.fromkeys(c["note"] for c in cited))
        card.sources = notes
        board.move(card, "Done", f"answered from {len(notes)} {'note' if len(notes) == 1 else 'notes'}")
    elif refused:
        board.move(card, "Needs you", f"the brain refused {len(refused)} citations; {OWNER} answers")
    else:
        board.move(card, "Needs you", f"no note in the brain answers it; {OWNER} answers")


async def work(board: Board, card: Card, model: Model | str | None = None, brain: Path = BRAIN) -> Card:
    model = model or default_model()
    state = board.path.parent
    board.move(card, "In flight", f"{RESEARCHER} picked it up")
    card.employee = RESEARCHER
    steps = Steps()
    async with server(brain, state, card) as mcp:
        try:
            result = await Runner.run(
                researcher(mcp, model), f"{card.customer} asks: {card.title}", max_turns=12, hooks=steps
            )
            usage, ok, said = result.context_wrapper.usage, True, str(result.final_output)
        except AgentsException as e:
            run = e.run_data
            usage, ok, said = (run.context_wrapper.usage if run else Usage()), False, f"run failed: {e}"
        except APIError as e:
            usage, ok, said = steps.usage, False, f"run failed: {type(e).__name__}"
    card.answer = tidy(said) if ok else ""
    lines = [f"{c['tool']} {', '.join(c['args'].values())[:70]}" for c in calls(state, card.id)]
    refused = [
        f"Brain refused: {c['said'].removeprefix('Refused: ')}"
        for c in calls(state, card.id)
        if not c["allowed"]
    ]
    card.runs.append(Run(RESEARCHER, usage.input_tokens, usage.output_tokens, price(usage), ok, said, lines))
    card.log.extend(f"Gate: {r.removeprefix('Brain refused: ')}" for r in refused)
    settle(board, card, state)
    return card


def report(board: Board, card: Card) -> None:
    """One question as the room reads it off a projector: each brain call, the answer, where it landed."""
    print(paint(f"{card.customer} asks: {card.title}", "1"))
    for c in calls(board.path.parent, card.id):
        args = ", ".join(c["args"].values())
        line = f"  {c['tool']}({args[:64]}{'...' if len(args) > 64 else ''})"
        print(line if c["allowed"] else paint(line + "  refused", "1;31"))
    if card.column == "Done":
        print("  " + card.answer.strip().replace("\n", "\n  "))
    tone = "1;32" if card.column == "Done" else "1;33"
    print(paint(f"{card.id} {card.column}: {card.log[-1].split(': ', 1)[-1]}", tone))


async def naive(model: Model | str) -> None:
    """The same questions to the same model with no brain: what a chat window says."""
    bare = Agent(name=RESEARCHER, instructions=f"You answer questions on {OWNER}'s client work.", model=model)
    for card in SEED[:2]:
        print(paint(f"{card.customer} asks: {card.title}", "1"))
        result = await Runner.run(bare, card.title)
        print("  " + str(result.final_output).strip().replace("\n", "\n  "))


async def demo(model: Model | str) -> None:
    shutil.rmtree(STATE, ignore_errors=True)
    board = Board(STATE / "board.json", [replace(c, runs=[], log=[]) for c in SEED])
    board.save()
    for card in board.cards:
        await work(board, card, model)
        report(board, card)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--naive", action="store_true", help="ask the questions with no brain")
    p.add_argument("--serve", action="store_true", help="serve the board from the last run")
    p.add_argument("--port", type=int, default=8093)
    a = p.parse_args()
    if a.serve:
        from brain.board import serve

        serve(STATE, TEAM, a.port)
        return
    if a.naive:
        asyncio.run(naive(default_model()))
        return
    if problems := Vault(BRAIN).check() if (BRAIN / "notes").is_dir() else ["no brain yet"]:
        sys.exit("The brain is not ready; run make file first.\n" + "\n".join(problems))
    asyncio.run(demo(default_model()))


if __name__ == "__main__":
    main()
