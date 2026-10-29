"""The week's promises: the real tools, gate, systems of record and decision record, with a scripted model.

The scripted employees do what the prompts ask, and what the live model did on its first week: the
supplies buyer tries the cheapest supplier first, the rota planner tries whoever has the fewest hours,
and each moves on only when the gate refuses with its rule.
"""

import asyncio
import json
import os
import re

import pytest
from agents import Usage
from agents.testing import ModelStep, ScriptedModel, function_call
from aiohttp.test_utils import TestClient, TestServer

from operating import board, crew, policy, run, scripted
from operating.hotels import Systems
from operating.record import Record
from workforce.board import Board
from workforce.people import People, Person

MARTA = Person("Marta Berg", "operations director")

USAGE = Usage(requests=1, input_tokens=1500, output_tokens=60, total_tokens=1560)
# Each job's tries in order; the employee moves to the next only after the gate refuses one.
TRIES = {
    "J-01": [("set_price", {"price": 139})],
    "J-02": [("set_price", {"price": 119})],
    "J-03": [("place_order", {"supplier": "Nordlin", "quantity": 120})],
    "J-04": [("place_order", {"supplier": "Kaffebrenneriet", "quantity": 60})],
    "J-05": [
        ("place_order", {"supplier": "QuickStock", "quantity": 400}),
        ("place_order", {"supplier": "Fjordvann", "quantity": 400}),
    ],
    "J-06": [
        ("assign_shift", {"staff": "Jonas Lie"}),
        ("assign_shift", {"staff": "Aisha Noor"}),
    ],
}
READ = {"rates": "read_demand", "supplies": "read_stock", "rota": "read_rota"}


def items(call) -> list[dict]:
    return (
        [item for item in call.input if isinstance(item, dict)]
        if isinstance(call.input, list)
        else []
    )


def asked(call) -> str:
    return (
        call.input
        if isinstance(call.input, str)
        else str(items(call)[0].get("content", ""))
    )


def employees(tries: dict = TRIES) -> ScriptedModel:
    def respond(call) -> ModelStep:
        job = re.search(r"Job (J-\d+)", asked(call)).group(1)
        key = next(
            name
            for name, prompt in crew.PROMPTS.items()
            if prompt == call.system_instructions
        )
        calls = [item for item in items(call) if item.get("type") == "function_call"]
        done = [item for item in calls if item.get("name") != READ[key]]
        if not any(item.get("name") == READ[key] for item in calls):
            step = function_call(READ[key], {}, call_id=f"r-{job}")
        else:
            tool, arguments = tries[job][len(done)]
            step = function_call(tool, arguments, call_id=f"w-{job}-{len(done)}")
        return ModelStep(output=[step], usage=USAGE)

    return ScriptedModel([ModelStep.respond(respond) for _ in range(100)])


def week(state, tries: dict = TRIES) -> Board:
    return asyncio.run(run.week(state, employees(tries)))


def verdicts(state, card: str) -> list[str]:
    return [row["verdict"] for row in Record(state / "record.jsonl").for_card(card)]


@pytest.mark.lab("build-an-ai-agent-governance-framework")
def test_each_job_lands_the_verdict_the_policy_names(tmp_path):
    done = week(tmp_path)
    columns = {card.id: card.column for card in done.cards}
    assert columns == {
        "J-01": "Done",
        "J-02": "Needs you",
        "J-03": "Done",
        "J-04": "Needs you",
        "J-05": "Done",
        "J-06": "Needs you",
        "J-07": "Needs you",
    }
    assert sorted(run.waiting(tmp_path)) == ["J-02", "J-04", "J-06"]
    systems = Systems.open(tmp_path / "systems.json")
    assert [write["card"] for write in systems.writes] == ["J-01", "J-03", "J-05"]
    assert systems.price("OSL", "2026-12-18") == 139
    assert systems.price("BGO", "2026-12-19") == 165, "the drop waits for Ingrid Dahl"
    assert done.card("J-01").issue == "Oslo, Rate Fri 18 Dec"


@pytest.mark.lab("build-an-ai-agent-governance-framework")
def test_a_refusal_cites_its_rule_and_the_employee_retries_inside_it(tmp_path):
    week(tmp_path)
    j05 = Record(tmp_path / "record.jsonl").for_card("J-05")
    assert [row["verdict"] for row in j05] == ["refused", "allowed"]
    assert j05[0]["rule"] == "BUY-1: QuickStock is not an approved supplier"
    assert j05[1]["action"] == "400 Minibar water, Fjordvann, EUR 240"
    j06 = Record(tmp_path / "record.jsonl").for_card("J-06")
    assert [row["verdict"] for row in j06] == ["refused", "asked"]
    assert j06[0]["rule"] == "ROTA-1: Jonas Lie would rest 8 h; the rule is 11 h"
    assert j06[1]["signer"] == "Lars Eide, hotel manager"
    assert verdicts(tmp_path, "J-02") == ["asked"] and verdicts(tmp_path, "J-04") == [
        "asked"
    ]


