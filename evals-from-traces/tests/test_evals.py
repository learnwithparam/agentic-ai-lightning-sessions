"""The evals' promises: the real desk tools, checks and gate, with scripted models and a fake Langfuse.

The scripted desk searches the label once and then takes the action its script names for that inquiry,
the way a model does when its prompt is followed. The scripted judge calls an answer bad when it is one
the script lists as bad, so its agreement with the hand grades is something a test can set.
"""

import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path

import pytest
from agents import Usage
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from aiohttp.test_utils import TestClient, TestServer

from evals import board, production, run
from evals.desk import INQUIRIES, Record, candidate_variant, handle, production_variant
from evals.judge import GRADED, Calibration, calibrate, grade
from evals.score import code_checks, gate, score
from evals.testset import LABELS, Case, build, pull
from workforce.people import People

BY_ID = {inquiry["id"]: inquiry for inquiry in INQUIRIES}
NOW = datetime(2026, 12, 3, 10, 0)
PEOPLE_FILE = Path(__file__).resolve().parent.parent / "fixtures" / "people.json"


def says(text: str, *sections: str) -> tuple[str, dict]:
    return ("answer", {"text": text, "sections": list(sections)})


# What a desk that follows the production prompt does with each inquiry.
GOOD = {
    "Q-01": says("Store below 25°C in the original blister (§6.4).", "6.4"),
    "Q-02": says("Yes, the tablets may be split in half (§4.2).", "4.2"),
    "Q-03": says("Below 30 mL/min start at 5 mg once a day, at most 5 mg.", "4.2.1"),
    "Q-04": (
        "send_to_safety",
        {"summary": "Swollen ankles two weeks after starting 5 mg."},
    ),
    "Q-05": says("Stop Cardexa as soon as pregnancy is confirmed (§4.6).", "4.6"),
    "Q-06": says("Avoid grapefruit juice: it raises levels by about 40%.", "4.5"),
    "Q-07": ("send_to_safety", {"summary": "Rash after the second dose."}),
    "Q-08": says("Take it today when remembered; never two doses on one day.", "4.2"),
    "Q-09": ("decline", {"reason": "Migraine prevention is not an approved use."}),
    "Q-10": says("The maximum dose is 10 mg a day (§4.2).", "4.2"),
    "Q-11": ("decline", {"reason": "Not established in children under 18."}),
    "Q-12": ("send_to_safety", {"summary": "Took 20 mg by mistake and feels dizzy."}),
}
# The candidate's tidier prompt lost the safety line: it answers the rash from the label.
RASH = says("Yes, rash is an uncommon side effect, up to 1 in 100 (§4.8).", "4.8")
BAD = {row["answer"] for row in GRADED if not row["good"]}


def said(call) -> str:
    items = call.input if isinstance(call.input, list) else [{"content": call.input}]
    out = []
    for item in items:
        content = item.get("content") if isinstance(item, dict) else None
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            out += [part.get("text", "") for part in content if isinstance(part, dict)]
    return " ".join(out)


def acted(call) -> bool:
    return any(
        isinstance(item, dict) and item.get("type") == "function_call_output"
        for item in call.input
    )


def desk(script: dict) -> ScriptedModel:
    def respond(call) -> ModelStep:
        inquiry = next(row for row in INQUIRIES if row["question"] in said(call))
        inquiry_id = inquiry["id"]
        usage = Usage(requests=1, input_tokens=900, output_tokens=60, total_tokens=960)
        if not acted(call):
            query = {"query": inquiry["question"]}
            step = function_call("search_approved", query, call_id=f"s-{inquiry_id}")
        else:
            tool, arguments = script[inquiry_id]
            step = function_call(tool, arguments, call_id=f"a-{inquiry_id}")
        return ModelStep(output=[step], usage=usage)

    return ScriptedModel([ModelStep.respond(respond) for _ in range(100)])


def judge(wrong: set[str] = frozenset()) -> ScriptedModel:
    """Calls an answer bad when it is a known bad one, and flips its verdict on the answers in `wrong`."""

    def respond(call) -> ModelStep:
        answer = said(call).rsplit("Answer: ", 1)[-1]
        good = (answer not in BAD) != (answer in wrong)
        reply = json.dumps({"good": good, "reason": "scripted"})
        return ModelStep(output=[assistant_message(reply)])

    return ScriptedModel([ModelStep.respond(respond) for _ in range(100)])


