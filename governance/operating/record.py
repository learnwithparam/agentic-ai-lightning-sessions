"""The decision record: one line per verdict, appended and never rewritten.

Each row says which card, which employee, what it asked for, what was decided, by which rule, and who
decided it: the policy for what an employee may do alone, a named person for the rest. A file opened
only to append is the whole mechanism; a test fails if anything here rewrites a line.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

# allowed: the policy let the employee do it alone. asked: waiting for the named signer. signed and
# declined: that person's answer. refused: a rule said no. not started: paused, or over its budget.
VERDICTS = ("allowed", "asked", "signed", "declined", "refused", "not started")


class Record:
    def __init__(self, path: Path) -> None:
        self.path = path

    def add(
        self,
        card: str,
        agent: str,
        action: str,
        verdict: str,
        rule: str,
        decider: str,
        signer: str = "",
    ) -> None:
        assert verdict in VERDICTS, verdict
        row = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "card": card,
            "agent": agent,
        }
        row |= {
            "action": action,
            "verdict": verdict,
            "rule": rule,
            "by": decider,
            "signer": signer,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as log:
            log.write(json.dumps(row) + "\n")

    def rows(self) -> list[dict]:
        return (
            [json.loads(line) for line in self.path.read_text().splitlines()]
            if self.path.exists()
            else []
        )

    def for_card(self, card: str) -> list[dict]:
        return [row for row in self.rows() if row["card"] == card]
