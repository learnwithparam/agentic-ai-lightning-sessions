"""The shift's promises: the real crew tools, maintenance system, tracer, budget and board, with a scripted
model (upkeep/scripted.py) and a fake Langfuse (tests/fake_langfuse.py).

The kill stops the fake recording first, as a SIGKILL ends every post.
"""

import asyncio
import os
import re

import pytest
from agents import set_trace_processors, set_tracing_disabled
from aiohttp.test_utils import TestClient, TestServer
from fake_langfuse import FakeLangfuse

from upkeep import board, budget, naive, run
from upkeep.crew import PLANNER, TRIAGE
from upkeep.plant import ALERTS, Cmms, Refused
from upkeep.scripted import PLAN, crew_model
from upkeep.stream import Streaming
from workforce.board import Board
from workforce.people import People, Person
from workforce.traces import Langfuse

INGRID = Person("Ingrid Halvorsen", "maintenance lead")


class Killed(BaseException):
    """What a SIGKILL is to the test: nothing after it runs, so nothing catches it."""


@pytest.fixture
def traced():
    langfuse = FakeLangfuse()
    set_trace_processors([Streaming(langfuse.post)])
    set_tracing_disabled(False)
    yield langfuse
    set_tracing_disabled(True)


@pytest.fixture
def people(monkeypatch):
    monkeypatch.setenv("BOARD_PASSPHRASE_INGRID_HALVORSEN", "ingrid-passphrase")
    monkeypatch.setenv("BOARD_PASSPHRASE_ERIK_LUND", "erik-passphrase")
    monkeypatch.setenv("BOARD_SECRET", "a-secret-for-this-test")
    return People(board.PEOPLE_FILE, os.environ["BOARD_SECRET"])


def killed_shift(state, langfuse: FakeLangfuse | None) -> None:
    def kill() -> None:
        if langfuse:
            langfuse.alive = False
        raise Killed

    get = langfuse.get if langfuse else None
    with pytest.raises(Killed):
        asyncio.run(run.shift(state, crew_model(), "A-04", kill, get, wait=0))
    if langfuse:
        langfuse.alive = True


def resumed(state, langfuse: FakeLangfuse | None, plan: dict = PLAN) -> Board:
    get = langfuse.get if langfuse else None
    return asyncio.run(run.shift(state, crew_model(plan), get=get, wait=0))


def paused_shift(state, monkeypatch) -> Board:
    monkeypatch.setenv("BUDGET_USD", "0.0006")
    return asyncio.run(run.shift(state, crew_model(), None, wait=0))


@pytest.mark.lab("build-an-ai-agent-dashboard-with-langfuse")
def test_a_killed_shift_resumes_and_every_repair_order_exists_once(tmp_path, traced):
    killed_shift(tmp_path, traced)
    left = Board.open(tmp_path / "board.json")
    assert left.card("A-04").column == "In flight"
    assert Cmms.open(tmp_path / "cmms.json").outcome("A-04").order_id == "WO-2004"

    done = resumed(tmp_path, traced)
    assert [card.column for card in done.cards] == ["Done"] * 7
    cmms = Cmms.open(tmp_path / "cmms.json")
    assert sorted(cmms.orders) == ["WO-2001", "WO-2002", "WO-2003", "WO-2004", "WO-2005"]
    assert cmms.orders["WO-2001"].alerts == ["A-01", "A-05"]
    assert cmms.orders["WO-2004"].alerts == ["A-04"]
    held = [alert for order in cmms.orders.values() for alert in order.alerts] + list(cmms.dismissed)
    assert sorted(held) == sorted(alert["id"] for alert in ALERTS)

    compressor = done.card("A-04")
    killed = [item for item in compressor.runs if item.note.startswith(run.KILLED)]
    assert {item.employee for item in killed} == {TRIAGE, PLANNER}
    assert all(item.input_tokens > 0 and item.cost > 0 for item in killed), "spend from its trace"
    assert compressor.runs[-1].note == "already booked WO-2004"


@pytest.mark.lab("build-an-ai-agent-dashboard-with-langfuse")
def test_a_resumed_run_that_joins_its_own_order_changes_nothing(tmp_path, traced):
    """What the live model did: on resume, triage saw WO-2004 open on the compressor and joined it."""
    killed_shift(tmp_path, traced)
    done = resumed(tmp_path, traced, PLAN | {"A-04": ("attach",)})
    assert done.card("A-04").runs[-1].note == "already booked WO-2004"
    assert Cmms.open(tmp_path / "cmms.json").orders["WO-2004"].alerts == ["A-04"]


