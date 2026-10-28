"""The crew rebooks Priya's cancelled flight through real MCP gateways, one process per employee. A
scripted model stands in for the LLM, so the runs need no keys and spend nothing; the gateways and the
travel system are real."""

import asyncio
import os
import re
from dataclasses import replace

import pytest
from agents import Usage
from agents.testing import ScriptedModel, assistant_message, function_call
from aiohttp.test_utils import TestClient, TestServer

from crew.board import PEOPLE_FILE, TASKS, app, card_page
from crew.gateway import TOOLS_EACH_AGENT_MAY_CALL, AgentName, Gateway, read_call_log
from crew.team import (
    NARRATION_MAX_COLUMNS,
    SEED,
    TEAM,
    Column,
    resume_after_approval,
    settle,
    work,
)
from crew.travel import Refused, TicketStatus, Travel
from workforce.board import Board, team
from workforce.people import People

TOKENS = Usage(requests=1, input_tokens=2000, output_tokens=200, total_tokens=2200)


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKFORCE_PRICE_IN", "1.0")
    monkeypatch.setenv("WORKFORCE_PRICE_OUT", "10.0")
    crew_board = Board(tmp_path / "board.json", [replace(SEED, runs=[], log=[])])
    crew_board.save()
    return crew_board


def told(recipient, flight="EW461", lands="11:55", call_id="m"):
    body = f"Your new flight is {flight}, landing at BER {lands}."
    return [
        function_call(
            "send_message", {"recipient": recipient, "body": body}, call_id=call_id
        )
    ]


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
    return {tool.name for tool in call.tools}


@pytest.mark.lab("build-a-team-of-ai-agents")
def test_the_card_is_done_only_when_a_ticket_lands_in_time_and_both_contacts_were_told(
    board,
):
    model = crew_model(
        told("Priya Shah", call_id="m2"), told("Klaus Bergmann", call_id="m3")
    )
    card = asyncio.run(work(board, board.cards[0], model=model))

    # Each employee saw only the tools its gateway lists for it.
    assert tools(model.calls[0]) == set(
        TOOLS_EACH_AGENT_MAY_CALL[AgentName.TRIP_PLANNER]
    )
    assert tools(model.calls[3]) == set(TOOLS_EACH_AGENT_MAY_CALL[AgentName.BOOKER])
    assert tools(model.calls[6]) == set(TOOLS_EACH_AGENT_MAY_CALL[AgentName.MESSENGER])

    travel = Travel(board.path.parent)
    ticket = travel.ticket(card.id)
    assert (ticket["flight"], ticket["status"], ticket["booked_by"]) == (
        "EW461",
        TicketStatus.TICKETED,
        AgentName.BOOKER,
    )
    assert travel.told(card.id) == ["Klaus Bergmann", "Priya Shah"]
    assert card.column == "Done"
    assert (
        card.log[-1]
        == "Done: EW461 ticketed, lands 11:55, Priya Shah and Klaus Bergmann told"
    )

    # The gateway logged every call with who made it, and the travel system's refusal of the late flight.
    log = read_call_log(board.path.parent, card.id)
    assert [call["agent"] for call in log] == [AgentName.TRIP_PLANNER] * 2 + [
        AgentName.BOOKER
    ] * 2 + [AgentName.MESSENGER] * 3
    assert [(call["agent"], call["args"]) for call in log if not call["allowed"]] == [
        (AgentName.BOOKER, {"flight_id": "U28603"})
    ]
    assert (
        f"Gateway refused {AgentName.BOOKER}: U28603 lands 19:30, too late for the 14:00 meeting"
        in card.runs[0].steps
    )
    assert "Trip planner handed the card to Booker" in card.runs[0].steps

    # The card page shows the room the ticket and the refused call.
    page = card_page(board.path.parent, card.id)
    assert (
        "Ticketed" in page
        and "EW461, economy" in page
        and "Klaus Bergmann, Priya Shah" in page
    )
    assert page.count('<tr class="no">') == 1
    # The team page credits each of the three employees with the one Done card.
    assert team(board, TEAM).count("<b>1</b> done of 1") == 3
    # Every model call across the three employees counts toward the card's one cost.
    assert card.runs[0].cost == pytest.approx(
        len(model.calls) * (2000 + 200 * 10) / 1_000_000
    )


