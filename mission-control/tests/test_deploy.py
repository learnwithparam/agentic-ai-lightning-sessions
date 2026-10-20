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

import pytest
from agents import set_trace_processors, set_tracing_disabled
from agents.testing import ScriptedModel, assistant_message, function_call
from agents.tracing import generation_span, trace

from workforce.access import Access, Badge, client_id
from workforce.board import Board
from workforce.ledger import Ledger
from workforce.mission import SEED, ask_human, work
from workforce.sources import Readers
from workforce.traces import Langfuse, install

FACTS = {
    ("agent:support-resolver", "employee", "desk:billing"),
    ("agent:engineering-analyst", "employee", "desk:engineering"),
    ("user:param", "staff", "desk:billing"),
    ("user:param", "staff", "desk:engineering"),
    ("user:sam", "staff", "desk:engineering"),
    ("user:param", "can_approve", "desk:billing"),
}


def fake(user: str) -> Access:
    return Access(lambda *fact: fact in FACTS, lambda agent: Badge(client_id(agent), user), user)


@pytest.fixture
def desk(tmp_path):
    board, ledger = Board.open(tmp_path / "board.json"), Ledger.open(tmp_path / "ledger.json")
    board.cards.append(replace(SEED, id=f"C-{uuid.uuid4().hex[:8]}", runs=[], log=[]))
    return board, board.cards[0], ledger


def refund_run(emit_traces: bool = False) -> ScriptedModel:
    return ScriptedModel(
        [
            [function_call("transfer_to_support_resolver", {}, call_id="h1")],
            [function_call("find_charges", {"customer": "cus_812"}, call_id="c1")],
            [
                function_call(
                    "request_refund", {"charge_id": "ch_3002", "reason": "second charge"}, call_id="c2"
                )
            ],
            [assistant_message("The duplicate ch_3002 is waiting for your approval.")],
        ],
        emit_traces=emit_traces,
    )


def test_an_employee_acting_for_someone_off_the_billing_desk_is_refused(desk):
    board, card, ledger = desk
    model = refund_run()

    asyncio.run(work(board, card, ledger, Readers("acme", "U_ENG"), model=model, access=fake("sam")))

    # The resolver may work billing, but not for sam: both tools refused, nothing queued, card not Done.
    assert "Refused: sam has no access to the billing desk" in str(model.calls[2].input)
    assert card.pending is None and card.column == "Backlog"
    assert any(line.startswith("Gate: sam has no access") for line in card.log)


def test_an_approver_openfga_refuses_leaves_the_refund_waiting(desk):
    board, card, ledger = desk
    asyncio.run(work(board, card, ledger, Readers("acme", "U_ENG"), model=refund_run(), access=fake("param")))
    assert card.column == "Needs you"

    async def sam(card):
        return "telegram:sam", True

    asyncio.run(ask_human(board, card, ledger, sam, fake("param")))

    assert ledger.refunds == [] and card.column == "Needs you" and card.pending
    assert "telegram:sam may not approve refunds" in card.log[-1]


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
    return next((a["value"] for a in span["attributes"] if a["key"] == key), None)


def test_every_run_on_a_card_is_one_langfuse_trace_in_the_card_session(desk, traced):
    board, card, ledger = desk

    asyncio.run(work(board, card, ledger, Readers("acme", "U_ENG"), model=refund_run(emit_traces=True)))

    [body] = traced
    root, *rest = spans(body)
    assert attr(root, "langfuse.session.id") == {"stringValue": card.id}
    assert len(root["traceId"]) == 32 and all(len(s["spanId"]) == 16 for s in spans(body))
    # Every span hangs off the run, and the tools and the handoff are there by name.
    known = {s["spanId"] for s in spans(body)}
    assert all(s["parentSpanId"] in known for s in rest)
    kinds = {(s["name"], attr(s, "langfuse.observation.type")["stringValue"]) for s in rest}
    assert {("find_charges", "tool"), ("request_refund", "tool"), ("Support resolver", "agent")} <= kinds
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

    board, card, ledger = desk
    set_trace_processors([Langfuse(down)])
    set_tracing_disabled(False)
    try:
        asyncio.run(work(board, card, ledger, Readers("acme", "U_ENG"), model=refund_run(emit_traces=True)))
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
    board, card, ledger = desk
    assert install(), "make stack-check sets LANGFUSE_HOST and its keys"
    try:
        sam, param = Access.from_env(user="sam"), Access.from_env(user="param")
        # Keycloak, not the prompt, says who holds each token and for whom.
        assert param.badge("Support resolver") == Badge("support-resolver", "param")

        asyncio.run(work(board, card, ledger, Readers("acme", "U_ENG"), model=refund_run(True), access=sam))
        assert card.column == "Backlog" and any(
            "sam has no access to the billing desk" in x for x in card.log
        )

        asyncio.run(work(board, card, ledger, Readers("acme", "U_ENG"), model=refund_run(True), access=param))
        assert card.column == "Needs you"

        def tap(who):
            async def approver(card):
                return who, True

            return approver

        # sam is a billing approver but not billing staff; OpenFGA's can_approve needs both.
        asyncio.run(ask_human(board, card, ledger, tap("terminal:sam"), param))
        assert ledger.refunds == [] and card.column == "Needs you"
        asyncio.run(ask_human(board, card, ledger, tap("terminal:param"), param))
        assert ledger.refunds[0]["approved_by"] == "terminal:param" and card.column == "Done"
    finally:
        set_tracing_disabled(True)
        set_trace_processors([])

    # Both runs on the card reach Langfuse as one session, sam's and param's, within seconds.
    for _ in range(60):
        runs = langfuse(f"/api/public/v2/observations?sessionId={card.id}&limit=10")["data"]
        if len(runs) == 2:
            break
        time.sleep(1)
    assert sorted(r["userId"] for r in runs) == ["param", "sam"], runs
    names = {
        o["name"]
        for r in runs
        for o in langfuse(f"/api/public/v2/observations?traceId={r['traceId']}&limit=100")["data"]
    }
    assert {"find_charges", "request_refund", "Support resolver"} <= names