@pytest.mark.lab("build-an-ai-agent-dashboard-with-langfuse")
def test_every_cards_runs_match_its_traces_per_employee(tmp_path, traced):
    killed_shift(tmp_path, traced)
    resumed(tmp_path, traced)
    rows = run.reconcile(tmp_path, traced.get)
    assert len(rows) == 7 and all(row.matches for row in rows), rows
    compressor = next(row for row in rows if row.card == "A-04")
    assert (compressor.runs, compressor.found) == (2, 2)


@pytest.mark.lab("build-an-ai-agent-dashboard-with-langfuse")
def test_yesterdays_shift_in_langfuse_does_not_count_against_todays(tmp_path, traced):
    """Every shift uses the card ids as sessions; the reconcile reads only this shift's traces."""
    for day in ("yesterday", "today"):
        killed_shift(tmp_path / day, traced)
        resumed(tmp_path / day, traced)
    rows = run.reconcile(tmp_path / "today", traced.get)
    assert all(row.matches for row in rows), rows


@pytest.mark.lab("build-an-ai-agent-dashboard-with-langfuse")
def test_a_trace_posted_only_at_its_end_loses_the_killed_runs_spend(tmp_path):
    """Mission Control's processor posts a trace when it finishes: a killed run leaves nothing behind."""
    langfuse = FakeLangfuse()
    set_trace_processors([Langfuse(langfuse.post)])
    set_tracing_disabled(False)
    try:
        killed_shift(tmp_path, langfuse)
        done = resumed(tmp_path, langfuse)
    finally:
        set_tracing_disabled(True)
    killed = [item for item in done.card("A-04").runs if item.note.startswith(run.KILLED)]
    assert [item.cost for item in killed] == [0.0]
    assert "no trace" in killed[0].note


@pytest.mark.lab("build-an-ai-agent-dashboard-with-langfuse")
def test_a_shift_over_budget_pauses_for_ingrid(tmp_path, monkeypatch):
    paused = paused_shift(tmp_path, monkeypatch)
    pause = paused.card(run.PAUSE_CARD)
    assert pause.column == "Needs you" and "Ingrid Halvorsen decides" in pause.log[-1]
    assert (tmp_path / run.PAUSED).exists() and budget.over(paused)
    waiting = [
        card.card_id for card in paused.cards if card.card_id in run.ALERT_IDS and card.column != "Done"
    ]
    assert waiting and "A-07" in waiting, "the alerts after the pause have not run"
    assert not any(card.column == "In flight" for card in paused.cards)
    page = board.board_page(tmp_path, INGRID)
    assert "Over budget: paused for Ingrid Halvorsen" in page
    assert "Workspace <b>KaaS Dairy</b>" in page


@pytest.mark.lab("build-an-ai-agent-dashboard-with-langfuse")
def test_only_ingrid_can_continue_the_shift(tmp_path, monkeypatch, people):
    paused_shift(tmp_path, monkeypatch)
    before = Cmms.open(tmp_path / "cmms.json").orders.copy()
    monkeypatch.setattr(
        board,
        "continue_shift",
        lambda state, name, get=None: run.continue_shift(state, name, crew_model(), None, wait=0),
    )
    form = f"/cards/{run.PAUSE_CARD}/continue"

    async def press(client) -> None:
        await client.post(form, data={"approved_by": "Ingrid Halvorsen"}, allow_redirects=False)

    async def sign_in(client, name: str, passphrase: str) -> None:
        reply = await client.post(
            "/login", data={"name": name, "passphrase": passphrase}, allow_redirects=False
        )
        assert reply.status == 303

    async def scenario() -> None:
        async with TestClient(TestServer(board.app(tmp_path, people))) as client:
            denied = await client.post(form, data={}, allow_redirects=False)
            assert denied.headers["Location"] == "/login"
            wrong = await client.post("/login", data={"name": "Ingrid Halvorsen", "passphrase": "guess"})
            assert wrong.status == 401

            # Erik is Erik whatever the form claims; the budget is Ingrid's.
            await sign_in(client, "Erik Lund", "erik-passphrase")
            await press(client)
            assert Board.open(tmp_path / "board.json").card(run.PAUSE_CARD).column == "Needs you"
            assert Cmms.open(tmp_path / "cmms.json").orders == before
            await client.post("/logout", allow_redirects=False)

            await sign_in(client, "Ingrid Halvorsen", "ingrid-passphrase")
            await press(client)
            for _ in range(200):
                if Board.open(tmp_path / "board.json").card(run.PAUSE_CARD).column == "Done":
                    break
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.3)
            await press(client)

    asyncio.run(scenario())
    decisions = [(row["by"], row["decision"], row["said"]) for row in budget.decisions(tmp_path)]
    assert decisions[0] == (
        "Erik Lund",
        "refused",
        "Erik Lund may not continue the shift; only Ingrid Halvorsen can raise the shift budget",
    )
    assert decisions[1][:2] == ("Ingrid Halvorsen", "approved")
    assert decisions[2] == ("Ingrid Halvorsen", "refused", "No shift is paused")
    done = Board.open(tmp_path / "board.json")
    assert done.card(run.PAUSE_CARD).column == "Done"
    assert all(done.card(alert).column == "Done" for alert in run.ALERT_IDS)
    assert len(Cmms.open(tmp_path / "cmms.json").orders) == 5
    assert not (tmp_path / run.PAUSED).exists()


