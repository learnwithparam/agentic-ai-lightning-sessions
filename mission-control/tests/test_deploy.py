"""The deploy step: each AI employee signs in as itself for one person (Keycloak), every tool asks OpenFGA
first, and every run lands in Langfuse with the card as its session.

The first tests fake the two services so `make check` stays offline. The test marked `stack` runs the
same board against the real Keycloak, OpenFGA and Langfuse from compose.yaml; `make stack-check` runs it.
"""

import asyncio
import json
import os
import time
import urllib.request
import uuid
from base64 import b64encode
from dataclasses import replace
from typing import NamedTuple

import pytest
from agents import set_trace_processors, set_tracing_disabled
from agents.testing import ScriptedModel, assistant_message, function_call
from agents.tracing import generation_span, trace

from workforce.access import Access, Badge, client_id
from workforce.board import Board, Card
from workforce.issues import Issues
from workforce.mission import SEED, NotAllowed, sign, work
from workforce.people import Person
from workforce.sources import Readers
from workforce.traces import Langfuse, install

FACTS = {
    ("agent:support-resolver", "employee", "desk:support"),
    ("agent:engineering-analyst", "employee", "desk:engineering"),
    ("user:param", "staff", "desk:support"),
    ("user:param", "staff", "desk:engineering"),
    ("user:sam", "staff", "desk:engineering"),
    ("user:param", "can_approve", "desk:support"),
}


def fake(user: str) -> Access:
    return Access(lambda *fact: fact in FACTS, lambda agent: Badge(client_id(agent), user), user)


class Desk(NamedTuple):
    board: Board
    card: Card
    issues: Issues


@pytest.fixture
def desk(tmp_path):
    board, issues = Board.open(tmp_path / "board.json"), Issues.open(tmp_path / "github.json")
    board.cards.append(replace(SEED, card_id=f"C-{uuid.uuid4().hex[:8]}", runs=[], log=[]))
    return Desk(board, board.cards[0], issues)


def reply_run(emit_traces: bool = False) -> ScriptedModel:
    return ScriptedModel(
        [
            [function_call("transfer_to_support_resolver", {}, call_id="h1")],
            [function_call("read_issue", {"number": 412}, call_id="r1")],
            [function_call("search_linear", {"query": "csv export timeout"}, call_id="s1")],
            [function_call("request_reply", {"body": "It ships in release 2.14 [EXP-41]."}, call_id="d1")],
            [assistant_message("The reply is waiting for approval.")],
        ],
        emit_traces=emit_traces,
    )


def test_an_employee_acting_for_someone_off_the_support_desk_is_refused(desk):
    board = desk.board
    card = desk.card
    issues = desk.issues
    model = reply_run()

    asyncio.run(work(board, card, issues, Readers("kaas", "U_ENG"), model=model, access=fake("sam")))

    # The resolver may work support, but not for sam: the issue is refused, nothing queued, card not Done.
    assert "Refused: sam has no access to the support desk" in str(model.calls[2].input)
    assert card.pending is None and card.column == "Backlog"
    assert any(line.startswith("Gate: sam has no access") for line in card.log)


def test_an_approver_openfga_refuses_leaves_the_reply_waiting(desk):
    board = desk.board
    card = desk.card
    issues = desk.issues
    asyncio.run(work(board, card, issues, Readers("kaas", "U_ENG"), model=reply_run(), access=fake("param")))
    assert card.column == "Needs you"

    contractor = Person("Sam", "contractor", "sam")
    with pytest.raises(NotAllowed):
        sign(board, card, issues, contractor, True, fake("param"))

    assert issues.comments(412) == [] and card.column == "Needs you" and card.pending
    assert "Sam may not approve this" in card.log[-1]

    # A role named support approver is not enough once OpenFGA answers: only its can_approve counts.
    renamed = Person("Sam", "support approver", "sam")
    with pytest.raises(NotAllowed):
        sign(board, card, issues, renamed, True, fake("param"))
    assert issues.comments(412) == [] and card.column == "Needs you"


@pytest.fixture
def traced():
    sent = []
    set_trace_processors([Langfuse(sent.append)])
    set_tracing_disabled(False)
    yield sent
    set_tracing_disabled(True)
    set_trace_processors([])


def spans(body):
    return body["resourceSpans"][0]["scopeSpans"][0]["spans"]


def attr(span, key):
    return next((item["value"] for item in span["attributes"] if item["key"] == key), None)


