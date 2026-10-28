"""The reader's promises, against the real claims system and ledger with a scripted model.

The scripted model bills each call the way a provider does: the instructions and text by length, a photo
by its detail, and the instructions as cached input when they match an earlier call word for word.
"""

import asyncio
import json
import re
from dataclasses import replace

import pytest
from agents import Usage
from agents.testing import ModelStep, ScriptedModel, assistant_message
from aiohttp.test_utils import TestClient, TestServer
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

from claims.board import PEOPLE_FILE, TASKS, Desk, app
from claims.reader import LADDER, SEED, TEAM, Levers, assistant_item, passes, resume_after_raise, settle, work
from claims.system import Claims, Refused
from workforce.board import Board, price
from workforce.people import People

PHOTO = {"high": 1000, "low": 300}
# Reasoning tokens a read spends at the model's default effort and at low effort, billed as output.
THINK = {None: 1100, "low": 250}
FILES = {
    "CL-301": {"parts": ["front bumper", "bonnet", "left headlamp"], "vehicles": 1, "total": "£2,656.80"},
    "CL-302": {"parts": ["rear bumper", "boot lid"], "vehicles": 1, "total": "£1,738.80"},
    "CL-303": {"parts": ["roof panel", "windscreen"], "vehicles": 11, "total": None},
}
ADJUSTER = "Nina Kowalski"
HANDLER = "Leo Grant"


@pytest.fixture(autouse=True)
def prices(monkeypatch):
    monkeypatch.setenv("WORKFORCE_PRICE_IN", "0.10")
    monkeypatch.setenv("WORKFORCE_PRICE_CACHED", "0.01")
    monkeypatch.setenv("WORKFORCE_PRICE_OUT", "0.50")


@pytest.fixture
def people(monkeypatch):
    monkeypatch.setenv("BOARD_PASSPHRASE_LEO_GRANT", "leo-passphrase")
    monkeypatch.setenv("BOARD_PASSPHRASE_NINA_KOWALSKI", "nina-passphrase")
    monkeypatch.setenv("BOARD_SECRET", "a-secret-for-this-test")
    return People(PEOPLE_FILE, "a-secret-for-this-test")


def parts(item) -> list[dict]:
    content = item.get("content") if isinstance(item, dict) else None
    return content if isinstance(content, list) else []


def images(call) -> list[dict]:
    return [part for item in call.input for part in parts(item) if part.get("type") == "input_image"]


def text(call) -> str:
    return " ".join(part.get("text", "") for item in call.input for part in parts(item))


def tokens(item) -> int:
    return sum(
        PHOTO[part["detail"]] if part.get("type") == "input_image" else len(part.get("text", "")) // 4
        for part in parts(item)
    )


