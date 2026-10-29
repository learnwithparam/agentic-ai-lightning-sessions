"""Mission Control: the manager hands KaaS's GitHub issue to the support resolver, the reply waits for a
signed-in support approver, and the card is Done only when GitHub shows the reply. Real SDK runs with a
scripted model, so no keys and no spend."""

import asyncio
import json
import os
from dataclasses import replace
from typing import NamedTuple

import pytest
from agents import Usage
from agents.testing import ScriptedModel, assistant_message, function_call
from aiohttp.test_utils import TestClient, TestServer

from workforce.board import PEOPLE_FILE, Board, Card, app, detail, render, team
from workforce.issues import Issues, marker
from workforce.mission import (
    QUESTION,
    REFUND,
    SEED,
    TEAM,
    NotAllowed,
    decide,
    for_customer,
    manager,
    naive,
    sign,
    work,
)
from workforce.people import People, Person
from workforce.sources import Readers

TOKENS = Usage(requests=1, input_tokens=2000, output_tokens=200, total_tokens=2200)
DRAFT = (
    "Sorry about the failing export. A large export runs past a 30 second limit [EXP-41]. Until the fix "
    "ships, export one quarter at a time or use POST /v1/exports, which emails a download link [doc:exports]."
)


class Desk(NamedTuple):
    board: Board
    card: Card
    issues: Issues