@pytest.fixture
def models(monkeypatch):
    scripts = {
        "production": desk(GOOD),
        "candidate": desk(GOOD | {"Q-07": RASH}),
        "candidate (safe)": desk(GOOD),
    }
    monkeypatch.setattr(run, "model_for", lambda variant: scripts[variant.name])
    monkeypatch.setattr(run, "judge_model", lambda: judge())
    for name in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    return scripts


@pytest.fixture
def people(monkeypatch):
    monkeypatch.setenv("BOARD_PASSPHRASE_AMARA_OKAFOR", "amara-passphrase")
    monkeypatch.setenv("BOARD_PASSPHRASE_JONAS_WEBER", "jonas-passphrase")
    monkeypatch.setenv("BOARD_SECRET", "a-secret-for-this-test")
    return People(PEOPLE_FILE, os.environ["BOARD_SECRET"])


def case(inquiry_id: str, record: Record | None = None) -> Case:
    return Case(BY_ID[inquiry_id], LABELS[inquiry_id], record or Record(inquiry_id))


def acting(inquiry_id: str, tool: str, arguments: dict) -> Record:
    record = Record(inquiry_id)
    record.act(tool, arguments)
    return record


def read_report(state: Path) -> dict:
    return json.loads((state / "report.json").read_text())


async def sign_in(client, name, passphrase):
    reply = await client.post(
        "/login", data={"name": name, "passphrase": passphrase}, allow_redirects=False
    )
    assert reply.status == 303


@pytest.mark.lab("build-evals-for-an-ai-agent")
def test_a_candidate_that_answers_a_side_effect_report_is_refused_and_the_board_says_why(
    tmp_path, models, capsys
):
    asyncio.run(run.runs(tmp_path))
    swap = asyncio.run(run.demo(tmp_path))
    # The board's terminal is WIDTH columns: a longer line wraps its last character onto a line of its own.
    shown = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out).splitlines()
    assert [line for line in shown if len(line) > run.WIDTH] == []

    assert not swap.approved
    assert swap.reasons == [
        "Q-07: answered; the label says sent to safety",
        "passes 11 of 12; production passes 12",
    ]
    report = read_report(tmp_path)
    assert report["status"] == "done"
    assert report["source"] == "the run log"
    assert report["calibration"] == {"agreed": len(GRADED), "total": len(GRADED)}
    first = {row["id"]: row for row in report["runs"]["production"]}
    assert all(
        all(check["passed"] for check in row["checks"]) for row in first.values()
    )
    # The judge read every production answer the code checks passed, and nothing else.
    judged = {
        key
        for key, row in first.items()
        if any(check["name"] == "judge" for check in row["checks"])
    }
    assert judged == {
        key for key, label in LABELS.items() if label["route"] == "answer"
    }

    page = board.board_page(tmp_path)
    assert "Swap refused" in page
    assert "Q-07: answered; the label says sent to safety" in page
    assert "Sign the swap" not in page
    rash_card = next(
        card for card in board.cards(report, tmp_path) if card.card_id == "Q-07"
    )
    assert rash_card.column == "Needs you"
    assert (
        rash_card.log[-1] == "Candidate: route: answered; the label says sent to safety"
    )
    assert "Labelled by Dr. Amara Okafor" in board.card_page(tmp_path, "Q-07")


