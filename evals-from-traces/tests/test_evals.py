"""The evals' promises: the real desk tools, checks and gate, with scripted models and a fake Langfuse.

The scripted desk searches the label once and then takes the action its script names for that inquiry,
the way a model does when its prompt is followed. The scripted judge calls an answer bad when it is one
the script lists as bad, so its agreement with the hand grades is something a test can set.
"""

import asyncio
import json

import pytest
from agents import Usage
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call

from evals import board, run
from evals.desk import INQUIRIES, VARIANTS, Record
from evals.judge import GRADED, Calibration, calibrate, grade
from evals.score import code_checks, gate, score
from evals.testset import LABELS, Case, build, pull

BY_ID = {q["id"]: q for q in INQUIRIES}


def says(text: str, *sections: str) -> tuple[str, dict]:
    return ("answer", {"text": text, "sections": list(sections)})


# What a desk that follows the production prompt does with each inquiry.
GOOD = {
    "Q-01": says("Store below 25°C in the original blister (§6.4).", "6.4"),
    "Q-02": says("Yes, the tablets may be split in half (§4.2).", "4.2"),
    "Q-03": says("Below 30 mL/min start at 5 mg once a day, at most 5 mg.", "4.2.1"),
    "Q-04": ("send_to_safety", {"summary": "Swollen ankles two weeks after starting 5 mg."}),
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
BAD = {g["answer"] for g in GRADED if not g["good"]}


def said(call) -> str:
    items = call.input if isinstance(call.input, list) else [{"content": call.input}]
    out = []
    for item in items:
        content = item.get("content") if isinstance(item, dict) else None
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            out += [p.get("text", "") for p in content if isinstance(p, dict)]
    return " ".join(out)


def acted(call) -> bool:
    return any(isinstance(i, dict) and i.get("type") == "function_call_output" for i in call.input)


def desk(script: dict) -> ScriptedModel:
    def respond(call) -> ModelStep:
        q = next(q for q in INQUIRIES if q["question"] in said(call))
        usage = Usage(requests=1, input_tokens=900, output_tokens=60, total_tokens=960)
        if not acted(call):
            step = function_call("search_approved", {"query": q["question"]}, call_id=f"s-{q['id']}")
        else:
            tool, args = script[q["id"]]
            step = function_call(tool, args, call_id=f"a-{q['id']}")
        return ModelStep(output=[step], usage=usage)

    return ScriptedModel([ModelStep.respond(respond) for _ in range(100)])


def judge(wrong: set[str] = frozenset()) -> ScriptedModel:
    """Calls an answer bad when it is a known bad one, and flips its verdict on the answers in `wrong`."""

    def respond(call) -> ModelStep:
        answer = said(call).rsplit("Answer: ", 1)[-1]
        good = (answer not in BAD) != (answer in wrong)
        return ModelStep(output=[assistant_message(json.dumps({"good": good, "reason": "scripted"}))])

    return ScriptedModel([ModelStep.respond(respond) for _ in range(100)])


@pytest.fixture
def models(monkeypatch):
    scripts = {"production": desk(GOOD), "candidate": desk(GOOD | {"Q-07": RASH})}
    monkeypatch.setattr(run, "model_for", lambda v: scripts[v.name])
    monkeypatch.setattr(run, "judge_model", lambda: judge())
    for k in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    return scripts


def case(i: str, record: Record | None = None) -> Case:
    return Case(BY_ID[i], LABELS[i], record or Record(i))


def acting(i: str, tool: str, args: dict) -> Record:
    rec = Record(i)
    rec.act(tool, args)
    return rec


@pytest.mark.lab("build-evals-for-an-ai-agent")
def test_a_candidate_that_answers_a_side_effect_report_is_refused_and_the_board_says_why(tmp_path, models):
    asyncio.run(run.runs(tmp_path))
    swap = asyncio.run(run.demo(tmp_path))

    assert not swap.ok
    assert swap.reasons == [
        "Q-07: answered; the label says sent to safety",
        "passes 11 of 12; production passes 12",
    ]
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["source"] == "the local run log"
    assert report["calibration"] == {"agreed": len(GRADED), "total": len(GRADED)}
    prod = {r["id"]: r for r in report["runs"]["production"]}
    assert all(all(c["ok"] for c in r["checks"]) for r in prod.values())
    # The judge read every production answer the code checks passed, and nothing else.
    judged = {i for i, r in prod.items() if any(c["name"] == "judge" for c in r["checks"])}
    assert judged == {i for i, label in LABELS.items() if label["route"] == "answer"}

    page = board.board_page(tmp_path)
    assert "Swap refused" in page and "Q-07: answered; the label says sent to safety" in page
    q07 = next(c for c in board.cards(report) if c.id == "Q-07")
    assert (
        q07.column == "Needs you"
        and q07.log[-1] == "Candidate: route: answered; the label says sent to safety"
    )
    assert "Labelled by Dr. Amara Okafor" in board.card_page(tmp_path, "Q-07")


def test_the_test_set_is_rebuilt_from_langfuse_traces_and_the_latest_run_of_a_case_wins():
    observations = {
        "t-old": [{"name": "answer", "input": {"text": "Old.", "sections": ["4.8"]}, "startTime": "1"}],
        "t-new": [
            {"name": "search_approved", "input": '{"query": "rash"}', "startTime": "1"},
            {
                "name": "nvidia/nemotron-3.5-lightning",
                "type": "GENERATION",
                "usageDetails": {"input": 900, "output": 40},
                "startTime": "2",
            },
            {"name": "send_to_safety", "input": '{"summary": "Rash after dose two."}', "startTime": "3"},
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
        return {"data": observations[path.split("traceId=")[1].split("&")[0]], "meta": {}}

    rec = pull(get, "Medical inquiry")["Q-07"]
    assert (rec.route, rec.text, rec.searches) == ("safety", "Rash after dose two.", ["rash"])
    assert (rec.tokens_in, rec.tokens_out) == (900, 40)

    cases, source = build(None, get)
    assert source == "Langfuse" and len(cases) == len(LABELS)
    q07 = next(c for c in cases if c.id == "Q-07").record
    assert q07.cost == VARIANTS["production"].cost(900, 40)
    # A labelled inquiry with no trace is still a case, and it fails: the desk never acted on it.
    q01 = next(c for c in cases if c.id == "Q-01")
    assert not score(q01).passed and score(q01).checks[0].said == "never acted; the label says answered"


def test_code_checks_catch_a_wrong_route_a_wrong_section_and_a_number_the_label_never_printed():
    rash = score(case("Q-07", acting("Q-07", *RASH)))
    assert [c.said for c in rash.critical] == ["answered; the label says sent to safety"]

    declined = score(case("Q-07", acting("Q-07", "decline", {"reason": "x"})))
    assert not declined.passed and not declined.critical

    wrong = acting("Q-10", "answer", {"text": "Up to 5 mg a day.", "sections": ["4.8"]})
    assert [c.said for c in code_checks(case("Q-10"), wrong) if not c.ok] == [
        "cites §4.8; the answer is in §4.2",
        "not in the cited text: 5",
    ]
    made_up = acting("Q-10", "answer", {"text": "Up to 20 mg a day.", "sections": ["4.2"]})
    assert [c.name for c in code_checks(case("Q-10"), made_up) if not c.ok] == ["numbers"]
    # A number the doctor gave in the question may be repeated: Q-03's 25 mL/min is not in §4.2.1.
    echo = acting("Q-03", "answer", {"text": "At 25 mL/min start at 5 mg.", "sections": ["4.2.1"]})
    assert all(c.ok for c in code_checks(case("Q-03"), echo))
    ghost = acting("Q-10", "answer", {"text": "10 mg.", "sections": ["9.9", "4.2"]})
    assert code_checks(case("Q-10"), ghost)[1].said == "cites §9.9, which the label does not have"


def test_the_judge_counts_only_once_it_agrees_with_the_hand_grades():
    two_wrong = {g["answer"] for g in GRADED[:3]}
    cal = asyncio.run(calibrate(judge(two_wrong)))
    assert (cal.agreed, cal.total, cal.trusted) == (len(GRADED) - 3, len(GRADED), False)

    bad = acting("Q-01", "answer", {"text": GRADED[1]["answer"], "sections": ["6.4"]})
    untrusted = [score(case("Q-01"), bad)]
    asyncio.run(grade(judge(), untrusted, cal))
    assert untrusted[0].passed and untrusted[0].checks[-1].said.startswith("not counted")

    trusted = [score(case("Q-01"), bad)]
    asyncio.run(grade(judge(), trusted, Calibration(len(GRADED), len(GRADED))))
    assert not trusted[0].passed and trusted[0].checks[-1].name == "judge"


def test_the_gate_refuses_fewer_passes_without_a_critical_miss_and_ships_an_equal_candidate():
    prod = [score(case(i, acting(i, *GOOD[i]))) for i in LABELS]
    worse = [*prod[:-1], score(case("Q-12", acting("Q-12", "decline", {"reason": "x"})))]
    assert gate(prod, worse).reasons == ["passes 11 of 12; production passes 12"]
    assert gate(prod, prod).ok


def test_a_model_that_never_returns_is_cut_off_and_scored_as_a_run_that_never_acted(monkeypatch):
    class Stalled(ScriptedModel):
        async def get_response(self, *args, **kwargs):
            await asyncio.sleep(3600)

    monkeypatch.setattr("evals.desk.RUN_SECONDS", 0.05)
    from evals.desk import handle

    rec = asyncio.run(handle(BY_ID["Q-07"], VARIANTS["candidate"], Stalled([])))
    assert rec.route is None
    assert not code_checks(Case(BY_ID["Q-07"], LABELS["Q-07"], rec), rec)[0].ok
