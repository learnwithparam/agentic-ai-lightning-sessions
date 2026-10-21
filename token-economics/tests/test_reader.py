"""The reader's promises, against the real claims system and ledger with a scripted model.

The scripted model bills each call the way a provider does: the instructions and text by length, a photo
by its detail, and the instructions as cached input when they match an earlier call word for word.
"""

import asyncio
import json
from dataclasses import replace

import pytest
from agents import Usage
from agents.testing import ModelStep, ScriptedModel, assistant_message
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

from claims.reader import LADDER, SEED, Levers, work
from claims.system import Claims
from workforce.board import Board, price

PHOTO = {"high": 1000, "low": 300}
# Reasoning tokens a read spends at the model's default effort and at low effort, billed as output.
THINK = {None: 1100, "low": 250}
FILES = {
    "CL-301": {"parts": ["front bumper", "bonnet", "left headlamp"], "vehicles": 1, "total": "£2,656.80"},
    "CL-302": {"parts": ["rear bumper", "boot lid"], "vehicles": 1, "total": "£1,738.80"},
    "CL-303": {"parts": ["roof panel", "windscreen"], "vehicles": 11, "total": None},
}


@pytest.fixture(autouse=True)
def prices(monkeypatch):
    monkeypatch.setenv("WORKFORCE_PRICE_IN", "0.10")
    monkeypatch.setenv("WORKFORCE_PRICE_CACHED", "0.01")
    monkeypatch.setenv("WORKFORCE_PRICE_OUT", "0.50")


def parts(item) -> list[dict]:
    content = item.get("content") if isinstance(item, dict) else None
    return content if isinstance(content, list) else []


def images(call) -> list[dict]:
    return [p for item in call.input for p in parts(item) if p.get("type") == "input_image"]


def text(call) -> str:
    return " ".join(p.get("text", "") for item in call.input for p in parts(item))


def tokens(item) -> int:
    return sum(
        PHOTO[p["detail"]] if p.get("type") == "input_image" else len(p.get("text", "")) // 4
        for p in parts(item)
    )


