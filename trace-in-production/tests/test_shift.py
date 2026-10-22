"""The shift's promises: the real crew tools, maintenance system, tracer and budget, with a scripted model,
a fake Langfuse and a fake Telegram.

The scripted crew does what the prompts ask: triage reads the sensor, then books, joins or dismisses as
its script names; the planner reads the manual and books. The fake Langfuse keeps the OTLP rows it is
posted the way Langfuse does, one observation per span id with the latest post winning, and answers
the v2 observations API from them. The kill stops it recording first, as a SIGKILL ends every post.
"""

import asyncio
import json
import re
import urllib.parse
from datetime import UTC, datetime

import pytest
from agents import Usage, set_trace_processors, set_tracing_disabled
from agents.testing import ModelStep, ScriptedModel, function_call
from agents.tracing import get_current_span

from upkeep import board, budget, run
from upkeep.crew import PLANNER, PLANNER_PROMPT, TRIAGE
from upkeep.plant import ALERTS, Cmms, Refused
from upkeep.stream import Streaming
from workforce.board import Board
from workforce.traces import Langfuse

PLAN = {
    "A-01": ("book", "MP-F2-07", "high"),
    "A-02": ("book", "MP-P1-02", "critical"),
    "A-03": ("book", "MP-C3-04", "medium"),
    "A-04": ("book", "MP-K1-11", "high"),
    "A-05": ("attach",),
    "A-06": ("dismiss",),
    "A-07": ("book", "MP-R1-03", "medium"),
}
USAGE = Usage(requests=1, input_tokens=2000, output_tokens=80, total_tokens=2080)


class Killed(BaseException):
    """What a SIGKILL is to the test: nothing after it runs, so nothing catches it."""


class FakeLangfuse:
    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.alive = True

    def post(self, body: dict) -> None:
        if not self.alive:
            return
        for rs in body["resourceSpans"]:
            for ss in rs["scopeSpans"]:
                for s in ss["spans"]:
                    self.rows[s["spanId"]] = self.observation(s)

    @staticmethod
    def observation(s: dict) -> dict:
        a = {x["key"]: next(iter(x["value"].values())) for x in s["attributes"]}
        at = datetime.fromtimestamp(int(s["startTimeUnixNano"]) / 1e9, UTC).isoformat()
        return {
            "id": s["spanId"],
            "traceId": s["traceId"],
            "parentObservationId": s.get("parentSpanId"),
            "name": s["name"],
            "type": a.get("langfuse.observation.type", "span").upper(),
            "startTime": at,
            "sessionId": a.get("langfuse.session.id"),
            "metadata": {"attempt": a["langfuse.trace.metadata.attempt"]}
            if "langfuse.trace.metadata.attempt" in a
            else {},
            "usageDetails": {
                "input": int(a.get("gen_ai.usage.input_tokens", 0)),
                "output": int(a.get("gen_ai.usage.output_tokens", 0)),
            },
        }

    def get(self, path: str) -> dict:
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(path).query))
        rows = list(self.rows.values())
        if "traceId" in q:
            rows = [r for r in rows if r["traceId"] == q["traceId"]]
        if q.get("isRootObservation") == "true":
            rows = [r for r in rows if not r["parentObservationId"]]
        for k in ("name", "sessionId"):
            if k in q:
                rows = [r for r in rows if r[k] == q[k]]
        return {"data": rows, "meta": {}}


@pytest.fixture
def traced():
    lf = FakeLangfuse()
    set_trace_processors([Streaming(lf.post)])
    set_tracing_disabled(False)
    yield lf
    set_tracing_disabled(True)


def items(call) -> list[dict]:
    return [i for i in call.input if isinstance(i, dict)] if isinstance(call.input, list) else []


def text(call) -> str:
    if isinstance(call.input, str):
        return call.input
    out = []
    for i in items(call):
        c = i.get("content") or i.get("output")
        out += [c] if isinstance(c, str) else [p.get("text", "") for p in c or [] if isinstance(p, dict)]
    return " ".join(out)


def called(call, tool: str) -> bool:
    return any(i.get("type") == "function_call" and i.get("name") == tool for i in items(call))


