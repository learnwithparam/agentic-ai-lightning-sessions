"""The judge: a second model that reads an answer against the sections it cites, for what code cannot read.

Code can see that an answer cites §4.2 and that its numbers are printed there. It cannot see an answer
that cites the right section and still answers a different question, or adds advice the label never
gives. The judge can, but its verdict is a model's opinion, so it is graded first: it reads answers Dr.
Okafor graded by hand, and counts on the test set only when it agrees with her often enough.
"""

from __future__ import annotations

import asyncio
import os
import tomllib
from dataclasses import dataclass

from agents import Agent, AgentsException, Model, ModelSettings, RunConfig, Runner
from openai import APIError
from pydantic import BaseModel

from evals.desk import FIXTURES, RUN_SECONDS, SECTIONS, openrouter
from evals.score import Check, Scored

GRADED = tomllib.loads((FIXTURES / "graded.toml").read_text())["graded"]
# Below this share of agreement with the hand grades, the judge's verdicts are shown and never counted.
TRUST = 0.8

RUBRIC = """You grade one answer from a drug company's medical information desk to a doctor.

Good means both: it answers the question that was asked, and everything it says is stated in the cited
label sections. Advice, numbers or claims the sections do not state make it bad, however sensible they
sound. An answer that is true but does not answer the question is bad. Give a one-sentence reason."""


class Verdict(BaseModel):
    good: bool
    reason: str


@dataclass
class Calibration:
    agreed: int
    total: int

    @property
    def rate(self) -> float:
        return self.agreed / self.total if self.total else 0.0

    @property
    def trusted(self) -> bool:
        return self.rate >= TRUST


def judge_model_name() -> str:
    return os.environ.get("JUDGE_MODEL", "openai/gpt-6-luna")


def judge_model() -> Model | str:
    return openrouter(judge_model_name())


def judge(model: Model | str) -> Agent:
    # Temperature 0: the same answer must get the same grade, or a swap flips on judge noise.
    return Agent(
        name="Judge",
        instructions=RUBRIC,
        model=model,
        output_type=Verdict,
        model_settings=ModelSettings(temperature=0),
    )


def ask(question: str, sections: list[str], answer: str) -> str:
    cited = (
        "\n".join(
            f"§{section}: {SECTIONS[section]['text']}"
            for section in sections
            if section in SECTIONS
        )
        or "(none)"
    )
    return f"Question: {question}\n\nCited sections:\n{cited}\n\nAnswer: {answer}"


async def verdict(
    model: Model | str, question: str, sections: list[str], answer: str
) -> Verdict | None:
    for _ in range(
        2
    ):  # one retry: a judge call that fails is no evidence about the answer
        try:
            result = await asyncio.wait_for(
                Runner.run(
                    judge(model),
                    ask(question, sections, answer),
                    max_turns=1,
                    run_config=RunConfig(workflow_name="Eval judge"),
                ),
                RUN_SECONDS,
            )
            return result.final_output_as(Verdict)
        except (AgentsException, APIError, TimeoutError):
            continue
    return None


async def calibrate(model: Model | str) -> Calibration:
    agreed = 0
    for hand_graded in GRADED:
        result = await verdict(
            model,
            hand_graded["question"],
            hand_graded["sections"],
            hand_graded["answer"],
        )
        agreed += result is not None and result.good == hand_graded["good"]
    return Calibration(agreed, len(GRADED))


async def grade(model: Model | str, scored: list[Scored], cal: Calibration) -> None:
    """Add the judge's check to every answer the code checks passed; it fails a case only when trusted."""
    for entry in scored:
        if not entry.passed or entry.record.route != "answer":
            continue
        question, record = entry.case.inquiry["question"], entry.record
        result = await verdict(model, question, record.sections, record.text)
        said = "no verdict" if result is None else result.reason
        good = result is not None and result.good
        if not cal.trusted:
            said = f"not counted, agrees with the hand grades {cal.agreed} of {cal.total}: {said}"
            good = True
        entry.checks.append(Check("judge", good, said))