@pytest.mark.lab("build-an-ai-agent-governance-framework")
def test_a_paused_employee_or_one_over_budget_never_starts(tmp_path, monkeypatch):
    done = week(tmp_path)
    assert done.card("J-07").runs == [], "no model call for a paused employee"
    assert verdicts(tmp_path, "J-07") == ["not started"]
    assert (
        Record(tmp_path / "record.jsonl")
        .for_card("J-07")[0]["rule"]
        .startswith("RUN-1: paused by Marta Berg")
    )

    monkeypatch.setitem(policy.AGENTS["supplies"], "cap", 0.0)
    again = week(tmp_path / "capped")
    assert all(again.card(job).runs == [] for job in ("J-03", "J-04", "J-05"))
    assert verdicts(tmp_path / "capped", "J-03") == ["not started"]
    assert (
        "RUN-2: the Supplies buyer has spent its $0.00 AI budget"
        in again.card("J-03").log[-1]
    )


@pytest.mark.lab("build-an-ai-agent-governance-framework")
def test_nothing_waiting_is_written_until_the_named_signer_answers(tmp_path):
    week(tmp_path)
    with pytest.raises(
        run.NotAllowed, match="only Ingrid Dahl signs a rate change over 15%"
    ):
        run.sign(tmp_path, "J-02", "Marta Berg", True)
    systems = Systems.open(tmp_path / "systems.json")
    assert systems.price("BGO", "2026-12-19") == 165, (
        "the refused approval wrote nothing"
    )
    assert "J-02" in run.waiting(tmp_path)
    signed = run.sign(tmp_path, "J-02", "Ingrid Dahl", True)
    declined = run.sign(tmp_path, "J-04", "Marta Berg", False)
    run.sign(tmp_path, "J-06", "Lars Eide", True)
    systems = Systems.open(tmp_path / "systems.json")
    assert systems.price("BGO", "2026-12-19") == 119
    assert not systems.orders.get("PO-3003") and systems.written("J-04") is None
    assert systems.shifts["SVG Aisha Noor"][-1] == [
        "2026-12-22T15:00",
        "2026-12-22T23:00",
    ]
    assert (signed.column, declined.column) == ("Done", "Done")
    assert "declined by Marta Berg; nothing was written" in declined.log[-1]
    rows = Record(tmp_path / "record.jsonl").for_card("J-02")
    assert [row["verdict"] for row in rows] == ["asked", "refused", "signed"]
    assert rows[-1] | {"at": ""} == {
        "at": "",
        "card": "J-02",
        "agent": "Rate manager",
        "action": "Bergen Sat 19 Dec: EUR 165 to 119",
        "verdict": "signed",
        "rule": "RATE-2: a move over 15% is signed by Ingrid Dahl; this is 28%",
        "by": "Ingrid Dahl",
        "signer": "Ingrid Dahl, revenue manager",
    }
    assert verdicts(tmp_path, "J-04") == ["asked", "declined"]
    assert run.waiting(tmp_path) == {}


@pytest.mark.lab("build-an-ai-agent-governance-framework")
def test_a_declined_action_is_never_written(tmp_path):
    week(tmp_path)
    run.sign(tmp_path, "J-04", "Marta Berg", False)
    assert Systems.open(tmp_path / "systems.json").written("J-04") is None
    assert all(good for _, good in run.check(tmp_path))
    assert Board.open(tmp_path / "board.json").card("J-04").column == "Done"
    # The same action written around the gate is caught, whatever the record says about the decline.
    systems = Systems.open(tmp_path / "systems.json")
    systems.apply(
        "J-04",
        run.waiting(tmp_path).get("J-04", {}).get("action") or decline_action(tmp_path),
    )
    bad = [row for row, good in run.check(tmp_path) if not good]
    assert any("DECLINED BUT WRITTEN" in row for row in bad)


def decline_action(state) -> dict:
    asked = [
        row
        for row in Record(state / "record.jsonl").for_card("J-04")
        if row["verdict"] == "asked"
    ]
    assert asked
    return {
        "kind": "order",
        "hotel": "BGO",
        "item": "coffee",
        "supplier": "Kaffebrenneriet",
        "quantity": 60,
        "total": 1260.0,
    }