def crew_model(plan: dict = PLAN) -> ScriptedModel:
    def respond(call) -> ModelStep:
        said = text(call)
        alert = re.search(r"Alert (A-\d+)", said).group(1)
        a = next(x for x in ALERTS if x["id"] == alert)
        act = plan[alert]
        if call.system_instructions == PLANNER_PROMPT:
            step = (
                function_call("book_repair", {"procedure": act[1], "priority": act[2]}, call_id=f"b-{alert}")
                if called(call, "look_up_manual")
                else function_call("look_up_manual", {"machine": a["machine"]}, call_id=f"m-{alert}")
            )
        elif not called(call, "read_sensor"):
            step = function_call("read_sensor", {"machine": a["machine"]}, call_id=f"r-{alert}")
        elif act[0] == "book":
            step = function_call("transfer_to_repair_planner", {}, call_id=f"h-{alert}")
        elif act[0] == "attach":
            order = re.search(r"Open orders: (WO-\d+)", said).group(1)
            step = function_call("attach_to_order", {"order_id": order}, call_id=f"j-{alert}")
        else:
            step = function_call("dismiss_alert", {"reason": "Back to 0 s blocked"}, call_id=f"d-{alert}")
        # The chat-completions model writes its usage onto its generation span; the scripted one does not.
        if span := get_current_span():
            span.span_data.usage = {"input_tokens": USAGE.input_tokens, "output_tokens": USAGE.output_tokens}
        return ModelStep(output=[step], usage=USAGE)

    return ScriptedModel([ModelStep.respond(respond) for _ in range(200)], emit_traces=True)


def killed_shift(tmp_path, lf: FakeLangfuse | None, sent: list[str]) -> None:
    def kill() -> None:
        if lf:
            lf.alive = False
        raise Killed

    get = lf.get if lf else None
    with pytest.raises(Killed):
        asyncio.run(run.shift(tmp_path, crew_model(), "A-04", kill, get, sent.append, wait=0))
    if lf:
        lf.alive = True


def resumed(tmp_path, lf: FakeLangfuse | None, sent: list[str], plan: dict = PLAN) -> Board:
    get = lf.get if lf else None
    return asyncio.run(run.shift(tmp_path, crew_model(plan), get=get, send=sent.append, wait=0))


@pytest.mark.lab("build-an-ai-agent-dashboard-with-langfuse")
def test_a_killed_shift_resumes_and_every_repair_order_exists_once(tmp_path, traced):
    sent: list[str] = []
    killed_shift(tmp_path, traced, sent)
    left = Board.open(tmp_path / "board.json")
    assert left.card("A-04").column == "In flight"
    assert Cmms.open(tmp_path / "cmms.json").outcome("A-04") == "WO-2004"

    done = resumed(tmp_path, traced, sent)
    assert [c.column for c in done.cards] == ["Done"] * 7
    cmms = Cmms.open(tmp_path / "cmms.json")
    assert sorted(cmms.orders) == ["WO-2001", "WO-2002", "WO-2003", "WO-2004", "WO-2005"]
    assert cmms.orders["WO-2001"].alerts == ["A-01", "A-05"]
    assert cmms.orders["WO-2004"].alerts == ["A-04"]
    held = [a for o in cmms.orders.values() for a in o.alerts] + list(cmms.dismissed)
    assert sorted(held) == sorted(a["id"] for a in ALERTS)

    a04 = done.card("A-04")
    killed = [r for r in a04.runs if r.note.startswith(run.KILLED)]
    assert {r.employee for r in killed} == {TRIAGE, PLANNER}
    assert all(r.input_tokens > 0 and r.cost > 0 for r in killed), "spend from its trace"
    assert a04.runs[-1].note == "already booked WO-2004"


@pytest.mark.lab("build-an-ai-agent-dashboard-with-langfuse")
def test_a_resumed_run_that_joins_its_own_order_changes_nothing(tmp_path, traced):
    """What the live model did: on resume, triage saw WO-2004 open on the compressor and joined it."""
    sent: list[str] = []
    killed_shift(tmp_path, traced, sent)
    done = resumed(tmp_path, traced, sent, PLAN | {"A-04": ("attach",)})
    assert done.card("A-04").runs[-1].note == "already booked WO-2004"
    assert Cmms.open(tmp_path / "cmms.json").orders["WO-2004"].alerts == ["A-04"]


