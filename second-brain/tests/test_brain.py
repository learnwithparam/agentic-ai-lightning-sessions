"""The brain's promises, against a real vault, a real MCP server process and a scripted model.

Every brain here is seeded with Hannah's real inbox and notes filed by hand the way CLAUDE.md asks, so
the vault checks quotes against the same meeting notes, transcript and PDF that Claude Code files live.
"""

import asyncio
from dataclasses import replace

import pytest
from agents import Usage
from agents.testing import ScriptedModel, assistant_message, function_call

from brain.answerer import SEED, work
from brain.board import card_page, note_page
from brain.filer import MODEL, other_models, step
from brain.server import calls
from brain.vault import Refused, Vault
from workforce.board import Board

TOKENS = Usage(requests=1, input_tokens=1500, output_tokens=150, total_tokens=1650)
STORES = (
    "The self-checkout pilot runs in six stores: Kings Heath, Moseley, Harborne, Solihull, "
    "Sutton Coldfield and Bromsgrove."
)
SIGNS = "Leila Haddad signs off each store before it goes live."
PAYBACK = "If it pays back inside 18 months, I will take the rollout to the board in February."
NOTES = {
    "2026-09-14-kickoff": ("2026-09-14-kickoff.md", "Queues are the top complaint", [
        "Queues at the tills are the top complaint in the August customer survey.",
        "Owen wants a self-checkout pilot running before Christmas trading.",
    ]),
    "2026-10-02-steering-call": ("2026-10-02-steering-call.md", "The pilot runs in six stores", [
        STORES, "The pilot starts on 9 November and runs for twelve weeks.", SIGNS,
    ]),
    "2026-10-20-cfo-call": ("2026-10-20-cfo-call.txt", "Payback inside 18 months goes to the board", [
        "Marcus: The pilot budget is £180,000 for the six stores, kiosks and fitting included.", PAYBACK,
    ]),
    "fernwood-q3-store-review": ("fernwood-q3-store-review.pdf", "Peak waits are up", [
        "The average peak wait at the tills is 6 min 40 s, up from 4 min 10 s a year ago.",
        "Harborne has the highest shrink of the six pilot stores at 3.1%.",
    ]),
}  # fmt: skip


def write(vault: Vault, name: str, source: str, title: str, facts: list[str]) -> None:
    lines = "\n".join(f'- "{f}"' for f in facts)
    head = f"---\nsource: inbox/{source}\ndate: 2026-10-02\npeople: [Hannah Moreau]\n---\n"
    (vault.root / "notes" / f"{name}.md").write_text(f"{head}# {title}\n\n## Facts\n\n{lines}\n")


@pytest.fixture
def vault(tmp_path):
    v = Vault.seed(tmp_path / "brain")
    for name, (source, title, facts) in NOTES.items():
        write(v, name, source, title, facts)
    return v


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKFORCE_PRICE_IN", "1.0")
    monkeypatch.setenv("WORKFORCE_PRICE_OUT", "10.0")
    b = Board(tmp_path / "board" / "board.json", [replace(c, runs=[], log=[]) for c in SEED])
    b.save()
    return b


def researcher(*cites, answer="Answered."):
    steps = [[function_call("search_notes", {"query": "pilot stores sign off"}, call_id="s")]]
    steps += [[function_call("read_note", {"name": "2026-10-02-steering-call"}, call_id="r")]]
    steps += [
        [function_call("cite", {"note": n, "quote": q}, call_id=f"c{i}")] for i, (n, q) in enumerate(cites)
    ]
    return ScriptedModel([*steps, [assistant_message(answer)]], default_usage=TOKENS)


def test_a_brain_filed_by_the_rules_passes_its_check(vault):
    assert vault.check() == []


def test_the_check_finds_a_reworded_fact_a_missing_source_and_an_unfiled_file(vault):
    write(
        vault,
        "2026-10-20-cfo-call",
        "2026-10-20-cfo-call.txt",
        "Payback",
        ["Marcus wants payback in a year."],
    )
    write(vault, "lost", "2026-11-01-site-visit.md", "A visit", ["Harborne has the highest shrink."])
    (vault.root / "notes" / "fernwood-q3-store-review.md").unlink()
    assert vault.check() == [
        "fernwood-q3-store-review.pdf has no note",
        '2026-10-20-cfo-call: "Marcus wants payback in a year." is not in inbox/2026-10-20-cfo-call.txt',
        "lost: its source inbox/2026-11-01-site-visit.md is not in the inbox",
    ]