@pytest.mark.lab("build-a-team-of-ai-agents")
def test_a_ticket_nobody_was_told_about_never_closes_the_card(board):
    card = asyncio.run(
        work(board, board.cards[0], model=crew_model(told("Priya Shah", call_id="m2")))
    )

    assert Travel(board.path.parent).ticket(card.id)["status"] == "ticketed"
    assert card.column == "Needs you"
    assert (
        card.log[-1] == "Needs you: EW461 is ticketed but Klaus Bergmann was not told"
    )


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
    assert (ticket["status"], ticket["approved_by"]) == (
        TicketStatus.WAITING_FOR_MANAGER,
        None,
    )
    assert (
        card.column == "Needs you"
        and card.log[-1] == "Needs you: £690 on LH921 waits for Dana Ruiz"
    )
    refusal = (
        f"{AgentName.SOLO} may not approve a fare over the cap; only Dana Ruiz can"
    )
    assert f"Gateway refused {AgentName.SOLO}: {refusal}" in card.runs[0].steps

    # Priya's manager, a human, approves it on the board, and only then is the card Done.
    Travel(board.path.parent).approve("TK-001", approved_by="Dana Ruiz", approve=True)
    settle(board, card, Travel(board.path.parent))
    assert (
        card.log[-1]
        == "Done: LH921 ticketed, lands 11:20, Priya Shah and Klaus Bergmann told"
    )


def test_a_tool_outside_an_employees_scope_is_refused_and_logged_even_when_called_directly(
    tmp_path,
):
    gateway = Gateway(AgentName.MESSENGER, tmp_path, "C-3")
    assert {tool.name for tool in asyncio.run(gateway.list_tools())} == set(
        TOOLS_EACH_AGENT_MAY_CALL[AgentName.MESSENGER]
    )
    said = asyncio.run(gateway.call_tool("book_flight", {"flight_id": "EW461"}))
    assert "Refused: Messenger may not call book_flight" in str(said)
    assert [call["allowed"] for call in read_call_log(tmp_path)] == [False]
    assert Travel(tmp_path).ticket("C-3") is None


@pytest.mark.parametrize(
    ("flight", "why"),
    [
        ("BA986", "BA986 is full"),
        ("BA984", "BA984 lands 13:05, too late for the 14:00 meeting"),
        ("XX000", "XX000 is not a flight from LHR to BER on 2026-10-30"),
    ],
)
def test_the_travel_system_refuses_a_flight_that_breaks_its_rules(
    tmp_path, flight, why
):
    with pytest.raises(Refused, match=why):
        Travel.open(tmp_path).book("C-3", flight, AgentName.BOOKER)


def test_a_message_goes_only_to_a_contact_on_the_trip(tmp_path):
    with pytest.raises(Refused, match="not a contact"):
        Travel.open(tmp_path).send(
            "C-3", "someone@example.com", "EW461", AgentName.MESSENGER
        )


def held_fare_run(board):
    """The solo agent books LH921, £690, over the £400 cap: the travel system holds it for Dana."""
    model = ScriptedModel(
        [
            [function_call("book_flight", {"flight_id": "LH921"}, call_id="s1")],
            [assistant_message("Booked LH921; it waits for the manager.")],
        ],
        default_usage=TOKENS,
    )
    return asyncio.run(work(board, board.cards[0], model=model, alone=True))


def messenger_model():
    return ScriptedModel(
        [
            [function_call("read_ticket", {}, call_id="m1")],
            told("Priya Shah", "LH921", "11:20", "m2"),
            told("Klaus Bergmann", "LH921", "11:20", "m3"),
            [assistant_message("Priya and Klaus know she lands at 11:20.")],
        ],
        default_usage=TOKENS,
    )


@pytest.fixture
def people(monkeypatch):
    monkeypatch.setenv("BOARD_PASSPHRASE_PRIYA", "priya-passphrase")
    monkeypatch.setenv("BOARD_PASSPHRASE_DANA", "dana-passphrase")
    monkeypatch.setenv("BOARD_PASSPHRASE_SAM", "sam-passphrase")
    monkeypatch.setenv("BOARD_SECRET", "a-secret-for-this-test")
    return People(PEOPLE_FILE, os.environ["BOARD_SECRET"])


