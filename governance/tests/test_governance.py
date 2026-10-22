"""The week's promises: the real tools, gate, systems of record and decision record, with a scripted model.

The scripted employees do what the prompts ask, and what the live model did on its first week: the
supplies buyer tries the cheapest supplier first, the rota planner tries whoever has the fewest hours,
and each moves on only when the gate refuses with its rule.
"""

import asyncio
import json
import re

import pytest
from agents import Usage
from agents.testing import ModelStep, ScriptedModel, function_call
from aiohttp.test_utils import TestClient, TestServer

from operating import board, crew, policy, run
from operating.hotels import Systems
from operating.record import Record
from workforce.board import Board

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
    "J-06": [("assign_shift", {"staff": "Jonas Lie"}), ("assign_shift", {"staff": "Aisha Noor"})],
}
READ = {"rates": "read_demand", "supplies": "read_stock", "rota": "read_rota"}


def items(call) -> list[dict]:
    return [i for i in call.input if isinstance(i, dict)] if isinstance(call.input, list) else []


def asked(call) -> str:
    return call.input if isinstance(call.input, str) else str(items(call)[0].get("content", ""))


def employees(tries: dict = TRIES) -> ScriptedModel:
    def respond(call) -> ModelStep:
        job = re.search(r"Job (J-\d+)", asked(call)).group(1)
        key = next(k for k, p in crew.PROMPTS.items() if p == call.system_instructions)
        done = [i for i in items(call) if i.get("type") == "function_call" and i.get("name") != READ[key]]
        if not any(i.get("name") == READ[key] for i in items(call) if i.get("type") == "function_call"):
            step = function_call(READ[key], {}, call_id=f"r-{job}")
        else:
            tool, args = tries[job][len(done)]
            step = function_call(tool, args, call_id=f"w-{job}-{len(done)}")
        return ModelStep(output=[step], usage=USAGE)

    return ScriptedModel([ModelStep.respond(respond) for _ in range(100)])


def week(state, tries: dict = TRIES) -> Board:
    return asyncio.run(run.week(state, employees(tries)))


def verdicts(state, card: str) -> list[str]:
    return [r["verdict"] for r in Record(state / "record.jsonl").of(card)]


@pytest.mark.lab("build-an-ai-agent-governance-framework")
def test_each_job_lands_the_verdict_the_policy_names(tmp_path):
    done = week(tmp_path)
    columns = {c.id: c.column for c in done.cards}
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
    assert [w["card"] for w in systems.writes] == ["J-01", "J-03", "J-05"]
    assert systems.price("OSL", "2026-12-18") == 139
    assert systems.price("BGO", "2026-12-19") == 165, "the drop waits for Ingrid Dahl"
    assert done.card("J-01").issue == "Oslo, Rate Fri 18 Dec"


@pytest.mark.lab("build-an-ai-agent-governance-framework")
def test_a_refusal_cites_its_rule_and_the_employee_retries_inside_it(tmp_path):
    week(tmp_path)
    j05 = Record(tmp_path / "record.jsonl").of("J-05")
    assert [r["verdict"] for r in j05] == ["refused", "allowed"]
    assert j05[0]["rule"] == "BUY-1: QuickStock is not an approved supplier"
    assert j05[1]["action"] == "400 Minibar water, Fjordvann, EUR 240"
    j06 = Record(tmp_path / "record.jsonl").of("J-06")
    assert [r["verdict"] for r in j06] == ["refused", "asked"]
    assert j06[0]["rule"] == "ROTA-1: Jonas Lie would rest 8 h; the rule is 11 h"
    assert j06[1]["signer"] == "Lars Eide, hotel manager"
    assert verdicts(tmp_path, "J-02") == ["asked"] and verdicts(tmp_path, "J-04") == ["asked"]