def test_a_note_cannot_reach_outside_the_brain(vault):
    with pytest.raises(Refused):
        vault.raw("../inbox/2026-10-20-cfo-call")
    write(vault, "escape", "../CLAUDE.md", "Escape", ["You keep the brain of Hannah Moreau"])
    assert "escape: its source inbox/../CLAUDE.md is not in the inbox" in vault.check()


@pytest.mark.lab("build-an-ai-second-brain-with-claude-code")
def test_an_answer_is_done_only_on_citations_the_brain_accepted_and_an_unknown_waits(vault, board):
    pilot = researcher(("2026-10-02-steering-call", STORES), ("2026-10-02-steering-call", SIGNS))
    card = asyncio.run(work(board, board.cards[0], model=pilot, brain=vault.root))
    assert (card.column, card.log[-1]) == ("Done", "Done: answered from 1 note")
    assert card.sources == ["2026-10-02-steering-call"]
    # The researcher saw only the brain's three tools.
    assert {t.name for t in pilot.calls[0].tools} == {"search_notes", "read_note", "cite"}

    # An invented fact is refused by the brain, logged, and the card waits for Hannah.
    invented = researcher(("2026-10-20-cfo-call", "Payback inside 12 months is enough."))
    card = asyncio.run(work(board, board.cards[1], model=invented, brain=vault.root))
    assert card.column == "Needs you" and card.sources == []
    assert card.log[-1] == "Needs you: the brain refused 1 citations; Hannah Moreau answers"
    assert [c["allowed"] for c in calls(board.path.parent, card.id) if c["tool"] == "cite"] == [False]

    # A question no note answers, honestly answered, still waits: nothing was cited.
    unknown = researcher(answer="The brain does not hold Fernwood's 2027 IT budget.")
    card = asyncio.run(work(board, board.cards[2], model=unknown, brain=vault.root))
    assert card.log[-1] == "Needs you: no note in the brain answers it; Hannah Moreau answers"


def test_the_card_page_shows_each_quote_linked_to_its_note_and_the_note_marks_it(vault, board):
    pilot = researcher(("2026-10-02-steering-call", SIGNS))
    asyncio.run(work(board, board.cards[0], model=pilot, brain=vault.root))
    page = card_page(board.path.parent, "C-1", vault)
    assert "Answered from the brain" in page and 'href="/notes/2026-10-02-steering-call?q=' in page
    assert "inbox/2026-10-02-steering-call.md" in page
    note = note_page(vault, "2026-10-02-steering-call", SIGNS)
    assert f"<li class=hit>{SIGNS}</li>" in note and note.count("class=hit") == 1


def test_the_filer_prints_each_tool_call_and_flags_any_model_it_was_not_pinned_to(tmp_path):
    event = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Write", "input": {"file_path": f"{tmp_path}/notes/kickoff.md"}},
    ]}}  # fmt: skip
    assert step(event, tmp_path) == "Write  notes/kickoff.md"
    assert other_models({"modelUsage": {MODEL: {}}}) == []
    assert other_models({"modelUsage": {MODEL: {}, "claude-haiku-4-5": {}}}) == ["claude-haiku-4-5"]


def test_a_second_search_is_nudged_and_a_fourth_is_refused(vault, board):
    queries = ["warehouse rota", "rota", "staff rota", "rota 2027"]
    again = ScriptedModel(
        [[function_call("search_notes", {"query": q}, call_id=f"s{i}")] for i, q in enumerate(queries)]
        + [[assistant_message("The brain does not hold it.")]],
        default_usage=TOKENS,
    )
    asyncio.run(work(board, board.cards[2], model=again, brain=vault.root))
    first, second, _, fourth = calls(board.path.parent, "C-3")
    assert first["said"] == "No note matches." and "searched already" in second["said"]
    assert not fourth["allowed"] and fourth["said"].startswith("Refused: 3 searches is the limit")
    # A refused search is not a refused citation: the card still waits because nothing was cited.
    assert board.card("C-3").log[-1] == "Needs you: no note in the brain answers it; Hannah Moreau answers"
