"""The brain's promises, against a real vault, a real MCP server process and a scripted model.

Every brain here is seeded with Hannah's real inbox and notes filed by hand the way CLAUDE.md asks, so
the vault checks quotes against the same meeting notes, transcript and PDF that Claude Code files live.
"""

import asyncio
from dataclasses import replace

import pytest
from agents import Usage
from agents.testing import ScriptedModel, assistant_message, function_call
from aiohttp.test_utils import TestClient, TestServer

from brain.answerer import SEED, TEAM, tidy, work
from brain.board import PEOPLE_FILE, app, card_page, note_page
from brain.filer import FILED, MODEL, file_offline, other_models, step
from brain.server import calls
from brain.vault import Refused, Vault
from workforce.board import Board
from workforce.people import People, Person

HANNAH = Person("Hannah Moreau", "consultant")
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
    "q3-store-review": ("q3-store-review.pdf", "Peak waits are up", [
        "The average peak wait at the tills is 6 min 40 s, up from 4 min 10 s a year ago.",
        "Harborne has the highest shrink of the six pilot stores at 3.1%.",
    ]),
}  # fmt: skip


def write(vault: Vault, name: str, source: str, title: str, facts: list[str]) -> None:
    lines = "\n".join(f'- "{fact}"' for fact in facts)
    head = f"---\nsource: inbox/{source}\ndate: 2026-10-02\npeople: [Hannah Moreau]\n---\n"
    (vault.root / "notes" / f"{name}.md").write_text(f"{head}# {title}\n\n## Facts\n\n{lines}\n")


@pytest.fixture
def vault(tmp_path):
    seeded = Vault.seed(tmp_path / "brain")
    for name, (source, title, facts) in NOTES.items():
        write(seeded, name, source, title, facts)
    return seeded


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKFORCE_PRICE_IN", "1.0")
    monkeypatch.setenv("WORKFORCE_PRICE_OUT", "10.0")
    seeded = Board(tmp_path / "board" / "board.json", [replace(card, runs=[], log=[]) for card in SEED])
    seeded.save()
    return seeded