@pytest.mark.lab("build-an-ai-agent-dashboard-with-langfuse")
def test_every_cards_runs_match_its_traces_per_employee(tmp_path, traced):
    sent: list[str] = []
    killed_shift(tmp_path, traced, sent)
    resumed(tmp_path, traced, sent)
    rows = run.reconcile(tmp_path, traced.get)
    assert len(rows) == 7 and all(r.endswith("match") for r in rows), rows
    assert "A-04  2 runs on the board, 2 in Langfuse" in next(r for r in rows if "A-04" in r)


@pytest.mark.lab("build-an-ai-agent-dashboard-with-langfuse")
def test_yesterdays_shift_in_langfuse_does_not_count_against_todays(tmp_path, traced):
    """Every shift uses the card ids as sessions; the reconcile reads only this shift's traces."""
    sent: list[str] = []
    killed_shift(tmp_path / "yesterday", traced, sent)
    resumed(tmp_path / "yesterday", traced, sent)
    killed_shift(tmp_path / "today", traced, sent)
    resumed(tmp_path / "today", traced, sent)
    rows = run.reconcile(tmp_path / "today", traced.get)
    assert all(r.endswith("match") for r in rows), rows


@pytest.mark.lab("build-an-ai-agent-dashboard-with-langfuse")
def test_a_trace_posted_only_at_its_end_loses_the_killed_runs_spend(tmp_path):
    """Mission Control's processor posts a trace when it finishes: a killed run leaves nothing behind."""
    lf = FakeLangfuse()
    set_trace_processors([Langfuse(lf.post)])
    set_tracing_disabled(False)
    try:
        sent: list[str] = []
        killed_shift(tmp_path, lf, sent)
        done = resumed(tmp_path, lf, sent)
    finally:
        set_tracing_disabled(True)
    killed = [r for r in done.card("A-04").runs if r.note.startswith(run.KILLED)]
    assert [r.cost for r in killed] == [0.0]
    assert "no trace" in killed[0].note


@pytest.mark.lab("build-an-ai-agent-dashboard-with-langfuse")
def test_the_budget_alert_goes_once_across_the_kill(tmp_path, traced):
    sent: list[str] = []
    killed_shift(tmp_path, traced, sent)
    done = resumed(tmp_path, traced, sent)
    spent = sum(c.cost for c in done.cards)
    assert spent > budget.BUDGET
    assert len(sent) == 1, sent
    assert sent[0].startswith("Brekke Dairy Line 2: the crew has spent")
    assert f"{PLANNER}: " in sent[0] and f"{TRIAGE}: " in sent[0]
    again = asyncio.run(run.shift(tmp_path, crew_model(), get=traced.get, send=sent.append, wait=0))
    assert len(sent) == 1 and all(c.column == "Done" for c in again.cards)


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
    assert (order.id, how, len(cmms.orders)) == ("WO-2001", "joined", 1)
    with pytest.raises(Refused):
        cmms.attach("A-02", "WO-2001")
    assert Cmms.open(tmp_path / "cmms.json").orders["WO-2001"].alerts == ["A-01", "A-05"]


@pytest.mark.lab("build-an-ai-agent-dashboard-with-langfuse")
def test_the_board_shows_spend_by_employee_the_alert_and_each_order(tmp_path, traced):
    sent: list[str] = []
    killed_shift(tmp_path, traced, sent)
    resumed(tmp_path, traced, sent)
    page = board.board_page(tmp_path)
    assert "Spend by employee" in page and "Budget alert sent to Ingrid Halvorsen on Telegram" in page
    assert "Workspace <b>Brekke Dairy</b>" in page
    detail = board.card_page(tmp_path, "A-05")
    assert "WO-2001 in the maintenance system" in detail and "Alerts on this order: A-01, A-05" in detail
    assert "/project/mission-control/sessions/A-05" in detail
    assert "Dismissed" in board.card_page(tmp_path, "A-06")
    assert json.loads((tmp_path / "alerts.json").read_text())["budget"]["text"] == sent[0]


@pytest.mark.lab("build-an-ai-agent-dashboard-with-langfuse")
def test_every_line_the_shift_prints_fits_the_recorded_terminal(tmp_path, traced, capsys):
    sent: list[str] = []
    killed_shift(tmp_path, traced, sent)
    resumed(tmp_path, traced, sent)
    for row in run.reconcile(tmp_path, traced.get):
        run.say(row)
    shown = [re.sub(r"\x1b\[[0-9;]*m", "", s) for s in capsys.readouterr().out.splitlines()]
    assert len(shown) > 10
    assert [s for s in shown if len(s) > run.WIDTH] == []
