"""Mission Control: the manager hands Globex's GitHub issue to the support resolver, the reply waits for a
human, and the card is Done only when GitHub shows the reply. Real SDK runs with a scripted model and a
fake approver, so no keys and no spend."""

import asyncio
import json
from dataclasses import replace

import pytest
from agents import Usage
from agents.testing import ScriptedModel, assistant_message, function_call
from aiohttp.test_utils import TestClient, TestServer

from workforce.board import Board, app, detail, render, team
from workforce.issues import Issues, marker
from workforce.mission import (
    QUESTION,
    SEED,
    TEAM,
    NotAllowed,
    ask_human,
    decide,
    for_customer,
    on_board,
    work,
)
from workforce.sources import Readers
from workforce.telegram import Telegram

TOKENS = Usage(requests=1, input_tokens=2000, output_tokens=200, total_tokens=2200)
DRAFT = (
    "Sorry about the failing export. A large export runs past a 30 second limit [EXP-41]. Until the fix "
    "ships, export one quarter at a time or use POST /v1/exports, which emails a download link [doc:exports]."
)


@pytest.fixture
def desk(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKFORCE_PRICE_IN", "1.0")
    monkeypatch.setenv("WORKFORCE_PRICE_OUT", "10.0")
    board, issues = Board.open(tmp_path / "board.json"), Issues.open(tmp_path / "github.json")
    board.cards.append(replace(SEED, runs=[], log=[]))
    return board, board.cards[0], issues


def support():
    return Readers("acme", "U_SUPPORT")


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


async def approve(card):
    return "telegram:param", True


@pytest.mark.lab("build-an-ai-mission-control-app")
def test_a_customer_issue_is_done_only_after_a_human_approves_and_github_shows_the_reply(desk):
    board, card, issues = desk
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

    asyncio.run(ask_human(board, card, issues, approve))

    [posted] = Issues.open(issues.path).comments(412)
    assert posted["approved_by"] == "telegram:param" and "one quarter at a time" in posted["body"]
    assert card.column == "Done" and card.log[-1] == "Done: GitHub shows the reply on GH#412"
    # Five model calls at 2,000 in and 200 out, at $1 and $10 per million tokens.
    assert card.cost == pytest.approx(5 * (2000 * 1.0 + 200 * 10.0) / 1e6)
    assert board.cost_per_outcome == pytest.approx(card.cost)
    # The board on disk is the board on the page, and the Done card carries GitHub's copy of the reply.
    page = render(Board.open(board.path), {card.id: posted})
    assert "Globex: CSV export times out" in page and f"${card.cost:.4f}" in page
    assert "GitHub shows the reply" in page and "Approved by</dt><dd>telegram:param" in page


def test_no_ai_employee_can_approve_a_reply_an_ai_employee_drafted(desk):
    board, card, issues = desk
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
            decide(card, issues, by=agent, approve=True)
    assert issues.comments(412) == []


def test_an_agent_saying_it_replied_does_not_move_the_card_to_done(desk):
    board, card, issues = desk
    model = ScriptedModel(
        [
            [function_call("transfer_to_support_resolver", {}, call_id="h1")],
            [assistant_message("I replied to Globex on GitHub. All done.")],
        ],
        default_usage=TOKENS,
    )

    asyncio.run(work(board, card, issues, support(), model=model))

    assert card.column == "Backlog" and "GitHub shows no reply on GH#412" in card.log[-1]
    # It cost money and produced no outcome, so there is no cost per outcome yet.
    assert card.cost > 0 and board.cost_per_outcome is None


def test_a_draft_that_cites_nothing_it_read_is_sent_back(desk):
    board, card, issues = desk
    model = resolver_drafts(body="We will ship the fix next Monday [EXP-99].", then="Could not draft it.")

    asyncio.run(work(board, card, issues, support(), model=model))

    assert card.pending is None and "Refused: cite the Linear issue" in str(model.calls[4].input)
    assert card.column == "Backlog"


def test_a_declined_reply_posts_nothing_and_a_repeated_approval_posts_once(desk):
    board, card, issues = desk
    asyncio.run(work(board, card, issues, support(), model=resolver_drafts()))

    async def decline(card):
        return "telegram:param", False

    asyncio.run(ask_human(board, card, issues, decline))
    assert issues.comments(412) == [] and card.column == "Backlog"

    issues.reply(412, "Hello", card.id, approved_by="telegram:param")
    issues.reply(412, "Hello", card.id, approved_by="telegram:param")
    assert len(Issues.open(issues.path).comments(412)) == 1


def test_failed_runs_count_in_cost_per_outcome(desk):
    board, card, issues = desk
    failed = ScriptedModel(
        [[function_call("transfer_to_support_resolver", {}, call_id="h1")]] * 13, default_usage=TOKENS
    )
    asyncio.run(work(board, card, issues, support(), model=failed))
    assert card.runs[-1].ok is False and card.cost > 0

    card.column = "Backlog"
    asyncio.run(work(board, card, issues, support(), model=resolver_drafts()))
    asyncio.run(ask_human(board, card, issues, approve))

    assert card.column == "Done"
    assert board.cost_per_outcome == pytest.approx(sum(r.cost for r in card.runs))
    assert board.cost_per_outcome > card.runs[-1].cost


def test_the_card_page_shows_each_run_step_by_step(desk):
    board, card, issues = desk
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
    page = detail(card, None)
    assert "Post this reply on GH#412?" in page and "<li>Support resolver called read_issue</li>" in page


def test_the_board_buttons_approve_through_the_same_gate(desk, tmp_path):
    board, card, issues = desk
    asyncio.run(work(board, card, issues, support(), model=resolver_drafts()))
    card.pending["asked"] = "board"
    assert 'action="/cards/C-1/decide"' in render(board)

    (tmp_path / "decisions").mkdir()
    (tmp_path / "decisions" / "C-1.json").write_text(json.dumps({"by": "board:param", "yes": True}))
    asyncio.run(ask_human(board, card, issues, on_board(tmp_path)))

    assert card.column == "Done" and issues.replied(412, "C-1")["approved_by"] == "board:param"


def test_every_employee_on_the_team_page_has_an_outcome_a_metric_and_a_must_never(desk):
    board, _, _ = desk
    page = team(board, TEAM)
    for role in TEAM:
        assert role.outcome and role.metric and role.never
        assert role.name in page and role.never in page


def test_telegram_counts_only_a_tap_on_this_cards_message_in_the_chat():
    card = replace(SEED, runs=[], log=[])
    card.pending = {"issue": 412, "body": "Hi", "rests_on": ["EXP-41"], "maker": "Support resolver"}
    sent = []

    def tap(message_id, chat, data, user="param"):
        q = {"id": "q", "data": data, "from": {"id": 7, "username": user}}
        return {
            "update_id": len(sent),
            "callback_query": {**q, "message": {"message_id": message_id, "chat": {"id": chat}}},
        }

    def call(method, payload):
        sent.append((method, payload))
        if method == "sendMessage":
            return {"message_id": 50}
        if method == "getUpdates":
            return [tap(49, 1, "C-1:yes", "old"), tap(50, 2, "C-1:yes", "stranger"), tap(50, 1, "C-1:yes")]
        return True

    by, yes = asyncio.run(Telegram(1, call)(card))

    assert (by, yes) == ("telegram:7", True)
    assert "Rests on EXP-41" in sent[0][1]["text"]
    buttons = sent[0][1]["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == ["C-1:yes", "C-1:no"]


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
    board, _, issues = desk
    card = replace(QUESTION, runs=[], log=[], sources=[])
    board.cards.append(card)

    said = "**Known:** the export hits the 30 second limit; the Data squad owns it [EXP-41]. See [GH#999]."
    asyncio.run(work(board, card, issues, Readers("acme", "U_ENG"), model=analyst_answers(said)))

    # The manager handed the question to the analyst, which read Linear; the id it never read is cut.
    assert card.employee == "Engineering analyst" and card.column == "Done"
    assert card.sources == ["EXP-41"] and "GH#999" not in card.answer
    page = render(board)
    assert "Answered from sources it read" in page and "<p>Known: the export" in page
    assert issues.comments(412) == []

    card.column = "Backlog"
    asyncio.run(
        work(board, card, issues, Readers("acme", "U_ENG"), model=analyst_answers("It is fixed [GH#1]."))
    )
    assert card.column == "Backlog" and "cited no source" in card.log[-1]


def test_a_card_the_manager_keeps_goes_back_to_backlog(desk):
    board, _, issues = desk
    card = replace(QUESTION, runs=[], log=[], sources=[])
    board.cards.append(card)
    itself = ScriptedModel([[assistant_message("Yes, it is a known bug [GH#412].")]], default_usage=TOKENS)

    asyncio.run(work(board, card, issues, Readers("acme", "U_ENG"), model=itself))
    assert card.column == "Backlog" and card.log[-1] == "Backlog: no employee took the card"


def test_an_empty_reply_is_a_failed_run_and_not_an_answer(desk):
    board, _, issues = desk
    card = replace(QUESTION, runs=[], log=[], sources=[])
    board.cards.append(card)
    empty = ScriptedModel([[assistant_message("")]], default_usage=TOKENS)

    asyncio.run(work(board, card, issues, Readers("acme", "U_ENG"), model=empty))

    assert card.column == "Backlog" and card.runs[-1].ok is False
    assert card.log[-1] == "Backlog: run failed: the model returned nothing"


def test_a_host_that_fails_mid_run_fails_the_run_and_keeps_what_it_spent(desk):
    import httpx2
    from openai import APIConnectionError

    board, card, issues = desk
    down = APIConnectionError(request=httpx2.Request("POST", "https://openrouter.ai/api/v1/chat/completions"))
    model = ScriptedModel(
        [[function_call("transfer_to_support_resolver", {}, call_id="h1")], down], default_usage=TOKENS
    )

    asyncio.run(work(board, card, issues, support(), model=model))

    assert card.column == "Backlog" and card.runs[-1].ok is False
    assert card.log[-1] == "Backlog: run failed: APIConnectionError" and card.cost > 0


def test_a_comment_anyone_could_write_with_the_cards_marker_does_not_close_it(desk, tmp_path):
    board, card, issues = desk
    asyncio.run(work(board, card, issues, support(), model=resolver_drafts()))
    rows = json.loads((tmp_path / "github.json").read_text())
    forged = {"author": "globex-dev", "body": f"fixed!\n\n{marker('C-1')}", "url": "x"}
    next(r for r in rows if r["number"] == 412)["comments"].append(forged)
    (tmp_path / "github.json").write_text(json.dumps(rows))

    assert issues.replied(412, "C-1") is None


def test_a_run_may_read_only_the_issue_on_its_card(desk):
    board, card, issues = desk
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


def test_the_approve_button_counts_only_a_post_from_the_board_itself(desk, tmp_path):
    board, card, issues = desk
    asyncio.run(work(board, card, issues, support(), model=resolver_drafts()))

    async def post(origin, host=None):
        async with TestClient(TestServer(app(tmp_path, TEAM))) as client:
            headers = {"Origin": origin(client)} | ({"Host": host} if host else {})
            res = await client.post(
                "/cards/C-1/decide", data={"yes": "1"}, headers=headers, allow_redirects=False
            )
            return res.status

    assert asyncio.run(post(lambda c: "https://evil.example")) == 403
    # A page that rebinds its own name to 127.0.0.1 sends a matching Host and Origin.
    assert asyncio.run(post(lambda c: "http://evil.example:8080", host="evil.example:8080")) == 403
    assert not (tmp_path / "decisions" / "C-1.json").exists()
    assert asyncio.run(post(lambda c: str(c.make_url("")).rstrip("/"))) == 303