def test_the_test_set_is_rebuilt_from_langfuse_traces_and_the_latest_run_of_a_case_wins(
    tmp_path,
):
    observations = {
        "t-old": [
            {
                "name": "answer",
                "input": {"text": "Old.", "sections": ["4.8"]},
                "startTime": "1",
            }
        ],
        "t-new": [
            {"name": "search_approved", "input": '{"query": "rash"}', "startTime": "1"},
            {
                "name": "nvidia/nemotron-3.5-lightning",
                "type": "GENERATION",
                "usageDetails": {"input": 900, "output": 40},
                "startTime": "2",
            },
            {
                "name": "send_to_safety",
                "input": '{"summary": "Rash after dose two."}',
                "startTime": "3",
            },
        ],
    }
    roots = [
        {"traceId": "t-old", "sessionId": "Q-07", "startTime": "2026-12-01T09:00:00Z"},
        {"traceId": "t-new", "sessionId": "Q-07", "startTime": "2026-12-01T10:00:00Z"},
    ]

    def get(path: str) -> dict:
        assert path.startswith("/api/public/v2/observations?")
        if "isRootObservation=true" in path:
            assert "name=Medical+inquiry&" in path
            # Two pages, so the cursor is followed.
            if "cursor=" not in path:
                return {"data": roots[:1], "meta": {"cursor": "next"}}
            return {"data": roots[1:], "meta": {}}
        trace = path.split("traceId=")[1].split("&")[0]
        return {"data": observations[trace], "meta": {}}

    record = pull(get, "Medical inquiry")["Q-07"]
    assert (record.route, record.text, record.searches) == (
        "safety",
        "Rash after dose two.",
        ["rash"],
    )
    assert (record.tokens_in, record.tokens_out) == (900, 40)

    cases, source = build(tmp_path, get)
    assert source == "Langfuse" and len(cases) == len(LABELS)
    rash = next(found for found in cases if found.inquiry_id == "Q-07").record
    assert rash.cost == production_variant(tmp_path).cost(900, 40)
    # A labelled inquiry with no trace is still a case, and it fails: the desk never acted on it.
    silent = next(found for found in cases if found.inquiry_id == "Q-01")
    assert not score(silent).passed
    assert score(silent).checks[0].said == "never acted; the label says answered"


def test_code_checks_catch_a_wrong_route_a_wrong_section_and_a_number_the_label_never_printed():
    rash = score(case("Q-07", acting("Q-07", *RASH)))
    assert [check.said for check in rash.critical] == [
        "answered; the label says sent to safety"
    ]

    declined = score(case("Q-07", acting("Q-07", "decline", {"reason": "x"})))
    assert not declined.passed and not declined.critical

    wrong = acting("Q-10", "answer", {"text": "Up to 5 mg a day.", "sections": ["4.8"]})
    failed = [
        check.said for check in code_checks(case("Q-10"), wrong) if not check.passed
    ]
    assert failed == ["cites §4.8; the answer is in §4.2", "not in the cited text: 5"]
    made_up = acting(
        "Q-10", "answer", {"text": "Up to 20 mg a day.", "sections": ["4.2"]}
    )
    failed_names = [
        check.name for check in code_checks(case("Q-10"), made_up) if not check.passed
    ]
    assert failed_names == ["numbers"]
    # A number the doctor gave in the question may be repeated: Q-03's 25 mL/min is not in §4.2.1.
    echo = acting(
        "Q-03", "answer", {"text": "At 25 mL/min start at 5 mg.", "sections": ["4.2.1"]}
    )
    assert all(check.passed for check in code_checks(case("Q-03"), echo))
    ghost = acting("Q-10", "answer", {"text": "10 mg.", "sections": ["9.9", "4.2"]})
    assert (
        code_checks(case("Q-10"), ghost)[1].said
        == "cites §9.9, which the label does not have"
    )


def test_the_judge_counts_only_once_it_agrees_with_the_hand_grades():
    two_wrong = {row["answer"] for row in GRADED[:3]}
    calibration = asyncio.run(calibrate(judge(two_wrong)))
    assert (calibration.agreed, calibration.total, calibration.trusted) == (
        len(GRADED) - 3,
        len(GRADED),
        False,
    )

    bad = acting("Q-01", "answer", {"text": GRADED[1]["answer"], "sections": ["6.4"]})
    untrusted = [score(case("Q-01"), bad)]
    asyncio.run(grade(judge(), untrusted, calibration))
    assert untrusted[0].passed and untrusted[0].checks[-1].said.startswith(
        "not counted"
    )

    trusted = [score(case("Q-01"), bad)]
    asyncio.run(grade(judge(), trusted, Calibration(len(GRADED), len(GRADED))))
    assert not trusted[0].passed and trusted[0].checks[-1].name == "judge"


def test_the_gate_refuses_fewer_passes_without_a_critical_miss_and_ships_an_equal_candidate():
    baseline = [score(case(key, acting(key, *GOOD[key]))) for key in LABELS]
    worse = [
        *baseline[:-1],
        score(case("Q-12", acting("Q-12", "decline", {"reason": "x"}))),
    ]
    assert gate(baseline, worse).reasons == ["passes 11 of 12; production passes 12"]
    assert gate(baseline, baseline).approved


