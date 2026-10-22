"""The judge: a second model that reads an answer against the sections it cites, for what code cannot read.

Code can see that an answer cites §4.2 and that its numbers are printed there. It cannot see an answer
that cites the right section and still answers a different question, or adds advice the label never
gives. The judge can, but its verdict is a model's opinion, so it is graded first: it reads answers Dr.
Okafor graded by hand, and counts on the test set only when it agrees with her often enough.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass

from agents import Agent, AgentsException, Model, RunConfig, Runner
from openai import APIError
from pydantic import BaseModel

from evals.desk import FIXTURES, SECTIONS, openrouter
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


def judge_model() -> Model | str:
    return openrouter(os.environ.get("JUDGE_MODEL", "openai/gpt-6-luna"))


def judge(model: Model | str) -> Agent:
    return Agent(name="Judge", instructions=RUBRIC, model=model, output_type=Verdict)


def ask(question: str, sections: list[str], answer: str) -> str:
    cited = "\n".join(f"§{s}: {SECTIONS[s]['text']}" for s in sections if s in SECTIONS) or "(none)"
    return f"Question: {question}\n\nCited sections:\n{cited}\n\nAnswer: {answer}"


async def verdict(model: Model | str, question: str, sections: list[str], answer: str) -> Verdict | None:
    for _ in range(2):  # one retry: a judge call that fails is no evidence about the answer
        try:
            result = await Runner.run(
                judge(model),
                ask(question, sections, answer),
                max_turns=1,
                run_config=RunConfig(workflow_name="Eval judge"),
            )
            return result.final_output_as(Verdict)
        except (AgentsException, APIError):
            continue
    return None


async def calibrate(model: Model | str) -> Calibration:
    agreed = 0
    for g in GRADED:
        v = await verdict(model, g["question"], g["sections"], g["answer"])
        agreed += v is not None and v.good == g["good"]
    return Calibration(agreed, len(GRADED))


async def grade(model: Model | str, scored: list[Scored], cal: Calibration) -> None:
    """Add the judge's check to every answer the code checks passed; it fails a case only when trusted."""
    for s in scored:
        if not s.passed or s.record.route != "answer":
            continue
        v = await verdict(model, s.case.inquiry["question"], s.record.sections, s.record.text)
        said = "no verdict" if v is None else v.reason
        ok = v is not None and v.good
        if not cal.trusted:
            said, ok = f"not counted, agrees with the hand grades {cal.agreed} of {cal.total}: {said}", True
        s.checks.append(Check("judge", ok, said))
