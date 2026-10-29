"""The signed model swap: the one write that changes what production runs.

A swap the gate refused cannot be signed by anyone. A swap that passed is signed only by the person who
owns the desk's answers, Dr. Okafor. Every attempt, refused or signed, is appended to decisions.jsonl.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

SIGNER = "Dr. Amara Okafor"


class Refused(Exception):
    """The swap was not signed; the message is what the board shows."""


def decisions_path(state: Path) -> Path:
    return state / "decisions.jsonl"


def read_decisions(state: Path) -> list[dict]:
    path = decisions_path(state)
    lines = path.read_text().splitlines() if path.exists() else []
    return [json.loads(line) for line in lines if line.strip()]


def note(
    state: Path, signed_by: str, decision: str, said: str, on_time: datetime
) -> None:
    state.mkdir(parents=True, exist_ok=True)
    row = {
        "by": signed_by,
        "decision": decision,
        "said": said,
        "at": on_time.strftime("%H:%M"),
    }
    with decisions_path(state).open("a") as log:
        log.write(json.dumps(row) + "\n")


def switch(state: Path, report: dict, signed_by: str, on_time: datetime) -> None:
    """Make the candidate production's model, if the gate passed it and its owner signs."""
    if not report["swap"]["approved"]:
        said = "the gate refused this candidate; nobody can sign it"
        note(state, signed_by, "refused", said, on_time)
        raise Refused(said)
    if signed_by != SIGNER:
        said = (
            f"{signed_by} may not sign a model swap; only {SIGNER} signs a model swap"
        )
        note(state, signed_by, "refused", said, on_time)
        raise Refused(said)
    candidate = report["candidate"]
    settings = {
        "model": candidate["model"],
        "prompt": candidate["prompt"],
        "signed_by": signed_by,
    }
    temporary = state / f"production.json.{os.getpid()}"
    temporary.write_text(json.dumps(settings, indent=1))
    os.replace(temporary, state / "production.json")
    note(state, signed_by, "signed", "production now runs the candidate", on_time)


def switched(state: Path) -> dict | None:
    path = state / "production.json"
    return (
        json.loads(path.read_text()) if path.exists() and path.stat().st_size else None
    )