def researcher(*cites, answer="Answered."):
    steps = [[function_call("search_notes", {"query": "pilot stores sign off"}, call_id="s")]]
    steps += [[function_call("read_note", {"name": "2026-10-02-steering-call"}, call_id="r")]]
    steps += [
        [function_call("cite", {"note": note, "quote": quote}, call_id=f"c{index}")]
        for index, (note, quote) in enumerate(cites)
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
    (vault.root / "notes" / "q3-store-review.md").unlink()
    assert vault.check() == [
        "q3-store-review.pdf has no note",
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
    assert {tool.name for tool in pilot.calls[0].tools} == {"search_notes", "read_note", "cite"}

    # An invented fact is refused by the brain, logged, and the card waits for Hannah.
    invented = researcher(("2026-10-20-cfo-call", "Payback inside 12 months is enough."))
    card = asyncio.run(work(board, board.cards[1], model=invented, brain=vault.root))
    assert card.column == "Needs you" and card.sources == []
    assert card.log[-1] == "Needs you: the brain refused 1 citations; Hannah Moreau answers"
    assert [call["allowed"] for call in calls(board.path.parent, card.card_id) if call["tool"] == "cite"] == [
        False
    ]

    # A question no note answers, honestly answered, still waits: nothing was cited.
    unknown = researcher(answer="The brain does not hold the chain's 2027 IT budget.")
    card = asyncio.run(work(board, board.cards[2], model=unknown, brain=vault.root))
    assert card.log[-1] == "Needs you: no note in the brain answers it; Hannah Moreau answers"


def test_the_card_page_shows_each_quote_linked_to_its_note_and_the_note_marks_it(vault, board):
    pilot = researcher(("2026-10-02-steering-call", SIGNS))
    asyncio.run(work(board, board.cards[0], model=pilot, brain=vault.root))
    page = card_page(board.path.parent, "C-1", vault, HANNAH)
    assert "Answered from the brain" in page and 'href="/notes/2026-10-02-steering-call?q=' in page
    assert "inbox/2026-10-02-steering-call.md" in page
    note = note_page(vault, "2026-10-02-steering-call", SIGNS, HANNAH)
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
        [
            [function_call("search_notes", {"query": text}, call_id=f"s{index}")]
            for index, text in enumerate(queries)
        ]
        + [[assistant_message("The brain does not hold it.")]],
        default_usage=TOKENS,
    )
    asyncio.run(work(board, board.cards[2], model=again, brain=vault.root))
    rows = calls(board.path.parent, "C-3")
    first = rows[0]
    second = rows[1]
    fourth = rows[3]
    assert first["said"] == "No note matches." and "searched already" in second["said"]
    assert not fourth["allowed"] and fourth["said"].startswith("Refused: 3 searches is the limit")
    # A refused search is not a refused citation: the card still waits because nothing was cited.
    assert board.card("C-3").log[-1] == "Needs you: no note in the brain answers it; Hannah Moreau answers"


def test_the_board_strips_the_inline_citation_marks_models_add():
    said = "Six stores.【steering】 Leila signs off.[Cited 2026-10-02-steering] Kings Heath [kickoff] (cite)"
    assert tidy(said) == "Six stores. Leila signs off. Kings Heath"


PASSPHRASES = {"Hannah Moreau": "hannah-pass", "Ravi Shah": "ravi-pass"}
ANSWER = "The 2027 IT budget is £1.2m, agreed with Marcus Bell by phone on 27 October."
SOURCE = "Phone call with Marcus Bell, 27 October 2026"


@pytest.fixture
def people(monkeypatch):
    monkeypatch.setenv("BOARD_SECRET", "test-secret")
    monkeypatch.setenv("BOARD_PASSPHRASE_HANNAH_MOREAU", PASSPHRASES["Hannah Moreau"])
    monkeypatch.setenv("BOARD_PASSPHRASE_RAVI_SHAH", PASSPHRASES["Ravi Shah"])
    return People(PEOPLE_FILE, "test-secret")


async def sign_in(client, name):
    reply = await client.post(
        "/login", data={"name": name, "passphrase": PASSPHRASES[name]}, allow_redirects=False
    )
    assert reply.status == 303


def waiting_board(board, vault):
    asyncio.run(
        work(board, board.cards[2], model=researcher(answer="The brain has no IT budget."), brain=vault.root)
    )
    assert board.card("C-3").column == "Needs you"


@pytest.mark.lab("build-an-ai-second-brain-with-claude-code")
def test_only_hannah_answers_for_the_client(vault, board, people):
    waiting_board(board, vault)

    async def scenario():
        async with TestClient(TestServer(app(board.path.parent, TEAM, people, vault.root))) as client:
            signed_out = await client.get("/cards/C-3", allow_redirects=False)
            assert (signed_out.status, signed_out.headers["Location"]) == (303, "/login")
            wrong = await client.post("/login", data={"name": "Ravi Shah", "passphrase": "no"})
            assert wrong.status == 401
            await sign_in(client, "Ravi Shah")
            forged = {"answer": ANSWER, "source": SOURCE, "by": "Hannah Moreau"}
            await client.post("/cards/C-3/answer", data=forged, allow_redirects=False)
            assert board.__class__.open(board.path).card("C-3").column == "Needs you"
            refusals = [call for call in calls(board.path.parent, "C-3") if not call["allowed"]]
            assert [call["by"] for call in refusals] == ["Ravi Shah"]
            await client.post("/logout", allow_redirects=False)
            await sign_in(client, "Hannah Moreau")
            await client.post("/cards/C-3/answer", data={"answer": ANSWER, "source": SOURCE})

    asyncio.run(scenario())
    card = Board.open(board.path).card("C-3")
    assert (card.column, card.log[-1]) == ("Done", "Done: Hannah Moreau answered, filed as answer-c-3")
    assert vault.check() == [] and ANSWER in vault.note("answer-c-3").facts[0]


def test_an_answer_without_a_source_is_refused(vault):
    with pytest.raises(Refused, match="needs both the answer"):
        vault.file_answer(
            "C-3", "IT budget?", "Marcus", ANSWER, " ", "Hannah Moreau", "Hannah Moreau", "2026-10-27"
        )


def test_a_filed_answer_passes_the_check(vault):
    note = vault.file_answer(
        "C-3", "IT budget?", "Marcus", ANSWER, SOURCE, "Hannah Moreau", "Hannah Moreau", "2026-10-27"
    )
    assert vault.check() == [] and note.name == "answer-c-3"


def test_offline_filing_passes_the_same_vault_check(tmp_path, capsys):
    assert file_offline(tmp_path / "brain") == 0
    out = capsys.readouterr().out
    assert "4 notes, 25 facts, every one found in its source" in out


def test_the_committed_filing_quotes_the_inbox_and_a_drifted_quote_fails(tmp_path, capsys):
    assert sorted(note.stem for note in FILED.glob("*.md")) == sorted(NOTES)
    root = tmp_path / "brain"
    file_offline(root)
    kept = root / "notes" / "2026-10-20-cfo-call.md"
    kept.write_text(kept.read_text().replace("£180,000", "£190,000"))
    assert Vault(root).check() != []