def provider(files: dict, invent: bool = False) -> ScriptedModel:
    seen: set[tuple[str, ...]] = set()

    def bill(call) -> ModelStep:
        system = call.system_instructions or ""
        cid = next(c for c in files if c in text(call))
        quoted = any("Repair quote" in p.get("text", "") for p in parts(call.input[-1]))
        out = {**files[cid], "note": "Damage matches what the policyholder described."}
        out["total"] = out["total"] if quoted or invent else None
        # Like the provider's cache: the longest start of this call that an earlier call sent word for word.
        chunks = [(system, len(system) // 4)] + [
            (json.dumps(i, sort_keys=True), tokens(i)) for i in call.input
        ]
        keys = [tuple(k for k, _ in chunks[: n + 1]) for n in range(len(chunks))]
        cached = sum(t for (_, t), key in zip(chunks, keys, strict=True) if key in seen)
        seen.update(keys)
        reasoning = THINK[call.model_settings.reasoning.effort if call.model_settings.reasoning else None]
        body = json.dumps(out)
        usage = Usage(
            requests=1,
            input_tokens=sum(t for _, t in chunks),
            output_tokens=len(body) // 4 + reasoning,
            total_tokens=sum(t for _, t in chunks) + len(body) // 4 + reasoning,
            input_tokens_details=InputTokensDetails(cached_tokens=cached, cache_write_tokens=0),
            output_tokens_details=OutputTokensDetails(reasoning_tokens=reasoning),
        )
        return ModelStep(output=[assistant_message(body)], usage=usage)

    return ScriptedModel([ModelStep.respond(bill) for _ in range(40)])


def run(
    tmp_path, levers: Levers, files: dict = FILES, invent: bool = False
) -> tuple[Board, Claims, ScriptedModel]:
    model = provider(files, invent)
    board = Board(tmp_path / "board.json", [replace(c, runs=[], log=[]) for c in SEED])
    board.save()
    for card in board.cards:
        asyncio.run(work(board, card, levers, model))
    return board, Claims.open(tmp_path), model


@pytest.mark.lab("cut-ai-agent-costs-with-prompt-caching")
def test_all_three_levers_file_every_claim_at_the_ledgers_cost(tmp_path):
    board, claims, model = run(tmp_path, LADDER[-1][1])

    assert len({c.system_instructions for c in model.calls}) == 1, "every call starts with the same policy"
    assert all(c.model_settings.reasoning.effort == "low" for c in model.calls), "every read thinks less"
    assert {i["detail"] for c in model.calls for i in images(c)} == {"low"}
    assert [c.column for c in board.cards] == ["Done", "Done", "Done"]
    assert claims.filed("CL-301")["total"] == "£2,656.80"
    hail = board.card("C-9")
    assert claims.filed("CL-303")["total"] is None and "an assessor prices it" in hail.log[-1]
    for card in board.cards:
        rows = claims.ledger(card.issue)
        assert len(rows) == len(claims.claim(card.issue)["documents"])
        assert card.cost == pytest.approx(sum(r["cost"] for r in rows))
    later = claims.ledger("CL-303")[1]
    assert later["cached"] > 0
    assert later["cost"] == pytest.approx(
        ((later["input"] - later["cached"]) * 0.10 + later["cached"] * 0.01 + later["output"] * 0.50) / 1e6
    )


def test_without_levers_the_thread_grows_and_the_hail_claim_stops_at_its_budget(tmp_path):
    board, claims, model = run(tmp_path, LADDER[0][1])

    assert len({c.system_instructions for c in model.calls}) == len(model.calls), "a timestamp on top"
    hail = [c for c in model.calls if "CL-303" in text(c)]
    assert [len(images(c)) for c in hail] == list(range(1, len(hail) + 1)), "every earlier photo again"
    assert {i["detail"] for c in model.calls for i in images(c)} == {"high"}
    assert all(c.model_settings.reasoning is None for c in model.calls)
    assert sum(r["cached"] for c in ("CL-301", "CL-302", "CL-303") for r in claims.ledger(c)) == 0
    card = board.card("C-9")
    stop = claims.stopped("CL-303")
    assert card.column == "Needs you" and claims.filed("CL-303") is None
    assert stop["read"] < len(claims.claim("CL-303")["documents"])
    assert claims.spent("CL-303") <= claims.budget
    assert f"{stop['read']} of 8 read" in card.log[-1] and claims.adjuster in card.log[-1]
    assert [c.column for c in board.cards[:2]] == ["Done", "Done"]


def test_cost_per_claim_falls_with_each_lever(tmp_path):
    costs = []
    for n, (_, levers) in enumerate(LADDER):
        board, _, _ = run(tmp_path / str(n), levers)
        costs.append(sum(c.cost for c in board.cards[:2]) / 2)
    assert costs == sorted(costs, reverse=True) and len(set(costs)) == len(costs)


def test_a_total_that_is_not_the_quotes_is_refused(tmp_path):
    wrong = {**FILES, "CL-301": {**FILES["CL-301"], "total": "£2,214.00"}}
    board, claims, _ = run(tmp_path, LADDER[-1][1], wrong)
    card = board.card("C-7")
    assert card.column == "Backlog" and claims.filed("CL-301") is None
    assert "not the £2,656.80 printed on quote-301.pdf" in card.log[-1]


def test_a_total_on_a_claim_with_no_quote_is_refused(tmp_path):
    invented = {**FILES, "CL-303": {**FILES["CL-303"], "total": "£40,000.00"}}
    # This reader prints a total on every read, quote or not.
    board, claims, _ = run(tmp_path, LADDER[-1][1], invented, invent=True)
    assert board.card("C-9").column == "Backlog" and claims.filed("CL-303") is None
    assert "has no quote behind it" in claims.refused("CL-303")


def test_price_bills_cached_input_at_the_cached_rate():
    u = Usage(
        requests=1,
        input_tokens=10_000,
        output_tokens=1_000,
        total_tokens=11_000,
        input_tokens_details=InputTokensDetails(cached_tokens=8_000, cache_write_tokens=0),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
    )
    assert price(u) == pytest.approx((2_000 * 0.10 + 8_000 * 0.01 + 1_000 * 0.50) / 1e6)


def test_the_board_shows_each_pass_and_the_ledger_of_a_stopped_claim(tmp_path):
    from claims.board import board_page, card_page
    from claims.reader import passes

    for n, (_, levers) in enumerate(LADDER, 1):
        run(tmp_path / f"pass-{n}", levers)
    ladder = passes(tmp_path)
    filed = [p.filed for p in ladder]
    assert filed[0] == 2 and filed[-1] == 3 and filed == sorted(filed)
    first = ladder[0]
    rows = [t for c in first.board.cards for t in Claims.open(tmp_path / "pass-1").ledger(c.issue)]
    assert first.fresh + first.cached == sum(t["input"] for t in rows)
    page = board_page(tmp_path, 4)
    assert all(f'href="/pass/{n}"' in page for n in range(1, 5)) and 'href="/cards/' not in page
    stopped = card_page(tmp_path, 1, "C-9")
    assert "Not read: one more read would cross the $0.0030 budget" in stopped
    assert stopped.count("<tr><td>hail-") == Claims.open(tmp_path / "pass-1").stopped("CL-303")["read"]