def test_a_model_that_never_returns_is_cut_off_and_scored_as_a_run_that_never_acted(
    monkeypatch,
):
    class Stalled(ScriptedModel):
        async def get_response(self, *args, **kwargs):
            await asyncio.sleep(3600)

    monkeypatch.setattr("evals.desk.RUN_SECONDS", 0.05)
    record = asyncio.run(
        handle(BY_ID["Q-07"], candidate_variant("default"), Stalled([]))
    )
    assert record.route is None
    assert not code_checks(Case(BY_ID["Q-07"], LABELS["Q-07"], record), record)[
        0
    ].passed


def test_a_refused_candidate_cannot_be_signed(tmp_path, models):
    asyncio.run(run.runs(tmp_path))
    asyncio.run(run.demo(tmp_path))
    with pytest.raises(production.Refused, match="the gate refused this candidate"):
        production.switch(tmp_path, read_report(tmp_path), production.SIGNER, NOW)
    assert production.switched(tmp_path) is None
    assert not (tmp_path / "production.json").exists()
    assert len(production.read_decisions(tmp_path)) == 1


def test_production_and_calibration_are_cached(tmp_path, models, monkeypatch):
    asyncio.run(run.runs(tmp_path))
    counted = judge()
    monkeypatch.setattr(run, "judge_model", lambda: counted)
    asyncio.run(run.demo(tmp_path))
    first = len(counted.calls)
    assert first > 0

    asyncio.run(run.demo(tmp_path))
    # Only the candidate's own answers are graded again: calibration and production are read back.
    assert len(counted.calls) - first < first


def test_the_report_is_written_while_it_runs(tmp_path, models, monkeypatch):
    asyncio.run(run.runs(tmp_path))
    seen = []
    original = run.write_report

    def watch(state, report):
        original(state, report)
        seen.append((report["status"], sorted(report["runs"])))

    monkeypatch.setattr(run, "write_report", watch)
    asyncio.run(run.demo(tmp_path))
    assert seen[0][0] == "running"
    assert ("running", ["production"]) in seen
    assert seen[-1] == ("done", ["candidate", "production"])
    assert "KaaS Pharma" in board.board_page(tmp_path)


@pytest.mark.lab("build-evals-for-an-ai-agent")
def test_only_dr_okafor_signs_a_model_swap(tmp_path, models, people):
    asyncio.run(run.runs(tmp_path))
    swap = asyncio.run(run.demo(tmp_path, candidate="safe"))
    assert swap.approved
    assert production.switched(tmp_path) is None

    async def scenario():
        async with TestClient(TestServer(board.app(tmp_path, people))) as client:
            # Nobody is signed in: the board and the signature itself go to the sign-in page.
            home = await client.get("/", allow_redirects=False)
            assert home.headers["Location"] == "/login"
            unsigned = await client.post(
                "/cards/S-1/sign", data={}, allow_redirects=False
            )
            assert unsigned.headers["Location"] == "/login"
            wrong = await client.post(
                "/login", data={"name": "Jonas Weber", "passphrase": "no"}
            )
            assert wrong.status == 401

            # Jonas proposed the swap. He is Jonas whatever the form claims, and the swap refuses him.
            await sign_in(client, "Jonas Weber", "jonas-passphrase")
            forged = {"decision": "sign", "signed_by": "Dr. Amara Okafor"}
            await client.post("/cards/S-1/sign", data=forged, allow_redirects=False)
            assert production.switched(tmp_path) is None
            assert not (tmp_path / "production.json").exists()
            page = await (await client.get("/cards/S-1")).text()
            assert "only Dr. Amara Okafor signs a model swap" in page
            await client.post("/logout", allow_redirects=False)

            # Dr. Okafor signs in and signs; production now runs the candidate and the card is Done.
            await sign_in(client, "Dr. Amara Okafor", "amara-passphrase")
            page = await (await client.get("/cards/S-1")).text()
            assert "Sign the swap" in page
            await client.post(
                "/cards/S-1/sign", data={"decision": "sign"}, allow_redirects=False
            )
            signed = production.switched(tmp_path)
            assert signed and signed["signed_by"] == "Dr. Amara Okafor"
            home = await (await client.get("/")).text()
            assert "signed by Dr. Amara Okafor" in home

    asyncio.run(scenario())
    assert production_variant(tmp_path).name == "production"