@pytest.mark.lab("build-an-ai-agent-dashboard-with-langfuse")
def test_naive_books_the_compressor_twice(tmp_path):
    cmms = asyncio.run(naive.shift(tmp_path))
    assert len(cmms.orders) == 6
    assert [order.order_id for order in cmms.orders.values() if order.alerts == ["A-04"]] == [
        "WO-2004",
        "WO-2005",
    ]


@pytest.mark.lab("build-an-ai-agent-dashboard-with-langfuse")
def test_an_empty_state_file_is_no_run(tmp_path):
    for name in ("board.json", "cmms.json", "shift.json"):
        (tmp_path / name).write_text("")
    assert Board.open(tmp_path / "board.json").cards == []
    assert Cmms.open(tmp_path / "cmms.json").orders == {}
    assert run.began(tmp_path) is None


@pytest.mark.lab("build-an-ai-agent-dashboard-with-langfuse")
def test_the_maintenance_system_holds_the_line_whatever_the_model_books(tmp_path):
    cmms = Cmms(tmp_path / "cmms.json")
    with pytest.raises(Refused):
        cmms.book("A-01", "MP-F2-99", "high")
    with pytest.raises(Refused):
        cmms.book("A-01", "MP-F2-07", "urgent")
    assert cmms.book("A-01", "MP-F2-07", "high")[1] == "booked"
    assert cmms.book("A-01", "MP-F2-07", "high")[1] == "already booked"
    # A-05 is the same fault: a planner that books it instead of joining still opens no second order.
    order, how = cmms.book("A-05", "MP-F2-07", "high")
    assert (order.order_id, how, len(cmms.orders)) == ("WO-2001", "joined", 1)
    with pytest.raises(Refused):
        cmms.attach("A-02", "WO-2001")
    assert Cmms.open(tmp_path / "cmms.json").orders["WO-2001"].alerts == ["A-01", "A-05"]


@pytest.mark.lab("build-an-ai-agent-dashboard-with-langfuse")
def test_the_board_shows_spend_by_employee_and_each_order(tmp_path, traced):
    killed_shift(tmp_path, traced)
    resumed(tmp_path, traced)
    page = board.board_page(tmp_path, INGRID)
    assert "Spend by employee" in page and "Within budget" in page
    assert "Workspace <b>KaaS Dairy</b>" in page
    detail = board.card_page(tmp_path, "A-05", INGRID)
    assert "WO-2001 in the maintenance system" in detail and "Alerts on this order: A-01, A-05" in detail
    assert "/project/mission-control/sessions/A-05" in detail
    assert "Dismissed" in board.card_page(tmp_path, "A-06", INGRID)


@pytest.mark.lab("build-an-ai-agent-dashboard-with-langfuse")
def test_every_line_the_shift_prints_fits_the_terminal(tmp_path, traced, capsys):
    killed_shift(tmp_path, traced)
    resumed(tmp_path, traced)
    for row in run.reconcile(tmp_path, traced.get):
        run.say(row.line())
    shown = [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in capsys.readouterr().out.splitlines()]
    assert len(shown) > 10
    assert [line for line in shown if len(line) > run.WIDTH] == []


@pytest.mark.lab("build-an-ai-agent-dashboard-with-langfuse")
def test_a_machine_named_by_its_name_finds_the_same_manual_as_its_id():
    from upkeep.crew import machine_id

    assert machine_id("Ammonia compressor K1") == machine_id(" k1 ") == "K1"
    assert machine_id("Nowhere") == "NOWHERE"