def test_every_run_on_a_card_is_one_langfuse_trace_in_the_card_session(desk, traced):
    board = desk.board
    card = desk.card
    issues = desk.issues

    asyncio.run(work(board, card, issues, Readers("kaas", "U_ENG"), model=reply_run(emit_traces=True)))

    [body] = traced
    root, *rest = spans(body)
    assert attr(root, "langfuse.session.id") == {"stringValue": card.card_id}
    assert len(root["traceId"]) == 32 and all(len(span["spanId"]) == 16 for span in spans(body))
    # Every span hangs off the run, and the tools and the handoff are there by name.
    known = {span["spanId"] for span in spans(body)}
    assert all(span["parentSpanId"] in known for span in rest)
    kinds = {(span["name"], attr(span, "langfuse.observation.type")["stringValue"]) for span in rest}
    assert {("read_issue", "tool"), ("request_reply", "tool"), ("Support resolver", "agent")} <= kinds
    assert sum(kind == "generation" for _, kind in kinds) >= 1


def test_a_model_call_carries_its_tokens_and_cost(traced, monkeypatch):
    monkeypatch.setenv("WORKFORCE_PRICE_IN", "1.0")
    monkeypatch.setenv("WORKFORCE_PRICE_OUT", "10.0")
    with trace("C-9 cost", group_id="C-9"):
        with generation_span(model="m", usage={"input_tokens": 2000, "output_tokens": 200}):
            pass

    gen = spans(traced[0])[1]
    assert attr(gen, "gen_ai.usage.input_tokens") == {"intValue": "2000"}
    assert attr(gen, "gen_ai.usage.cost") == {"doubleValue": pytest.approx((2000 * 1 + 200 * 10) / 1e6)}


def test_a_langfuse_outage_never_fails_a_card(desk):
    def down(body):
        raise OSError("connection refused")

    board = desk.board

    card = desk.card

    issues = desk.issues
    set_trace_processors([Langfuse(down)])
    set_tracing_disabled(False)
    try:
        asyncio.run(work(board, card, issues, Readers("kaas", "U_ENG"), model=reply_run(emit_traces=True)))
    finally:
        set_tracing_disabled(True)
        set_trace_processors([])
    assert card.column == "Needs you"


def langfuse(path: str) -> dict:
    key = b64encode(
        f"{os.environ['LANGFUSE_PUBLIC_KEY']}:{os.environ['LANGFUSE_SECRET_KEY']}".encode()
    ).decode()
    req = urllib.request.Request(  # noqa: S310 - the local stack
        f"{os.environ['LANGFUSE_HOST']}{path}", headers={"Authorization": f"Basic {key}"}
    )
    with urllib.request.urlopen(req, timeout=10) as res:  # noqa: S310
        return json.load(res)


@pytest.mark.stack
@pytest.mark.lab("deploy-agentic-ai-systems-for-engineers")
def test_the_board_runs_behind_keycloak_openfga_and_langfuse(desk):
    board = desk.board
    card = desk.card
    issues = desk.issues
    assert install(), "make stack-check sets LANGFUSE_HOST and its keys"
    try:
        sam, param = Access.from_env(user="sam"), Access.from_env(user="param")
        # Keycloak, not the prompt, says who holds each token and for whom.
        assert param.badge("Support resolver") == Badge("support-resolver", "param")

        asyncio.run(work(board, card, issues, Readers("kaas", "U_ENG"), model=reply_run(True), access=sam))
        assert card.column == "Backlog" and any(
            "sam has no access to the support desk" in line for line in card.log
        )

        asyncio.run(work(board, card, issues, Readers("kaas", "U_ENG"), model=reply_run(True), access=param))
        assert card.column == "Needs you"

        # sam is not support staff; OpenFGA's can_approve needs the approver and the staff relation.
        with pytest.raises(NotAllowed):
            sign(board, card, issues, Person("Sam", "contractor", "sam"), True, param)
        assert issues.comments(412) == [] and card.column == "Needs you"
        sign(board, card, issues, Person("Param", "support approver", "param"), True, param)
        assert issues.replied(412, card.card_id)["approved_by"] == "Param" and card.column == "Done"
    finally:
        set_tracing_disabled(True)
        set_trace_processors([])

    # Both runs on the card reach Langfuse as one session, sam's and param's, within seconds.
    for _ in range(60):
        runs = langfuse(f"/api/public/v2/observations?sessionId={card.card_id}&limit=10")["data"]
        if len(runs) == 2:
            break
        time.sleep(1)
    assert sorted(run["userId"] for run in runs) == ["param", "sam"], runs
    names = {
        observation["name"]
        for run in runs
        for observation in langfuse(f"/api/public/v2/observations?traceId={run['traceId']}&limit=100")["data"]
    }
    assert {"read_issue", "request_reply", "Support resolver"} <= names
