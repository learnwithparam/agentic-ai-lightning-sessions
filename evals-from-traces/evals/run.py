"""The two commands of the session: a week of production runs, then the evals that gate a swap.

`make runs` sends every inquiry to the production desk, traced. `make demo` builds the test set from
those traces, grades the judge, scores production, reruns the test set on the candidate, and asks the
gate whether the swap ships. The report it writes is what `make board` serves.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from agents import set_tracing_disabled

from evals.desk import INQUIRIES, STATE, VARIANTS, Record, handle, model_for, write_log
from evals.judge import Calibration, calibrate, grade, judge_model
from evals.score import SAID, Scored, Swap, gate, score
from evals.testset import build, langfuse
from workforce.mission import paint
from workforce.traces import install


def traced() -> None:
    """Trace to Langfuse when it is up; a room without it runs on the local log alone."""
    if not (langfuse() and install()):
        set_tracing_disabled(True)


# A terminal the room can read from the back holds about 76 columns.
WIDTH = 76


async def runs(state: Path = STATE, variant: str = "production") -> list[Record]:
    v = VARIANTS[variant]
    model, out = model_for(v), []
    for q in INQUIRIES:
        rec = await handle(q, v, model)
        out.append(rec)
        line = f"  {q['id']}  {SAID[rec.route]:<15} "
        print(line + fit(rec.text, WIDTH - len(line)))
    write_log(state, variant, out)
    return out


def fit(text: str, width: int) -> str:
    """One line of a terminal the room can read: the full report keeps every word."""
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def show(name: str, scored: list[Scored]) -> None:
    passed = sum(s.passed for s in scored)
    for s in scored:
        for c in s.failed:
            tone = "1;31" if c.critical else "1;33"
            flag = "  [critical]" if c.critical else ""
            line = f"  {s.case.id}  {c.name}: "
            print(paint(line + fit(c.said, WIDTH - len(line) - len(flag)) + flag, tone))
    print(paint(f"{name}: {passed} of {len(scored)} pass", "1"))


def dump(source: str, cal: Calibration, results: dict[str, list[Scored]], swap: Swap, state: Path) -> None:
    report = {
        "source": source,
        "calibration": asdict(cal),
        "runs": {
            name: [
                {"id": s.case.id, "record": asdict(s.record), "checks": [asdict(c) for c in s.checks]}
                for s in scored
            ]
            for name, scored in results.items()
        },
        "swap": asdict(swap),
    }
    state.mkdir(parents=True, exist_ok=True)
    (state / "report.json").write_text(json.dumps(report, indent=1))


async def demo(state: Path = STATE) -> Swap:
    cases, source = build(state)
    print(paint(f"Test set: {len(cases)} production runs from {source}, each joined to its label", "1;36"))
    jm = judge_model()
    cal = await calibrate(jm)
    trust = "counted" if cal.trusted else "shown, not counted"
    print(paint(f"Judge agrees with Dr. Okafor's grades on {cal.agreed} of {cal.total}: {trust}", "1;36"))

    production = [score(c) for c in cases]
    await grade(jm, production, cal)
    show("Production", production)

    print(paint("Candidate: the same inquiries, the shorter prompt and another model", "1;36"))
    v = VARIANTS["candidate"]
    model = model_for(v)
    records = [await handle(c.inquiry, v, model) for c in cases]
    write_log(state, "candidate", records)
    candidate = [score(c, r) for c, r in zip(cases, records, strict=True)]
    await grade(jm, candidate, cal)
    show("Candidate", candidate)

    swap = gate(production, candidate)
    if swap.ok:
        print(paint("Swap approved: no missed side effect, and no fewer passes", "1;32"))
    else:
        print(paint("Swap refused:", "1;31"))
        for reason in swap.reasons:
            print(paint(f"  {reason}", "1;31"))
    dump(source, cal, {"production": production, "candidate": candidate}, swap, state)
    return swap


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["runs", "demo", "serve"])
    p.add_argument("--port", type=int, default=8094)
    a = p.parse_args()
    if a.command == "serve":
        from evals.board import serve

        serve(STATE, a.port)
        return
    traced()
    if a.command == "runs":
        print(paint("A week of inquiries to the production desk", "1;36"))
        asyncio.run(runs())
    else:
        asyncio.run(demo())


if __name__ == "__main__":
    main()