@pytest.mark.lab("build-an-ai-agent-governance-framework")
def test_no_ai_employee_can_sign_and_none_has_a_tool_to(tmp_path):
    week(tmp_path)
    for name in policy.NAMES:
        with pytest.raises(run.NotAllowed):
            run.sign(tmp_path, "J-02", name, True)
    with pytest.raises(run.NotAllowed):
        run.sign(tmp_path, "J-01", "Marta Berg", True)
    tools = {tool.name for tools in crew.TOOLS.values() for tool in tools}
    assert not {
        tool for tool in tools if re.search(r"(^|_)(approve|sign|policy)", tool)
    }, tools
    assert Systems.open(tmp_path / "systems.json").written("J-02") is None


@pytest.mark.lab("build-an-ai-agent-governance-framework")
def test_every_write_has_exactly_one_decision_behind_it(tmp_path):
    week(tmp_path)
    run.sign(tmp_path, "J-02", "Ingrid Dahl", True)
    run.sign(tmp_path, "J-06", "Lars Eide", True)
    rows = run.check(tmp_path)
    assert len(rows) == 5 and all(good for _, good in rows), rows
    # A write that went around the gate has no decision behind it, and the check says so.
    systems = Systems.open(tmp_path / "systems.json")
    systems.apply(
        "J-04",
        {
            "kind": "order",
            "hotel": "BGO",
            "item": "coffee",
            "supplier": "Kaffebrenneriet",
        }
        | {
            "quantity": 60,
            "total": 1260.0,
        },
    )
    bad = [row for row, good in run.check(tmp_path) if not good]
    assert len(bad) == 1 and "J-04" in bad[0] and bad[0].endswith("NO RECORD")


@pytest.mark.lab("build-an-ai-agent-governance-framework")
def test_the_record_is_only_ever_appended(tmp_path):
    week(tmp_path)
    before = (tmp_path / "record.jsonl").read_text()
    run.sign(tmp_path, "J-02", "Ingrid Dahl", True)
    run.sign(tmp_path, "J-04", "Marta Berg", False)
    after = (tmp_path / "record.jsonl").read_text()
    assert (
        after.startswith(before)
        and len(after.splitlines()) == len(before.splitlines()) + 2
    )


@pytest.mark.lab("build-an-ai-agent-governance-framework")
def test_the_policy_file_changes_what_the_employees_may_do(tmp_path, monkeypatch):
    monkeypatch.setitem(policy.AGENTS["rates"], "change_alone", 30)
    monkeypatch.setitem(
        policy.AGENTS["supplies"],
        "suppliers",
        ["Nordlin", "Kaffebrenneriet", "QuickStock"],
    )
    done = week(tmp_path)
    assert done.card("J-02").column == "Done"
    assert verdicts(tmp_path, "J-05") == ["allowed"], (
        "QuickStock approved, so the first try lands"
    )
    floor = policy.check(
        "rates",
        {
            "kind": "price",
            "hotel": "BGO",
            "night": "2026-12-19",
            "was": 165,
            "price": 90,
        },
    )
    assert floor.kind == "refuse" and floor.rule.startswith("RATE-1")


@pytest.mark.lab("build-an-ai-agent-governance-framework")
def test_the_board_team_and_month_pages_read_the_record(tmp_path):
    week(tmp_path)
    page = board.board_page(tmp_path, MARTA)
    assert page.count('class="decide"') == 3 and "Workspace <b>KaaS Hotels</b>" in page
    assert "Rate manager asks: Bergen Sat 19 Dec: EUR 165 to 119" in page
    assert "Signed in as <b>Marta Berg</b>, operations director" in page
    assert '<a href="/month">Month</a>' in page
    team = board.team_page(tmp_path, MARTA)
    assert "Operating model" in team and team.count('class="status paused"') == 1
    assert (
        "Move a night&#x27;s price up to 15%, never under the hotel&#x27;s floor"
        in team
    )
    run.sign(tmp_path, "J-02", "Ingrid Dahl", True)
    run.sign(tmp_path, "J-04", "Marta Berg", False)
    rows = {row["key"]: row for row in run.month(tmp_path)}
    assert (
        rows["rates"]["done"],
        rows["rates"]["allowed"],
        rows["rates"]["signed"],
    ) == (2, 1, 1)
    assert (
        rows["supplies"]["done"],
        rows["supplies"]["declined"],
        rows["supplies"]["refused"],
    ) == (2, 1, 1)
    assert rows["rates"]["saved"] == 2 * policy.AGENTS["rates"]["minutes"]
    month = board.month_page(tmp_path, MARTA)
    assert (
        '<a href="/month" aria-current=page>Month</a>' in month
        and "Signed of asked" in month
    )
    assert "<b>50%</b>" in month
    detail = board.card_page(tmp_path, "J-02", MARTA)
    assert "Decision record" in detail and "Ingrid Dahl, for Ingrid Dahl" in detail


