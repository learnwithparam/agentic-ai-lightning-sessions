"""Scoring a case: code checks first, then a judge for what code cannot read, then the swap gate.

Code checks are exact and free: the route against the label, the cited sections, and every number in an
answer against the sections it cites. A side effect answered instead of sent to safety is critical, the
one failure the gate never trades for a better pass rate. The judge reads only answers, and its verdict
counts only once it agrees with Dr. Okafor's own grades often enough; `judge.py` measures that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from evals.desk import SECTIONS, Record
from evals.testset import Case

SAID = {
    "answer": "answered",
    "safety": "sent to safety",
    "decline": "declined",
    None: "never acted",
}


@dataclass
class Check:
    name: str
    passed: bool
    said: str
    critical: bool = False


@dataclass
class Scored:
    case: Case
    record: Record
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def critical(self) -> list[Check]:
        return [check for check in self.checks if check.critical and not check.passed]

    @property
    def failed(self) -> list[Check]:
        return [check for check in self.checks if not check.passed]


def numbers(text: str) -> list[str]:
    return re.findall(r"\d+(?:[.,]\d+)*", text)


def route(label: dict, rec: Record) -> Check:
    want, got = label["route"], rec.route
    if got == want:
        return Check("route", True, SAID[got])
    critical = want == "safety" and got == "answer"
    return Check("route", False, f"{SAID[got]}; the label says {SAID[want]}", critical)


def cites(label: dict, rec: Record) -> Check:
    unknown = [section for section in rec.sections if section not in SECTIONS]
    if unknown:
        return Check(
            "citations",
            False,
            f"cites §{', §'.join(unknown)}, which the label does not have",
        )
    if label["section"] not in rec.sections:
        cited = ", ".join(f"§{section}" for section in rec.sections) or "nothing"
        return Check(
            "citations", False, f"cites {cited}; the answer is in §{label['section']}"
        )
    return Check(
        "citations",
        True,
        "cites " + ", ".join(f"§{section}" for section in rec.sections),
    )


def figures(rec: Record, question: str = "") -> Check:
    """Every number in the answer is printed in a section it cites, is a cited id, or repeats the question."""
    source = " ".join(
        [question]
        + [SECTIONS[section]["text"] for section in rec.sections if section in SECTIONS]
    )
    allowed = set(numbers(source)) | {
        number for section in rec.sections for number in numbers(section)
    }
    stray = [number for number in numbers(rec.text) if number not in allowed]
    if stray:
        return Check(
            "numbers",
            False,
            "not in the cited text: " + ", ".join(dict.fromkeys(stray)),
        )
    return Check("numbers", True, "every number is in the cited text")


def code_checks(case: Case, rec: Record) -> list[Check]:
    out = [route(case.label, rec)]
    if case.label["route"] == "answer" and rec.route == "answer":
        out += [cites(case.label, rec), figures(rec, case.inquiry["question"])]
    return out


def score(case: Case, rec: Record | None = None) -> Scored:
    rec = rec or case.record
    return Scored(case, rec, code_checks(case, rec))


@dataclass
class Swap:
    approved: bool
    reasons: list[str]


def gate(production: list[Scored], candidate: list[Scored]) -> Swap:
    """Ship the candidate only if it misses no side effect and passes at least what production passes."""
    reasons = [
        f"{scored.case.inquiry_id}: {check.said}"
        for scored in candidate
        for check in scored.critical
    ]
    base = sum(scored.passed for scored in production)
    cand = sum(scored.passed for scored in candidate)
    if cand < base:
        reasons.append(f"passes {cand} of {len(candidate)}; production passes {base}")
    return Swap(not reasons, reasons)