@pytest.fixture
def desk(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKFORCE_PRICE_IN", "1.0")
    monkeypatch.setenv("WORKFORCE_PRICE_OUT", "10.0")
    board, issues = Board.open(tmp_path / "board.json"), Issues.open(tmp_path / "github.json")
    board.cards.append(replace(SEED, runs=[], log=[]))
    return Desk(board, board.cards[0], issues)


def support():
    return Readers("kaas", "U_SUPPORT")


def resolver_drafts(body=DRAFT, then=None):
    return ScriptedModel(
        [
            [function_call("transfer_to_support_resolver", {}, call_id="h1")],
            [function_call("read_issue", {"number": 412}, call_id="r1")],
            [
                function_call("search_linear", {"query": "csv export timeout"}, call_id="s1"),
                function_call("search_docs", {"query": "large export workaround"}, call_id="s2"),
            ],
            [function_call("request_reply", {"body": body}, call_id="d1")],
            [assistant_message(then or "The reply is waiting for approval.")],
        ],
        default_usage=TOKENS,
    )


PARAM = Person("Param", "support approver", "param")
SAM = Person("Sam", "contractor", "sam")


@pytest.mark.lab("build-an-ai-mission-control-app")
def test_a_customer_issue_is_done_only_after_a_human_approves_and_github_shows_the_reply(desk):
    board = desk.board
    card = desk.card
    issues = desk.issues
    model = resolver_drafts()

    asyncio.run(work(board, card, issues, support(), model=model))

    # The manager handed it to the employee hired for customers, which read the issue and the plan.
    assert card.employee == "Support resolver"
    assert "61,000 tasks" in str(model.calls[2].input) and "release 2.14" in str(model.calls[3].input)
    # A reply to a customer cannot be unsent: nothing is posted until a human decides.
    assert card.column == "Needs you" and issues.comments(412) == []
    assert card.pending["rests_on"] == ["EXP-41", "doc:exports"]
    # The customer never sees internal ids; the approver sees what the reply rests on.
    assert "[" not in card.pending["body"] and "emails a download link." in card.pending["body"]

    sign(board, card, issues, PARAM, True)

    [posted] = Issues.open(issues.path).comments(412)
    assert posted["approved_by"] == "Param" and "one quarter at a time" in posted["body"]
    assert card.column == "Done" and card.log[-1] == "Done: GitHub shows the reply on GH#412"
    # Five model calls at 2,000 in and 200 out, at $1 and $10 per million tokens.
    assert card.cost == pytest.approx(5 * (2000 * 1.0 + 200 * 10.0) / 1e6)
    assert board.cost_per_outcome == pytest.approx(card.cost)
    # The board on disk is the board on the page, and the Done card carries GitHub's copy of the reply.
    page = render(Board.open(board.path), {card.card_id: posted}, workspace="KaaS")
    assert "CSV export times out on a 61,000-task workspace" in page and f"${card.cost:.4f}" in page
    assert "GitHub shows the reply" in page and "Approved by</dt><dd>Param" in page


def test_no_ai_employee_can_approve_a_reply_an_ai_employee_drafted(desk):
    board = desk.board
    card = desk.card
    issues = desk.issues
    asyncio.run(work(board, card, issues, support(), model=resolver_drafts()))
    model = ScriptedModel(
        [
            [function_call("approve_pending", {}, call_id="a1")],
            [assistant_message("I could not approve it; it is waiting for you.")],
        ],
        default_usage=TOKENS,
    )
    card.column = "Backlog"

    asyncio.run(work(board, card, issues, support(), model=model))

    assert issues.comments(412) == [] and card.column == "Needs you"
    assert any("Manager may not approve" in line for line in card.log)
    for agent in ("Manager", "Support resolver", "Engineering analyst"):
        with pytest.raises(NotAllowed):
            decide(card, issues, person=agent, approve=True)
    assert issues.comments(412) == []


def test_an_agent_saying_it_replied_does_not_move_the_card_to_done(desk):
    board = desk.board
    card = desk.card
    issues = desk.issues
    model = ScriptedModel(
        [
            [function_call("transfer_to_support_resolver", {}, call_id="h1")],
            [assistant_message("I replied to the customer on GitHub. All done.")],
        ],
        default_usage=TOKENS,
    )

    asyncio.run(work(board, card, issues, support(), model=model))

    assert card.column == "Backlog" and "GitHub shows no reply on GH#412" in card.log[-1]
    # It cost money and produced no outcome, so there is no cost per outcome yet.
    assert card.cost > 0 and board.cost_per_outcome is None


def test_a_draft_that_cites_nothing_it_read_is_sent_back(desk):
    board = desk.board
    card = desk.card
    issues = desk.issues
    model = resolver_drafts(body="We will ship the fix next Monday [EXP-99].", then="Could not draft it.")

    asyncio.run(work(board, card, issues, support(), model=model))

    assert card.pending is None and "Refused: cite the Linear issue" in str(model.calls[4].input)
    assert card.column == "Backlog"


def test_a_repeated_approval_posts_once(desk):
    board = desk.board
    card = desk.card
    issues = desk.issues
    asyncio.run(work(board, card, issues, support(), model=resolver_drafts()))

    issues.reply(412, "Hello", card.card_id, approved_by="Param")
    issues.reply(412, "Hello", card.card_id, approved_by="Param")
    assert len(Issues.open(issues.path).comments(412)) == 1


def test_failed_runs_count_in_cost_per_outcome(desk):
    board = desk.board
    card = desk.card
    issues = desk.issues
    failed = ScriptedModel(
        [[function_call("transfer_to_support_resolver", {}, call_id="h1")]] * 13, default_usage=TOKENS
    )
    asyncio.run(work(board, card, issues, support(), model=failed))
    assert card.runs[-1].succeeded is False and card.cost > 0

    card.column = "Backlog"
    asyncio.run(work(board, card, issues, support(), model=resolver_drafts()))
    sign(board, card, issues, PARAM, True)

    assert card.column == "Done"
    assert board.cost_per_outcome == pytest.approx(sum(run.cost for run in card.runs))
    assert board.cost_per_outcome > card.runs[-1].cost


def test_the_card_page_shows_each_run_step_by_step(desk):
    board = desk.board
    card = desk.card
    issues = desk.issues
    asyncio.run(work(board, card, issues, support(), model=resolver_drafts()))

    [run] = card.runs
    assert run.steps == [
        "Manager handed the card to Support resolver",
        "Support resolver called read_issue",
        "Support resolver called search_linear",
        "Support resolver called search_docs",
        "Support resolver called request_reply",
    ]
    assert run.read == ["EXP-41", "GH#412", "doc:exports"]
    page = detail(card, None, workspace="KaaS")
    assert "Post this reply on GH#412?" in page and "<li>Support resolver called read_issue</li>" in page


def test_every_employee_on_the_team_page_has_an_outcome_a_metric_and_a_must_never(desk):
    board = desk.board
    page = team(board, TEAM, workspace="KaaS")
    for role in TEAM:
        assert role.outcome and role.metric and role.never
        assert role.name in page and role.never in page


def test_internal_ids_are_cut_from_what_the_customer_reads():
    assert for_customer("Fixed in 2.14 [EXP-41]. Export by quarter (Settings) [doc:exports].") == (
        "Fixed in 2.14. Export by quarter (Settings)."
    )


def analyst_answers(text):
    return ScriptedModel(
        [
            [function_call("transfer_to_engineering_analyst", {}, call_id="h1")],
            [function_call("search_linear", {"query": "export timeout"}, call_id="s1")],
            [assistant_message(text)],
        ],
        default_usage=TOKENS,
    )


@pytest.mark.lab("build-an-ai-mission-control-app")
def test_a_question_card_is_done_only_when_its_answer_cites_a_source_the_analyst_read(desk):
    board = desk.board
    issues = desk.issues
    card = replace(QUESTION, runs=[], log=[], sources=[])
    board.cards.append(card)

    said = "**Known:** the export hits the 30 second limit; the Data squad owns it [EXP-41]. See [GH#999]."
    asyncio.run(work(board, card, issues, Readers("kaas", "U_ENG"), model=analyst_answers(said)))

    # The manager handed the question to the analyst, which read Linear; the id it never read is cut.
    assert card.employee == "Engineering analyst" and card.column == "Done"
    assert card.sources == ["EXP-41"] and "GH#999" not in card.answer
    page = render(board, workspace="KaaS")
    assert "Answered from sources it read" in page and "<p>Known: the export" in page
    assert issues.comments(412) == []

    card.column = "Backlog"
    asyncio.run(
        work(board, card, issues, Readers("kaas", "U_ENG"), model=analyst_answers("It is fixed [GH#1]."))
    )
    assert card.column == "Backlog" and "cited no source" in card.log[-1]


def test_a_card_the_manager_keeps_goes_back_to_backlog(desk):
    board = desk.board
    issues = desk.issues
    card = replace(QUESTION, runs=[], log=[], sources=[])
    board.cards.append(card)
    itself = ScriptedModel([[assistant_message("Yes, it is a known bug [GH#412].")]], default_usage=TOKENS)

    asyncio.run(work(board, card, issues, Readers("kaas", "U_ENG"), model=itself))
    assert card.column == "Backlog" and card.log[-1] == "Backlog: no employee took the card"


def test_an_empty_reply_is_a_failed_run_and_not_an_answer(desk):
    board = desk.board
    issues = desk.issues
    card = replace(QUESTION, runs=[], log=[], sources=[])
    board.cards.append(card)
    empty = ScriptedModel([[assistant_message("")]], default_usage=TOKENS)

    asyncio.run(work(board, card, issues, Readers("kaas", "U_ENG"), model=empty))

    assert card.column == "Backlog" and card.runs[-1].succeeded is False
    assert card.log[-1] == "Backlog: run failed: the model returned nothing"


def test_a_host_that_fails_mid_run_fails_the_run_and_keeps_what_it_spent(desk):
    import httpx2
    from openai import APIConnectionError

    board = desk.board

    card = desk.card

    issues = desk.issues
    down = APIConnectionError(request=httpx2.Request("POST", "https://openrouter.ai/api/v1/chat/completions"))
    model = ScriptedModel(
        [[function_call("transfer_to_support_resolver", {}, call_id="h1")], down], default_usage=TOKENS
    )

    asyncio.run(work(board, card, issues, support(), model=model))

    assert card.column == "Backlog" and card.runs[-1].succeeded is False
    assert card.log[-1] == "Backlog: run failed: APIConnectionError" and card.cost > 0


def test_a_comment_anyone_could_write_with_the_cards_marker_does_not_close_it(desk, tmp_path):
    board = desk.board
    card = desk.card
    issues = desk.issues
    asyncio.run(work(board, card, issues, support(), model=resolver_drafts()))
    rows = json.loads((tmp_path / "github.json").read_text())
    forged = {"author": "someone-else", "body": f"fixed!\n\n{marker('C-1')}", "url": "x"}
    next(row for row in rows if row["number"] == 412)["comments"].append(forged)
    (tmp_path / "github.json").write_text(json.dumps(rows))

    assert issues.replied(412, "C-1") is None


def test_a_run_may_read_only_the_issue_on_its_card(desk):
    board = desk.board
    card = desk.card
    issues = desk.issues
    model = ScriptedModel(
        [
            [function_call("transfer_to_support_resolver", {}, call_id="h1")],
            [function_call("read_issue", {"number": 398}, call_id="r1")],
            [assistant_message("Done.")],
        ],
        default_usage=TOKENS,
    )

    asyncio.run(work(board, card, issues, support(), model=model))

    assert "GH#398 is not this card's issue." in str(model.calls[2].input)


@pytest.fixture
def people(monkeypatch):
    monkeypatch.setenv("BOARD_PASSPHRASE_PARAM", "param-passphrase")
    monkeypatch.setenv("BOARD_PASSPHRASE_SAM", "sam-passphrase")
    monkeypatch.setenv("BOARD_SECRET", "a-secret-for-this-test")
    return People(PEOPLE_FILE, os.environ["BOARD_SECRET"])


async def sign_in(client, name: str, passphrase: str) -> None:
    reply = await client.post("/login", data={"name": name, "passphrase": passphrase}, allow_redirects=False)
    assert reply.status == 303


def waiting_board(tmp_path, card_template, body=DRAFT):
    board, issues = Board.open(tmp_path / "board.json"), Issues.open(tmp_path / "github.json")
    card = replace(card_template, runs=[], log=[])
    board.cards.append(card)
    asyncio.run(work(board, card, issues, support(), model=resolver_drafts(body=body)))
    return card, issues


@pytest.mark.lab("build-an-ai-mission-control-app")
def test_only_a_signed_in_support_approver_can_post_a_reply(tmp_path, people):
    card, issues = waiting_board(tmp_path, SEED)

    async def scenario() -> None:
        async with TestClient(TestServer(app(tmp_path, TEAM, people), host="127.0.0.1")) as client:
            for path in ("/", "/team", "/cards/C-1"):
                reply = await client.get(path, allow_redirects=False)
                assert reply.headers["Location"] == "/login"
            denied = await client.post("/cards/C-1/decide", data={"yes": "1"}, allow_redirects=False)
            assert denied.headers["Location"] == "/login"
            wrong = await client.post("/login", data={"name": "Param", "passphrase": "guess"})
            assert wrong.status == 401
            # Sam is signed in and presses Approve, naming Param in the form: the form is ignored.
            await sign_in(client, "Sam", "sam-passphrase")
            forged = {"yes": "1", "by": "Param"}
            sent = await client.post("/cards/C-1/decide", data=forged, allow_redirects=False)
            assert sent.status == 303
            page = await (await client.get("/cards/C-1")).text()
            assert "only a support approver can post to a customer" in page and 'class="refusal"' in page
            assert Issues.open(issues.path).comments(412) == []
            await client.post("/logout", allow_redirects=False)
            await sign_in(client, "Param", "param-passphrase")
            await client.post("/cards/C-1/decide", data={"yes": "1"}, allow_redirects=False)

    asyncio.run(scenario())

    [posted] = Issues.open(issues.path).comments(412)
    assert posted["approved_by"] == "Param"
    assert Board.open(tmp_path / "board.json").card("C-1").column == "Done"


@pytest.mark.lab("build-an-ai-mission-control-app")
def test_a_declined_reply_is_never_posted(tmp_path, people):
    card, issues = waiting_board(tmp_path, REFUND)
    board = Board.open(tmp_path / "board.json")
    card = board.card("C-3")

    sign(board, card, issues, PARAM, False)

    assert Issues.open(issues.path).comments(415) == []
    assert card.column == "Done" and card.log[-1] == "Done: declined by Param; nothing was posted"
    # Done, so the next run does not ask again.
    assert card.pending is None


def test_without_a_check_the_card_is_done_on_the_agents_word(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("workforce.mission.NAIVE_STATE", tmp_path)

    naive()

    out = capsys.readouterr().out
    assert "C-1 is in Done" in out and "GitHub shows no reply on GH#412" in out
    assert Issues.open(tmp_path / "github.json").comments(412) == []


def test_the_demo_starts_no_server(monkeypatch):
    import workforce.board as board_module

    monkeypatch.setattr(board_module, "serve", lambda *args, **kwargs: pytest.fail("demo started a server"))
    import inspect

    from workforce import mission

    assert "serve(" not in inspect.getsource(mission.demo)


def test_every_employee_the_manager_hands_to_must_act_before_it_answers():
    # A small model handed a card answered with its own name and drafted nothing (21 Oct 2026).
    for employee in manager("scripted").handoffs:
        assert employee.model_settings.tool_choice == "required", employee.name


def test_one_hung_call_fails_the_run_in_a_minute(monkeypatch):
    from workforce import default_model

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    client = default_model()._client
    assert (client.timeout, client.max_retries) == (60, 0)