@pytest.mark.lab("build-an-ai-agent-governance-framework")
def test_a_paused_employee_or_one_over_budget_never_starts(tmp_path, monkeypatch):
    done = week(tmp_path)
    assert done.card("J-07").runs == [], "no model call for a paused employee"
    assert verdicts(tmp_path, "J-07") == ["not started"]
    assert Record(tmp_path / "record.jsonl").of("J-07")[0]["rule"].startswith("RUN-1: paused by Marta Berg")

    monkeypatch.setitem(policy.AGENTS["supplies"], "cap", 0.0)
    again = week(tmp_path / "capped")
    assert all(again.card(c).runs == [] for c in ("J-03", "J-04", "J-05"))
    assert verdicts(tmp_path / "capped", "J-03") == ["not started"]
    assert "RUN-2: the Supplies buyer has spent its $0.00 AI budget" in again.card("J-03").log[-1]


@pytest.mark.lab("build-an-ai-agent-governance-framework")
def test_nothing_waiting_is_written_until_the_named_signer_answers(tmp_path):
    week(tmp_path)
    signed = run.sign(tmp_path, "J-02", "board:marta", True)
    declined = run.sign(tmp_path, "J-04", "board:marta", False)
    run.sign(tmp_path, "J-06", "board:lars", True)
    systems = Systems.open(tmp_path / "systems.json")
    assert systems.price("BGO", "2026-12-19") == 119
    assert not systems.orders.get("PO-3003") and systems.written("J-04") is None
    assert systems.shifts["SVG Aisha Noor"][-1] == ["2026-12-22T15:00", "2026-12-22T23:00"]
    assert (signed.column, declined.column) == ("Done", "Backlog")
    rows = Record(tmp_path / "record.jsonl").of("J-02")
    assert rows[-1] | {"at": ""} == {
        "at": "",
        "card": "J-02",
        "agent": "Rate manager",
        "action": "Bergen Sat 19 Dec: EUR 165 to 119",
        "verdict": "signed",
        "rule": "RATE-2: a move over 15% is signed by Ingrid Dahl; this is 28%",
        "by": "board:marta",
        "signer": "Ingrid Dahl, revenue manager",
    }
    assert verdicts(tmp_path, "J-04") == ["asked", "declined"]
    assert run.waiting(tmp_path) == {}


@pytest.mark.lab("build-an-ai-agent-governance-framework")
def test_no_ai_employee_can_sign_and_none_has_a_tool_to(tmp_path):
    week(tmp_path)
    for name in policy.NAMES:
        with pytest.raises(run.NotAllowed):
            run.sign(tmp_path, "J-02", name, True)
    with pytest.raises(run.NotAllowed):
        run.sign(tmp_path, "J-01", "board:marta", True)
    tools = {t.name for ts in crew.TOOLS.values() for t in ts}
    assert not {t for t in tools if re.search(r"(^|_)(approve|sign|policy)", t)}, tools
    assert Systems.open(tmp_path / "systems.json").written("J-02") is None


@pytest.mark.lab("build-an-ai-agent-governance-framework")
def test_every_write_has_exactly_one_decision_behind_it(tmp_path):
    week(tmp_path)
    run.sign(tmp_path, "J-02", "board:marta", True)
    run.sign(tmp_path, "J-06", "board:lars", True)
    rows = run.check(tmp_path)
    assert len(rows) == 5 and all(ok for _, ok in rows), rows
    # A write that went around the gate has no decision behind it, and the check says so.
    systems = Systems.open(tmp_path / "systems.json")
    systems.apply(
        "J-04",
        {"kind": "order", "hotel": "BGO", "item": "coffee", "supplier": "Kaffebrenneriet"}
        | {
            "quantity": 60,
            "total": 1260.0,
        },
    )
    bad = [row for row, ok in run.check(tmp_path) if not ok]
    assert len(bad) == 1 and "J-04" in bad[0] and bad[0].endswith("NO RECORD")


@pytest.mark.lab("build-an-ai-agent-governance-framework")
def test_the_record_is_only_ever_appended(tmp_path):
    week(tmp_path)
    before = (tmp_path / "record.jsonl").read_text()
    run.sign(tmp_path, "J-02", "board:marta", True)
    run.sign(tmp_path, "J-04", "board:marta", False)
    after = (tmp_path / "record.jsonl").read_text()
    assert after.startswith(before) and len(after.splitlines()) == len(before.splitlines()) + 2


