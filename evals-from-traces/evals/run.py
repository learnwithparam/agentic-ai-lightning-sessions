"""The commands of the session: a week of production runs, then the evals that gate a swap.

`make runs` sends every inquiry to the production desk, traced. `make demo` builds the test set from
those traces, grades the judge, scores production, reruns the test set on the candidate, and asks the
gate whether the swap ships. A swap that passes waits on the board for Dr. Okafor's signature. The
report it writes after each phase is what `make board` serves.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path

from agents import set_tracing_disabled

from evals.desk import (
    INQUIRIES,
    PROMPTS,
    STATE,
    Record,
    candidate_variant,
    handle,
    model_for,
    model_name,
    production_variant,
    write_log,
)
from evals.judge import (
    GRADED,
    RUBRIC,
    Calibration,
    calibrate,
    grade,
    judge_model,
    judge_model_name,
)
from evals.score import SAID, Check, Scored, Swap, gate, score
from evals.testset import Case, build, langfuse
from workforce.mission import paint
from workforce.traces import install

# A terminal the room can read from the back holds about 76 columns.
WIDTH = 76
# The candidate reruns twelve cases; four at a time keeps the live run short without flooding the API.
CONCURRENCY = 4


def traced() -> None:
    """Trace to Langfuse when it is up; a room without it runs on the local log alone."""
    if not (langfuse() and install()):
        set_tracing_disabled(True)


async def runs(state: Path = STATE, variant: str = "production") -> list[Record]:
    chosen = production_variant(state)
    model, records = model_for(chosen), []
    for inquiry in INQUIRIES:
        record = await handle(inquiry, chosen, model)
        records.append(record)
        line = f"  {inquiry['id']}  {SAID[record.route]:<15} "
        print(line + fit(record.text, WIDTH - len(line)))
    write_log(state, variant, records)
    return records


def fit(text: str, width: int) -> str:
    """One line of a terminal the room can read: the full report keeps every word."""
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def show(name: str, scored: list[Scored]) -> None:
    passed = sum(entry.passed for entry in scored)
    for entry in scored:
        for check in entry.failed:
            tone = "1;31" if check.critical else "1;33"
            flag = "  [critical]" if check.critical else ""
            line = f"  {entry.case.inquiry_id}  {check.name}: "
            print(
                paint(
                    line + fit(check.said, WIDTH - len(line) - len(flag)) + flag, tone
                )
            )
    print(paint(f"{name}: {passed} of {len(scored)} pass", "1"))


def rows_of(scored: list[Scored]) -> list[dict]:
    return [
        {
            "id": entry.case.inquiry_id,
            "record": asdict(entry.record),
            "checks": [asdict(check) for check in entry.checks],
        }
        for entry in scored
    ]


def scored_of(rows: list[dict], cases: list[Case]) -> list[Scored]:
    by_id = {case.inquiry_id: case for case in cases}
    return [
        Scored(
            by_id[row["id"]],
            Record(**row["record"]),
            [Check(**check) for check in row["checks"]],
        )
        for row in rows
    ]


def write_report(state: Path, report: dict) -> None:
    """Atomic, so the board polling the file never reads half of it."""
    state.mkdir(parents=True, exist_ok=True)
    temporary = state / f"report.json.{os.getpid()}"
    temporary.write_text(json.dumps(report, indent=1))
    os.replace(temporary, state / "report.json")


def cached(state: Path, phase: str, inputs: object) -> tuple[Path, object | None]:
    digest = hashlib.sha256(
        json.dumps(inputs, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
    path = state / "cache" / f"{phase}-{digest}.json"
    return path, json.loads(path.read_text()) if path.exists() else None


def remember(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


async def calibration_for(state: Path, judge_name: str, model) -> Calibration:
    path, hit = cached(state, "calibration", [judge_name, RUBRIC, GRADED])
    if hit:
        print(paint("Judge calibration: cached", "2"))
        return Calibration(**hit)
    result = await calibrate(model)
    remember(path, asdict(result))
    return result


async def production_for(
    state: Path, cases: list[Case], cal: Calibration, judge_name: str, model
) -> list[Scored]:
    chosen = production_variant(state)
    inputs = [
        chosen.prompt,
        model_name(chosen),
        judge_name,
        asdict(cal),
        [(case.inquiry_id, case.label, asdict(case.record)) for case in cases],
    ]
    path, hit = cached(state, "production", inputs)
    if hit:
        print(paint("Production: cached", "2"))
        return scored_of(hit, cases)
    scored = [score(case) for case in cases]
    await grade(model, scored, cal)
    remember(path, rows_of(scored))
    return scored


async def rerun(cases: list[Case], kind: str) -> list[Record]:
    variant = candidate_variant(kind)
    model, slots = model_for(variant), asyncio.Semaphore(CONCURRENCY)

    async def one(case: Case) -> Record:
        async with slots:
            return await handle(case.inquiry, variant, model)

    return list(await asyncio.gather(*(one(case) for case in cases)))


async def demo(state: Path = STATE, candidate: str = "default") -> Swap:
    cases, source = build(state)
    print(
        paint(
            f"Test set: {len(cases)} production runs from {source}, each joined to its label",
            "1;36",
        )
    )
    judge_name = judge_model_name()
    judge = judge_model()
    report = {
        "source": source,
        "status": "running",
        "runs": {},
        "swap": None,
        "calibration": None,
        "candidate": None,
    }
    write_report(state, report)

    cal = await calibration_for(state, judge_name, judge)
    trust = "counted" if cal.trusted else "shown, not counted"
    print(
        paint(
            f"Judge agrees with Dr. Okafor's grades on {cal.agreed} of {cal.total}: {trust}",
            "1;36",
        )
    )
    report["calibration"] = asdict(cal)
    write_report(state, report)

    production = await production_for(state, cases, cal, judge_name, judge)
    show("Production", production)
    report["runs"]["production"] = rows_of(production)
    write_report(state, report)

    variant = candidate_variant(candidate)
    note = (
        "the shorter prompt and another model"
        if candidate == "default"
        else "production's prompt and another model"
    )
    print(paint(f"Candidate: the same inquiries, {note}", "1;36"))
    records = await rerun(cases, candidate)
    write_log(state, "candidate", records)
    scored = [score(case, record) for case, record in zip(cases, records, strict=True)]
    await grade(judge, scored, cal)
    show("Candidate", scored)

    swap = gate(production, scored)
    prompt_key = next(key for key, text in PROMPTS.items() if text == variant.prompt)
    report["runs"]["candidate"] = rows_of(scored)
    report["candidate"] = {"model": model_name(variant), "prompt": prompt_key}
    report["swap"] = asdict(swap)
    report["status"] = "done"
    write_report(state, report)
    if swap.approved:
        print(
            paint(
                "Swap ready for sign-off: Dr. Amara Okafor signs it on the board",
                "1;32",
            )
        )
    else:
        print(paint("Swap refused:", "1;31"))
        for reason in swap.reasons:
            print(paint(f"  {reason}", "1;31"))
    return swap


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["runs", "demo", "serve"])
    parser.add_argument("--port", type=int, default=8094)
    parser.add_argument("--candidate", choices=["default", "safe"], default="default")
    arguments = parser.parse_args()
    if arguments.command == "serve":
        from evals.board import serve

        serve(STATE, arguments.port)
        return
    traced()
    if arguments.command == "runs":
        print(paint("A week of inquiries to the production desk", "1;36"))
        asyncio.run(runs())
    else:
        asyncio.run(demo(candidate=arguments.candidate))


if __name__ == "__main__":
    main()