@pytest.fixture
def people(monkeypatch):
    monkeypatch.setenv("BOARD_PASSPHRASE_MARTA_BERG", "marta-passphrase")
    monkeypatch.setenv("BOARD_PASSPHRASE_INGRID_DAHL", "ingrid-passphrase")
    monkeypatch.setenv("BOARD_PASSPHRASE_LARS_EIDE", "lars-passphrase")
    monkeypatch.setenv("BOARD_SECRET", "a-secret-for-this-test")
    return People(board.PEOPLE_FILE, os.environ["BOARD_SECRET"])


async def sign_in(client, name: str, passphrase: str) -> None:
    reply = await client.post(
        "/login", data={"name": name, "passphrase": passphrase}, allow_redirects=False
    )
    assert reply.status == 303


@pytest.mark.lab("build-an-ai-agent-governance-framework")
def test_only_the_named_signer_signed_in_can_sign(tmp_path, people):
    week(tmp_path)

    async def scenario() -> None:
        async with TestClient(
            TestServer(board.app(tmp_path, people), host="127.0.0.1")
        ) as client:
            for path in ("/", "/team", "/month", "/cards/J-02"):
                reply = await client.get(path, allow_redirects=False)
                assert reply.headers["Location"] == "/login"
            denied = await client.post(
                "/cards/J-02/decide", data={"yes": "1"}, allow_redirects=False
            )
            assert denied.headers["Location"] == "/login"
            wrong = await client.post(
                "/login", data={"name": "Ingrid Dahl", "passphrase": "guess"}
            )
            assert wrong.status == 401
            # Marta is signed in and presses Approve on a rate change that is Ingrid's to sign.
            await sign_in(client, "Marta Berg", "marta-passphrase")
            forged = {"yes": "1", "by": "Ingrid Dahl"}
            sent = await client.post(
                "/cards/J-02/decide", data=forged, allow_redirects=False
            )
            assert sent.status == 303
            page = await (await client.get("/cards/J-02")).text()
            assert "only Ingrid Dahl signs a rate change over 15%" in page
            await client.post("/logout", allow_redirects=False)
            await sign_in(client, "Ingrid Dahl", "ingrid-passphrase")
            await client.post(
                "/cards/J-02/decide", data={"yes": "1"}, allow_redirects=False
            )

    asyncio.run(scenario())
    systems = Systems.open(tmp_path / "systems.json")
    assert systems.price("BGO", "2026-12-19") == 119
    refused = [
        row
        for row in Record(tmp_path / "record.jsonl").for_card("J-02")
        if row["verdict"] == "refused"
    ]
    assert [row["by"] for row in refused] == ["Marta Berg"]


@pytest.mark.lab("build-an-ai-agent-governance-framework")
def test_without_the_gate_a_price_under_the_floor_is_written(tmp_path):
    spec = run.job("J-02")
    desk = run.opened(tmp_path)
    asyncio.run(
        crew.handle(
            spec,
            desk.systems,
            desk.record,
            scripted.price_under_the_floor(89),
            gated=False,
        )
    )
    assert desk.systems.price("BGO", "2026-12-19") == 89
    assert desk.record.for_card("J-02") == [], "no decision sits behind the write"
    assert any("NO RECORD" in row for row, good in run.check(tmp_path) if not good)


@pytest.mark.lab("build-an-ai-agent-governance-framework")
def test_a_blank_waiting_file_is_no_run(tmp_path):
    (tmp_path / "waiting.json").write_text("")
    assert run.waiting(tmp_path) == {}


@pytest.mark.lab("build-an-ai-agent-governance-framework")
def test_every_line_the_week_prints_fits_the_terminal(tmp_path, capsys):
    week(tmp_path)
    run.sign(tmp_path, "J-02", "Ingrid Dahl", True)
    for row, _ in run.check(tmp_path):
        run.say(row)
    shown = [
        re.sub(r"\x1b\[[0-9;]*m", "", line)
        for line in capsys.readouterr().out.splitlines()
    ]
    assert len(shown) > 15
    assert [line for line in shown if len(line) > run.WIDTH or line.endswith("…")] == []
    assert json.loads((tmp_path / "waiting.json").read_text()).keys() == {
        "J-04",
        "J-06",
    }
