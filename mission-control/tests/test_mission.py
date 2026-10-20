"""Mission Control: the manager hands a double charge card to the support resolver, the refund waits for
a human, and the card is Done only when the ledger agrees. Real SDK runs with a scripted model and a
fake approver, so no keys and no spend."""

import asyncio
from dataclasses import replace

import pytest
from agents import Usage
from agents.testing import ScriptedModel, assistant_message, function_call

from workforce.board import Board, render
from workforce.ledger import Ledger
from workforce.mission import QUESTION, SEED, NotAllowed, ask_human, decide, work
from workforce.sources import Readers
from workforce.telegram import Telegram

TOKENS = Usage(requests=1, input_tokens=2000, output_tokens=200, total_tokens=2200)


@pytest.fixture
def desk(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKFORCE_PRICE_IN", "1.0")
    monkeypatch.setenv("WORKFORCE_PRICE_OUT", "10.0")
    board, ledger = Board.open(tmp_path / "board.json"), Ledger.open(tmp_path / "ledger.json")
    board.cards.append(replace(SEED, runs=[], log=[]))
    return board, board.cards[0], ledger


def resolver_requests(charge="ch_3002", then=None):
    return ScriptedModel(
        [
            [function_call("transfer_to_support_resolver", {}, call_id="h1")],
            [function_call("find_charges", {"customer": "cus_812"}, call_id="c1")],
            [function_call("request_refund", {"charge_id": charge, "reason": "second charge"}, call_id="c2")],
            [assistant_message(then or "The duplicate ch_3002 is waiting for your approval.")],
        ],
        default_usage=TOKENS,
    )


async def approve(card):
    return "telegram:param", True


@pytest.mark.lab("build-an-ai-mission-control-app")
def test_a_double_charge_is_done_only_after_a_human_approves_and_the_ledger_agrees(desk):
    board, card, ledger = desk
    model = resolver_requests()

    asyncio.run(work(board, card, ledger, Readers("acme", "U_ENG"), model=model))

    # The manager handed it to the employee hired for money, which found the duplicate on ORD-5531.
    assert card.employee == "Support resolver"
    assert "ch_3001 order ORD-5531" in str(model.calls[2].input)
    # A refund cannot be undone: nothing is written until a human decides.
    assert card.column == "Needs you" and ledger.refunds == []
    assert card.pending["charge"] == "ch_3002"

    asyncio.run(ask_human(board, card, ledger, approve))

    assert ledger.refunds == [{"charge": "ch_3002", "amount": 4900, "approved_by": "telegram:param"}]
    assert card.column == "Done" and ledger.duplicates("cus_812") == []
    # Four model calls at 2,000 in and 200 out, at $1 and $10 per million tokens.
    assert card.cost == pytest.approx(4 * (2000 * 1.0 + 200 * 10.0) / 1e6)
    assert board.cost_per_outcome == pytest.approx(card.cost)
    # The board on disk is the board on the page.
    page = render(Board.open(board.path), Ledger.open(ledger.path))
    assert "Customer charged twice at checkout" in page and f"${card.cost:.4f}" in page
    # The Done card carries the ledger rows that closed it, not the agent's word.
    assert "Refunded</dt><dd>ch_3002 49.00" in page and "Approved by</dt><dd>telegram:param" in page


def test_no_ai_employee_can_approve_a_refund_an_ai_employee_requested(desk):
    board, card, ledger = desk
    asyncio.run(work(board, card, ledger, Readers("acme", "U_ENG"), model=resolver_requests()))
    board.move(card, "In flight", "a user asked the manager to hurry")
    model = ScriptedModel(
        [
            [function_call("approve_pending", {}, call_id="a1")],
            [assistant_message("I could not approve it; it is waiting for you.")],
        ],
        default_usage=TOKENS,
    )
    card.column = "Backlog"

    asyncio.run(work(board, card, ledger, Readers("acme", "U_ENG"), model=model))

    assert ledger.refunds == [] and card.column == "Needs you"
    assert any("Manager may not approve" in line for line in card.log)
    for agent in ("Manager", "Support resolver", "Engineering analyst"):
        with pytest.raises(NotAllowed):
            decide(card, ledger, by=agent, approve=True)
    assert ledger.refunds == []


def test_an_agent_saying_done_does_not_move_the_card_to_done(desk):
    board, card, ledger = desk
    model = ScriptedModel(
        [
            [function_call("transfer_to_support_resolver", {}, call_id="h1")],
            [assistant_message("Refunded the customer. All done.")],
        ],
        default_usage=TOKENS,
    )

    asyncio.run(work(board, card, ledger, Readers("acme", "U_ENG"), model=model))

    assert card.column == "Backlog" and "ch_3002" in card.log[-1]
    # It cost money and produced no outcome, so there is no cost per outcome yet.
    assert card.cost > 0 and board.cost_per_outcome is None


def test_a_declined_refund_writes_nothing_and_a_repeated_approval_writes_once(desk):
    board, card, ledger = desk
    asyncio.run(work(board, card, ledger, Readers("acme", "U_ENG"), model=resolver_requests()))

    async def decline(card):
        return "telegram:param", False

    asyncio.run(ask_human(board, card, ledger, decline))
    assert ledger.refunds == [] and card.column == "Backlog"

    ledger.refund("ch_3002", approved_by="telegram:param")
    ledger.refund("ch_3002", approved_by="telegram:param")
    assert len(Ledger.open(ledger.path).refunds) == 1


def test_the_resolver_cannot_queue_a_refund_for_another_customer(desk):
    board, card, ledger = desk
    model = resolver_requests(charge="ch_3010", then="Nothing to refund.")

    asyncio.run(work(board, card, ledger, Readers("acme", "U_ENG"), model=model))

    assert card.pending is None and "belongs to another customer" in str(model.calls[3].input)


def test_failed_runs_count_in_cost_per_outcome(desk):
    board, card, ledger = desk
    failed = ScriptedModel(
        [[function_call("transfer_to_support_resolver", {}, call_id="h1")]] * 13, default_usage=TOKENS
    )
    asyncio.run(work(board, card, ledger, Readers("acme", "U_ENG"), model=failed))
    assert card.runs[-1].ok is False and card.cost > 0

    card.column = "Backlog"
    asyncio.run(work(board, card, ledger, Readers("acme", "U_ENG"), model=resolver_requests()))
    asyncio.run(ask_human(board, card, ledger, approve))

    assert card.column == "Done"
    assert board.cost_per_outcome == pytest.approx(sum(r.cost for r in card.runs))
    assert board.cost_per_outcome > card.runs[-1].cost


def test_telegram_counts_only_a_tap_on_this_cards_message_in_the_chat():
    card = replace(SEED, runs=[], log=[])
    card.pending = {"charge": "ch_3002", "reason": "second charge", "maker": "Support resolver"}
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

    assert (by, yes) == ("telegram:param", True)
    buttons = sent[0][1]["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == ["C-1:yes", "C-1:no"]


def analyst_answers(text):
    return ScriptedModel(
        [
            [function_call("transfer_to_engineering_analyst", {}, call_id="h1")],
            [function_call("search_linear", {"query": "double charge"}, call_id="s1")],
            [assistant_message(text)],
        ],
        default_usage=TOKENS,
    )


@pytest.mark.lab("build-an-ai-mission-control-app")
def test_a_question_card_is_done_only_when_its_answer_cites_a_source_the_analyst_read(desk):
    board, _, ledger = desk
    card = replace(QUESTION, runs=[], log=[], sources=[])
    board.cards.append(card)

    said = "**Known:** the retry path makes a new idempotency key [PAY-88]. Fixed in [GH#999]."
    asyncio.run(work(board, card, ledger, Readers("acme", "U_ENG"), model=analyst_answers(said)))

    # The manager handed the question to the analyst, which read Linear; the id it never read is cut.
    assert card.employee == "Engineering analyst" and card.column == "Done"
    assert card.sources == ["PAY-88"] and "GH#999" not in card.answer
    page = render(board, ledger)
    assert "Answered from sources it read" in page and "<p>Known: the retry" in page and ledger.refunds == []

    card.column = "Backlog"
    asyncio.run(
        work(board, card, ledger, Readers("acme", "U_ENG"), model=analyst_answers("It is fixed [GH#1]."))
    )
    assert card.column == "Backlog" and "cited no source" in card.log[-1]


def test_a_card_the_manager_keeps_goes_back_to_backlog(desk):
    board, _, ledger = desk
    card = replace(QUESTION, runs=[], log=[], sources=[])
    board.cards.append(card)
    itself = ScriptedModel([[assistant_message("Yes, it is a known bug [GH#412].")]], default_usage=TOKENS)

    # A question has no customer, so without this the empty ledger check would close it.
    asyncio.run(work(board, card, ledger, Readers("acme", "U_ENG"), model=itself))
    assert card.column == "Backlog" and card.log[-1] == "Backlog: no employee took the card"