async def sign_in(client, name, passphrase):
    reply = await client.post(
        "/login", data={"name": name, "passphrase": passphrase}, allow_redirects=False
    )
    assert reply.status == 303
    return reply


@pytest.mark.lab("build-a-team-of-ai-agents")
def test_only_the_signed_in_manager_can_approve_a_fare_over_the_cap(board, people):
    card = held_fare_run(board)
    state = board.path.parent
    assert card.column == Column.NEEDS_YOU

    async def scenario():
        async def resume(held_board, held_card):
            return await resume_after_approval(
                held_board, held_card, model=messenger_model()
            )

        web_app = app(state, TEAM, people, resume)
        async with TestClient(TestServer(web_app)) as client:
            # Nobody is signed in: every page and the approval itself send them to the sign-in page.
            for path in ("/", "/team", f"/cards/{card.id}"):
                assert (await client.get(path, allow_redirects=False)).headers[
                    "Location"
                ] == "/login"
            denied = await client.post(
                f"/cards/{card.id}/approve", data={}, allow_redirects=False
            )
            assert denied.headers["Location"] == "/login"
            wrong = await client.post(
                "/login", data={"name": "Dana Ruiz", "passphrase": "guess"}
            )
            assert wrong.status == 401

            # Priya is signed in. She is Priya whatever the form claims, and the travel system refuses her.
            await sign_in(client, "Priya Shah", "priya-passphrase")
            form = {"decision": "approve", "approved_by": "Dana Ruiz"}
            await client.post(
                f"/cards/{card.id}/approve", data=form, allow_redirects=False
            )
            assert (
                Travel(state).ticket(card.id)["status"]
                == TicketStatus.WAITING_FOR_MANAGER
            )
            refused = [
                call for call in read_call_log(state, card.id) if not call["allowed"]
            ]
            assert [(call["agent"], call["tool"]) for call in refused] == [
                ("Priya Shah", "approve_fare")
            ]
            await client.post("/logout", allow_redirects=False)

            # Dana signs in and approves; only the messenger runs, and then the card is Done.
            await sign_in(client, "Dana Ruiz", "dana-passphrase")
            page = await (await client.get(f"/cards/{card.id}")).text()
            assert (
                "Signed in as <b>Dana Ruiz</b>, manager" in page and "Approve" in page
            )
            await client.post(
                f"/cards/{card.id}/approve",
                data={"decision": "approve"},
                allow_redirects=False,
            )
            await asyncio.gather(*web_app[TASKS])

    asyncio.run(scenario())
    travel = Travel(state)
    ticket = travel.ticket(card.id)
    assert (ticket["status"], ticket["approved_by"]) == (
        TicketStatus.TICKETED,
        "Dana Ruiz",
    )
    assert travel.told(card.id) == ["Klaus Bergmann", "Priya Shah"]
    assert Board.open(board.path).card(card.id).column == Column.DONE
    assert "Dana Ruiz, signed in at" in card_page(state, card.id)
    used_by_resume = {call["agent"] for call in read_call_log(state, card.id)[-4:]}
    assert (
        AgentName.MESSENGER in used_by_resume and AgentName.BOOKER not in used_by_resume
    )


@pytest.mark.lab("build-a-team-of-ai-agents")
def test_a_sold_out_flight_leaves_only_the_fare_that_waits_for_the_manager(tmp_path):
    travel = Travel.open(tmp_path)
    travel.mark_full("EW461")
    with pytest.raises(Refused, match="EW461 is full"):
        travel.book("C-3", "EW461", AgentName.BOOKER)
    assert (
        travel.book("C-3", "LH921", AgentName.BOOKER)["status"]
        == TicketStatus.WAITING_FOR_MANAGER
    )


@pytest.mark.lab("build-a-team-of-ai-agents")
def test_live_narration_prints_one_short_line_per_tool_call(board, capsys):
    asyncio.run(
        work(
            board,
            board.cards[0],
            model=crew_model(told("Priya Shah", call_id="m2")),
            live=True,
        )
    )
    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    plain = [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in lines]
    assert len(plain) == 8
    assert max(len(line) for line in plain) <= NARRATION_MAX_COLUMNS
    assert plain[0].startswith("Trip planner read_trip")
    assert any("Refused" in line for line in plain)
