"""The crew rebooks Priya's cancelled flight through real MCP gateways, one process per employee. A
scripted model stands in for the LLM, so the runs need no keys and spend nothing; the gateways and the
travel system are real."""

import asyncio
from dataclasses import replace

import pytest
from agents import Usage
from agents.testing import ScriptedModel, assistant_message, function_call

from crew.board import card_page
from crew.gateway import AGENTS, BOOKER, MESSENGER, PLANNER, SCOPES, SOLO, Gateway, calls
from crew.team import SEED, settle, work
from crew.travel import Refused, Travel
from workforce.board import Board

TOKENS = Usage(requests=1, input_tokens=2000, output_tokens=200, total_tokens=2200)


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKFORCE_PRICE_IN", "1.0")
    monkeypatch.setenv("WORKFORCE_PRICE_OUT", "10.0")
    b = Board(tmp_path / "board.json", [replace(SEED, runs=[], log=[])])
    b.save()
    return b


def told(to, flight="EW461", lands="11:55", call_id="m"):
    body = f"Your new flight is {flight}, landing at BER {lands}."
    return [function_call("send_message", {"to": to, "body": body}, call_id=call_id)]


def crew_model(*messages):
    return ScriptedModel(
        [
            [function_call("read_trip", {}, call_id="p1")],
            [function_call("search_flights", {}, call_id="p2")],
            [function_call("transfer_to_booker", {}, call_id="p3")],
            # The booker tries the cheapest flight first; it lands after the meeting.
            [function_call("book_flight", {"flight_id": "U28603"}, call_id="b1")],
            [function_call("book_flight", {"flight_id": "EW461"}, call_id="b2")],
            [function_call("transfer_to_messenger", {}, call_id="b3")],
            [function_call("read_ticket", {}, call_id="m1")],
            *messages,
            [assistant_message("Priya and Klaus know she lands at 11:55.")],
        ],
        default_usage=TOKENS,
    )


def tools(call):
    return {t.name for t in call.tools}


@pytest.mark.lab("build-a-team-of-ai-agents")
def test_the_card_is_done_only_when_a_ticket_lands_in_time_and_both_contacts_were_told(board):
    model = crew_model(told("Priya Shah", call_id="m2"), told("Klaus Bergmann", call_id="m3"))
    card = asyncio.run(work(board, board.cards[0], model=model))

    # Each employee saw only the tools its gateway lists for it.
    assert tools(model.calls[0]) == set(SCOPES[PLANNER])
    assert tools(model.calls[3]) == set(SCOPES[BOOKER])
    assert tools(model.calls[6]) == set(SCOPES[MESSENGER])

    travel = Travel(board.path.parent)
    ticket = travel.ticket(card.id)
    assert (ticket["flight"], ticket["status"], ticket["booked_by"]) == ("EW461", "ticketed", BOOKER)
    assert travel.told(card.id) == ["Klaus Bergmann", "Priya Shah"]
    assert card.column == "Done"
    assert card.log[-1] == "Done: EW461 ticketed, lands 11:55, Priya Shah and Klaus Bergmann told"

    # The gateway logged every call with who made it, and the travel system's refusal of the late flight.
    log = calls(board.path.parent, card.id)
    assert [c["agent"] for c in log] == [PLANNER] * 2 + [BOOKER] * 2 + [MESSENGER] * 3
    assert [(c["agent"], c["args"]) for c in log if not c["allowed"]] == [(BOOKER, {"flight_id": "U28603"})]
    assert (
        f"Gateway refused {BOOKER}: U28603 lands 19:30, too late for the 14:00 meeting" in card.runs[0].steps
    )
    assert "Trip planner handed the card to Booker" in card.runs[0].steps

    # The card page shows the room the ticket and the refused call.
    page = card_page(board.path.parent, card.id)
    assert "Ticketed" in page and "EW461, economy" in page and "Klaus Bergmann, Priya Shah" in page
    assert page.count('<tr class="no">') == 1
    # Every model call across the three employees counts toward the card's one cost.
    assert card.runs[0].cost == pytest.approx(len(model.calls) * (2000 + 200 * 10) / 1_000_000)


@pytest.mark.lab("build-a-team-of-ai-agents")
def test_a_ticket_nobody_was_told_about_never_closes_the_card(board):
    card = asyncio.run(work(board, board.cards[0], model=crew_model(told("Priya Shah", call_id="m2"))))

    assert Travel(board.path.parent).ticket(card.id)["status"] == "ticketed"
    assert card.column == "Needs you"
    assert card.log[-1] == "Needs you: EW461 is ticketed but Klaus Bergmann was not told"


@pytest.mark.lab("build-a-team-of-ai-agents")
def test_one_agent_with_every_tool_cannot_approve_its_own_fare_over_the_cap(board):
    model = ScriptedModel(
        [
            [function_call("read_trip", {}, call_id="s1")],
            [function_call("search_flights", {}, call_id="s2")],
            [function_call("book_flight", {"flight_id": "LH921"}, call_id="s3")],
            [function_call("approve_fare", {"ticket_id": "TK-001"}, call_id="s4")],
            told("Priya Shah", "LH921", "11:20", "s5"),
            told("Klaus Bergmann", "LH921", "11:20", "s6"),
            [assistant_message("Booked LH921 and approved it.")],
        ],
        default_usage=TOKENS,
    )
    card = asyncio.run(work(board, board.cards[0], model=model, alone=True))

    ticket = Travel(board.path.parent).ticket(card.id)
    assert (ticket["status"], ticket["approved_by"]) == ("waiting for Dana Ruiz", None)
    assert card.column == "Needs you" and card.log[-1] == "Needs you: £690 on LH921 waits for Dana Ruiz"
    refusal = f"{SOLO} may not approve a fare over the cap; only Dana Ruiz can"
    assert f"Gateway refused {SOLO}: {refusal}" in card.runs[0].steps

    # Priya's manager, a human, approves it on the board, and only then is the card Done.
    Travel(board.path.parent).approve("TK-001", "Dana Ruiz", True, AGENTS)
    settle(board, card, Travel(board.path.parent))
    assert card.log[-1] == "Done: LH921 ticketed, lands 11:20, Priya Shah and Klaus Bergmann told"


def test_a_tool_outside_an_employees_scope_is_refused_and_logged_even_when_called_directly(tmp_path):
    g = Gateway(MESSENGER, tmp_path, "C-3")
    assert {t.name for t in asyncio.run(g.list_tools())} == set(SCOPES[MESSENGER])
    said = asyncio.run(g.call_tool("book_flight", {"flight_id": "EW461"}))
    assert "Refused: Messenger may not call book_flight" in str(said)
    assert [c["allowed"] for c in calls(tmp_path)] == [False]
    assert Travel(tmp_path).ticket("C-3") is None


@pytest.mark.parametrize(
    ("flight", "why"),
    [
        ("BA986", "BA986 is full"),
        ("BA984", "BA984 lands 13:05, too late for the 14:00 meeting"),
        ("XX000", "XX000 is not a flight from LHR to BER on 2026-10-30"),
    ],
)
def test_the_travel_system_refuses_a_flight_that_breaks_its_rules(tmp_path, flight, why):
    with pytest.raises(Refused, match=why):
        Travel.open(tmp_path).book("C-3", flight, BOOKER)


def test_a_message_goes_only_to_a_contact_on_the_trip(tmp_path):
    with pytest.raises(Refused, match="not a contact"):
        Travel.open(tmp_path).send("C-3", "someone@example.com", "EW461", MESSENGER)