def provider(files: dict, invent: bool = False) -> ScriptedModel:
    seen: set[tuple[str, ...]] = set()

    def bill(call) -> ModelStep:
        system = call.system_instructions or ""
        claim_id = next(key for key in files if key in text(call))
        quoted = any("Repair quote" in part.get("text", "") for part in parts(call.input[-1]))
        out = {**files[claim_id], "note": "Damage matches what the policyholder described."}
        out["total"] = out["total"] if quoted or invent else None
        # Like the provider's cache: the longest start of this call that an earlier call sent word for word.
        chunks = [(system, len(system) // 4)] + [
            (json.dumps(item, sort_keys=True), tokens(item)) for item in call.input
        ]
        keys = [tuple(label for label, _ in chunks[: count + 1]) for count in range(len(chunks))]
        cached = sum(size for (_, size), key in zip(chunks, keys, strict=True) if key in seen)
        seen.update(keys)
        effort = call.model_settings.reasoning.effort if call.model_settings.reasoning else None
        reasoning = THINK[effort]
        body = json.dumps(out)
        total_in = sum(size for _, size in chunks)
        usage = Usage(
            requests=1,
            input_tokens=total_in,
            output_tokens=len(body) // 4 + reasoning,
            total_tokens=total_in + len(body) // 4 + reasoning,
            input_tokens_details=InputTokensDetails(cached_tokens=cached, cache_write_tokens=0),
            output_tokens_details=OutputTokensDetails(reasoning_tokens=reasoning),
        )
        return ModelStep(output=[assistant_message(body)], usage=usage)

    return ScriptedModel([ModelStep.respond(bill) for _ in range(40)])


def fresh_board(folder) -> Board:
    board = Board(folder / "board.json", [replace(card, runs=[], log=[]) for card in SEED])
    board.save()
    return board


def run(folder, levers: Levers, files: dict = FILES, invent: bool = False) -> tuple[Board, ScriptedModel]:
    model = provider(files, invent)
    board = fresh_board(folder)
    for card in board.cards:
        asyncio.run(work(board, card, levers, model))
    return board, model


@pytest.mark.lab("cut-ai-agent-costs-with-prompt-caching")
def test_all_three_levers_file_every_claim_at_the_ledgers_cost(tmp_path):
    board, model = run(tmp_path, LADDER[-1][1])
    claims = Claims.open(tmp_path)

    assert len({call.system_instructions for call in model.calls}) == 1, (
        "every call starts with the same policy"
    )
    assert all(call.model_settings.reasoning.effort == "low" for call in model.calls), (
        "every read thinks less"
    )
    assert {item["detail"] for call in model.calls for item in images(call)} == {"low"}
    assert [card.column for card in board.cards] == ["Done", "Done", "Done"]
    assert claims.filed("CL-301")["total"] == "£2,656.80"
    hail = board.card("C-9")
    assert claims.filed("CL-303")["total"] is None and "an assessor prices it" in hail.log[-1]
    for card in board.cards:
        rows = claims.ledger(card.issue)
        assert len(rows) == len(claims.claim(card.issue)["documents"])
        assert card.cost == pytest.approx(sum(row["cost"] for row in rows))
    later = claims.ledger("CL-303")[1]
    assert later["cached"] > 0
    assert later["cost"] == pytest.approx(
        ((later["input"] - later["cached"]) * 0.10 + later["cached"] * 0.01 + later["output"] * 0.50) / 1e6
    )


def test_without_levers_the_thread_grows_and_the_hail_claim_stops_at_its_budget(tmp_path):
    board, model = run(tmp_path, LADDER[0][1])
    claims = Claims.open(tmp_path)

    assert len({call.system_instructions for call in model.calls}) == len(model.calls), "a timestamp on top"
    hail = [call for call in model.calls if "CL-303" in text(call)]
    assert [len(images(call)) for call in hail] == list(range(1, len(hail) + 1)), "every earlier photo again"
    assert {item["detail"] for call in model.calls for item in images(call)} == {"high"}
    assert all(call.model_settings.reasoning is None for call in model.calls)
    assert sum(row["cached"] for claim in ("CL-301", "CL-302", "CL-303") for row in claims.ledger(claim)) == 0
    card = board.card("C-9")
    stop = claims.stopped("CL-303")
    assert card.column == "Needs you" and claims.filed("CL-303") is None
    assert stop["read"] < len(claims.claim("CL-303")["documents"])
    assert claims.spent("CL-303") <= claims.budget
    assert f"{stop['read']} of 8 read" in card.log[-1]
    assert f"raise the budget? {ADJUSTER} decides" in card.log[-1]
    assert [card.column for card in board.cards[:2]] == ["Done", "Done"]


def test_cost_per_claim_falls_with_each_lever(tmp_path):
    costs = []
    for number, (_, levers) in enumerate(LADDER):
        board, _ = run(tmp_path / str(number), levers)
        costs.append(sum(card.cost for card in board.cards[:2]) / 2)
    assert costs == sorted(costs, reverse=True) and len(set(costs)) == len(costs)


def test_a_total_that_is_not_the_quotes_is_refused(tmp_path):
    wrong = {**FILES, "CL-301": {**FILES["CL-301"], "total": "£2,214.00"}}
    board, _ = run(tmp_path, LADDER[-1][1], wrong)
    claims = Claims.open(tmp_path)
    card = board.card("C-7")
    assert card.column == "Backlog" and claims.filed("CL-301") is None
    assert "not the £2,656.80 printed on quote-301.pdf" in card.log[-1]


def test_a_total_on_a_claim_with_no_quote_is_refused(tmp_path):
    invented = {**FILES, "CL-303": {**FILES["CL-303"], "total": "£40,000.00"}}
    # This reader prints a total on every read, quote or not.
    board, _ = run(tmp_path, LADDER[-1][1], invented, invent=True)
    claims = Claims.open(tmp_path)
    assert board.card("C-9").column == "Backlog" and claims.filed("CL-303") is None
    assert "has no quote behind it" in claims.refused("CL-303")


def test_price_bills_cached_input_at_the_cached_rate():
    usage = Usage(
        requests=1,
        input_tokens=10_000,
        output_tokens=1_000,
        total_tokens=11_000,
        input_tokens_details=InputTokensDetails(cached_tokens=8_000, cache_write_tokens=0),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
    )
    assert price(usage) == pytest.approx((2_000 * 0.10 + 8_000 * 0.01 + 1_000 * 0.50) / 1e6)


@pytest.mark.lab("cut-ai-agent-costs-with-prompt-caching")
def test_only_the_adjuster_can_raise_a_claims_budget(tmp_path):
    board, _ = run(tmp_path, LADDER[0][1])
    claims = Claims.open(tmp_path)
    assert board.card("C-9").column == "Needs you"
    budget = claims.budget_for("CL-303")

    with pytest.raises(Refused, match="is not the adjuster"):
        claims.decide_budget("CL-303", HANDLER, approve=True)
    assert claims.budget_for("CL-303") == budget and claims.stopped("CL-303") is not None
    assert claims.decisions("CL-303")[-1]["decision"] == "refused"
    assert claims.decisions("CL-303")[-1]["by"] == HANDLER

    claims.decide_budget("CL-303", ADJUSTER, approve=True)
    assert claims.budget_for("CL-303") == claims.raised_budget > budget
    assert claims.budget_for("CL-301") == budget, "only the stopped claim's budget is raised"
    with pytest.raises(Refused, match="is not waiting for a budget"):
        claims.decide_budget("CL-303", ADJUSTER, approve=True)


@pytest.mark.lab("cut-ai-agent-costs-with-prompt-caching")
def test_after_the_raise_only_the_stopped_claim_resumes_from_the_document_it_stopped_at(tmp_path):
    board, model = run(tmp_path, LADDER[0][1])
    claims = Claims.open(tmp_path)
    stop = claims.stopped("CL-303")
    documents = claims.claim("CL-303")["documents"]
    calls_before = len(model.calls)
    before = [len(board.card(claim).runs) for claim in ("C-7", "C-8", "C-9")]

    claims.decide_budget("CL-303", ADJUSTER, approve=True)
    asyncio.run(resume_after_raise(board, board.card("C-9"), LADDER[0][1], model))

    assert len(model.calls) - calls_before == len(documents) - stop["read"], "no document is read twice"
    first = model.calls[calls_before]
    assert len(images(first)) == stop["read"] + 1, "the thread so far, plus the document it stopped at"
    rows = claims.ledger("CL-303")
    assert [row["doc"] for row in rows] == documents
    assert [len(board.card(claim).runs) for claim in ("C-7", "C-8")] == before[:2]
    hail = board.card("C-9")
    assert hail.column == "Done" and "an assessor prices it" in hail.log[-1]
    assert hail.runs[-1].cost == pytest.approx(sum(row["cost"] for row in rows[stop["read"] :]))


def test_a_declined_budget_sends_the_claim_to_backlog_unfiled(tmp_path):
    board, _ = run(tmp_path, LADDER[0][1])
    claims = Claims.open(tmp_path)
    stop = claims.stopped("CL-303")

    claims.decide_budget("CL-303", ADJUSTER, approve=False)
    settle(board, board.card("C-9"), claims)

    hail = board.card("C-9")
    assert hail.column == "Backlog" and claims.filed("CL-303") is None
    assert f"{ADJUSTER} kept the budget" in hail.log[-1] and f"{stop['read']} of 8 read" in hail.log[-1]
    assert claims.declined("CL-303") == ADJUSTER


async def sign_in(client, name, passphrase):
    login = await client.post("/login", data={"name": name, "passphrase": passphrase}, allow_redirects=False)
    assert login.status == 303


async def raise_budget(client, decision="approve"):
    page = await (await client.get("/pass/1/cards/C-9")).text()
    action = re.search(r'<form method="post" action="([^"]+)" class="decide">', page)
    assert action, "the card page offers the budget form"
    sent = await client.post(action.group(1), data={"decision": decision}, allow_redirects=False)
    assert sent.status == 303
    return sent


def live_desk(tmp_path, levers=LADDER[0][1]) -> tuple[Desk, ScriptedModel]:
    desk = Desk(tmp_path / "stored", tmp_path / "live")
    _, model = run(desk.live / "pass-1", levers)
    return desk, model


def test_the_board_needs_a_signed_in_adjuster_to_raise_the_budget(tmp_path, people):
    desk, model = live_desk(tmp_path)

    async def scenario():
        async with (
            TestClient(TestServer(app(desk, TEAM, people, model))) as leo,
            TestClient(TestServer(app(desk, TEAM, people, model))) as nina,
        ):
            signed_out = await leo.post("/pass/1/cards/C-9/budget", data={"decision": "approve"})
            assert signed_out.history[0].headers["Location"] == "/login" or signed_out.status in (200, 401)
            wrong = await leo.post("/login", data={"name": ADJUSTER, "passphrase": "guess"})
            assert wrong.status == 401

            await sign_in(leo, HANDLER, "leo-passphrase")
            await raise_budget(leo)
            claims = Claims.open(desk.folder(1))
            assert Board.open(desk.folder(1) / "board.json").card("C-9").column == "Needs you"
            assert claims.decisions("CL-303")[-1]["by"] == HANDLER, "the person comes from the cookie"
            page = await (await leo.get("/pass/1/cards/C-9")).text()
            assert "Raise the budget?" in page and "is not the adjuster" in page

            await sign_in(nina, ADJUSTER, "nina-passphrase")
            await raise_budget(nina)
            await asyncio.gather(*nina.app[TASKS])

    asyncio.run(scenario())
    card = Board.open(desk.folder(1) / "board.json").card("C-9")
    assert card.column == "Done"
    assert Claims.open(desk.folder(1)).decisions("CL-303")[-1]["by"] == ADJUSTER


def test_the_board_says_kaas_insurance_and_never_the_model(tmp_path, people):
    desk = Desk(tmp_path / "stored", tmp_path / "live")
    for number, (_, levers) in enumerate(LADDER, 1):
        run(desk.stored / f"pass-{number}", levers)
    ladder = passes(desk.stored)
    filed = [result.filed for result in ladder]
    assert filed[0] == 2 and filed[-1] == 3 and filed == sorted(filed)
    first = ladder[0]
    rows = [
        row for card in first.board.cards for row in Claims.open(desk.stored / "pass-1").ledger(card.issue)
    ]
    assert first.fresh + first.cached == sum(row["input"] for row in rows)

    async def scenario():
        async with TestClient(TestServer(app(desk, TEAM, people))) as nina:
            await sign_in(nina, ADJUSTER, "nina-passphrase")
            front = await (await nina.get("/")).text()
            stopped = await (await nina.get("/pass/1/cards/C-9")).text()
            return front, stopped

    front, stopped = asyncio.run(scenario())
    assert "KaaS Insurance" in front and "Acme" not in front
    assert all(f'href="/pass/{number}"' in front for number in range(1, 5)) and 'href="/cards/' not in front
    for name in ("gpt", "claude", "nemotron", "gemini"):
        assert name not in front.lower() and name not in stopped.lower()
    assert "Not read: one more read would cross the $0.0030 budget" in stopped
    assert stopped.count("<tr><td>hail-") == Claims.open(desk.stored / "pass-1").stopped("CL-303")["read"]
    assert "Raise the budget?" not in stopped, "a stored pass is history, so it offers no decision"
    assert ladder[-1].per_thousand in front and len(ladder[-1].per_thousand.split(".")[1]) == 2


def test_a_resumed_threads_earlier_answers_are_messages_the_provider_accepts():
    """The chat-completions converter drops an assistant message with no id, which failed a live resume."""
    from agents.models.chatcmpl_converter import Converter

    item = assistant_item({"parts": ["bonnet"], "vehicles": 1, "total": None, "note": "dent"})
    assert Converter.maybe_response_output_message(item) is not None