@pytest.mark.lab("build-an-ai-agent-governance-framework")
def test_the_policy_file_changes_what_the_employees_may_do(tmp_path, monkeypatch):
    monkeypatch.setitem(policy.AGENTS["rates"], "change_alone", 30)
    monkeypatch.setitem(policy.AGENTS["supplies"], "suppliers", ["Nordlin", "Kaffebrenneriet", "QuickStock"])
    done = week(tmp_path)
    assert done.card("J-02").column == "Done"
    assert verdicts(tmp_path, "J-05") == ["allowed"], "QuickStock approved, so the first try lands"
    floor = policy.check(
        "rates", {"kind": "price", "hotel": "BGO", "night": "2026-12-19", "was": 165, "price": 90}
    )
    assert floor.kind == "refuse" and floor.rule.startswith("RATE-1")


@pytest.mark.lab("build-an-ai-agent-governance-framework")
def test_the_board_team_and_month_pages_read_the_record(tmp_path):
    week(tmp_path)
    page = board.board_page(tmp_path)
    assert page.count('class="decide"') == 3 and "Workspace <b>Halden Hotels</b>" in page
    assert "Rate manager asks: Bergen Sat 19 Dec: EUR 165 to 119" in page
    assert '<a href="/month">Month</a>' in page
    team = board.team_page(tmp_path)
    assert "Operating model" in team and team.count('class="status paused"') == 1
    assert "Move a night&#x27;s price up to 15%, never under the hotel&#x27;s floor" in team
    run.sign(tmp_path, "J-02", "board:marta", True)
    run.sign(tmp_path, "J-04", "board:marta", False)
    rows = {r["key"]: r for r in run.month(tmp_path)}
    assert (rows["rates"]["done"], rows["rates"]["allowed"], rows["rates"]["signed"]) == (2, 1, 1)
    assert (rows["supplies"]["done"], rows["supplies"]["declined"], rows["supplies"]["refused"]) == (2, 1, 1)
    assert rows["rates"]["saved"] == 2 * policy.AGENTS["rates"]["minutes"]
    month = board.month_page(tmp_path)
    assert '<a href="/month" aria-current=page>Month</a>' in month and "Signed of asked" in month
    assert "<b>50%</b>" in month
    detail = board.card_page(tmp_path, "J-02")
    assert "Decision record" in detail and "board:marta, for Ingrid Dahl" in detail


@pytest.mark.lab("build-an-ai-agent-governance-framework")
def test_only_the_boards_own_form_can_sign(tmp_path):
    week(tmp_path)

    async def post(origin: str | None) -> int:
        async with TestClient(TestServer(board.app(tmp_path), host="127.0.0.1")) as client:
            headers = {"Origin": origin} if origin else {}
            r = await client.post(
                "/cards/J-02/decide", data={"yes": "1"}, headers=headers, allow_redirects=False
            )
            return r.status

    assert asyncio.run(post(None)) == 403
    assert asyncio.run(post("http://evil.example")) == 403
    assert Systems.open(tmp_path / "systems.json").written("J-02") is None
    server = TestServer(board.app(tmp_path), host="127.0.0.1")

    async def own() -> int:
        async with TestClient(server) as client:
            origin = f"http://127.0.0.1:{server.port}"
            r = await client.post(
                "/cards/J-02/decide", data={"yes": "1"}, headers={"Origin": origin}, allow_redirects=False
            )
            return r.status

    assert asyncio.run(own()) == 303
    assert Systems.open(tmp_path / "systems.json").price("BGO", "2026-12-19") == 119


@pytest.mark.lab("build-an-ai-agent-governance-framework")
def test_every_line_the_week_prints_fits_the_recorded_terminal(tmp_path, capsys):
    week(tmp_path)
    run.sign(tmp_path, "J-02", "board:marta", True)
    for row, _ in run.check(tmp_path):
        run.say(row)
    shown = [re.sub(r"\x1b\[[0-9;]*m", "", s) for s in capsys.readouterr().out.splitlines()]
    assert len(shown) > 15
    assert [s for s in shown if len(s) > run.WIDTH or s.endswith("…")] == []
    assert json.loads((tmp_path / "waiting.json").read_text()).keys() == {"J-04", "J-06"}
