"""KaaS Retail's hiring system, the record the board settles on: roles, CVs, shortlists, audit log.

Its rules are code, so no CV or prompt can move them. A candidate's date of birth, nationality and home
address are never handed to the screener. A shortlist must be the size the role asks for, from people
who applied, and holds nobody whose CV is flagged and not yet cleared by the recruiter. Only the
recruiter reviews a flagged CV, and a refused review is in the audit log.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from workforce.board import is_blank

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
PRIVATE = ("date_of_birth", "nationality", "home_address")


class Refused(Exception):
    pass


@dataclass
class ATS:
    state: Path

    @classmethod
    def open(cls, state: Path) -> ATS:
        database = state / "ats.json"
        if is_blank(database):
            state.mkdir(parents=True, exist_ok=True)
            shutil.copy(FIXTURES / "ats.json", database)
        return cls(state)

    def _read(self) -> dict:
        return json.loads((self.state / "ats.json").read_text())

    def _write(self, data: dict) -> None:
        (self.state / "ats.json").write_text(json.dumps(data, indent=1, ensure_ascii=False))

    @property
    def recruiter(self) -> str:
        return self._read()["recruiter"]

    def role(self, requisition: str) -> dict:
        return self._read()["roles"][requisition]

    def candidate(self, candidate_id: str) -> dict:
        record = self._read()["candidates"][candidate_id]
        return {"id": candidate_id, "name": record["name"]}

    def cv_html(self, candidate_id: str) -> str:
        cv_file = self._read()["candidates"][candidate_id]["cv"]
        return (FIXTURES / "cvs" / cv_file).read_text()

    def field(self, card_id: str, candidate_id: str, name: str, asked_by: str) -> str:
        record = self._read()["candidates"][candidate_id]
        if name in PRIVATE:
            what = f"refused {asked_by}: {name.replace('_', ' ')}"
            self.audit(card_id, "private fields", candidate_id, what)
            raise Refused(f"{name} is private: screening may not use it")
        if name != "name":
            raise Refused(f"no field called {name}")
        return record["name"]

    def audit(self, card_id: str, check: str, candidate_id: str, what: str, found: tuple = ()) -> None:
        data = self._read()
        row = {"card": card_id, "check": check, "candidate": candidate_id, "what": what}
        if found:
            row["found"] = list(found)
        data.setdefault("audit", []).append(row)
        self._write(data)

    def log(self, card_id: str) -> list[dict]:
        return [row for row in self._read().get("audit", []) if row["card"] == card_id]

    def flag(self, card_id: str, candidate_id: str, hidden: list[str]) -> None:
        data = self._read()
        if candidate_id in data.get("flags", {}).get(card_id, {}):
            return
        flags = data.setdefault("flags", {}).setdefault(card_id, {})
        flags[candidate_id] = {"hidden": hidden, "reviewed_by": None}
        self._write(data)
        what = "withheld from the screener: text a person cannot see"
        self.audit(card_id, "hidden text", candidate_id, what)

    def flags(self, card_id: str) -> dict[str, dict]:
        return self._read().get("flags", {}).get(card_id, {})

    def review(self, card_id: str, candidate_id: str, reviewer: str, reject: bool) -> None:
        """The recruiter's call on a flagged CV. Anyone else is refused, and the refusal is logged."""
        data = self._read()
        if reviewer != data["recruiter"]:
            what = f"{reviewer} may not review this CV: only the recruiter reviews a flagged CV"
            self.audit(card_id, "review refused", candidate_id, what)
            raise Refused(what)
        flag = data.get("flags", {}).get(card_id, {}).get(candidate_id)
        if flag is None:
            raise Refused(f"{candidate_id} is not flagged on {card_id}")
        flag.update(reviewed_by=reviewer, rejected=reject)
        self._write(data)
        verdict = "rejected the application" if reject else "cleared the CV"
        self.audit(card_id, "recruiter", candidate_id, f"{reviewer} {verdict}")

    @staticmethod
    def is_held(flag: dict) -> bool:
        return not flag["reviewed_by"] or flag["rejected"]

    def submit(self, card_id: str, requisition: str, candidate_ids: list[str], submitted_by: str) -> dict:
        data = self._read()
        role = data["roles"][requisition]
        flags = data.get("flags", {}).get(card_id, {})
        size = role["shortlist"]
        if len(set(candidate_ids)) != size or len(candidate_ids) != size:
            raise Refused(f"{role['title']} needs a shortlist of {size} different people")
        strangers = [item for item in candidate_ids if item not in role["applicants"]]
        if strangers:
            raise Refused(f"{', '.join(strangers)} did not apply to {requisition}")
        held = [item for item in candidate_ids if item in flags and self.is_held(flags[item])]
        if held:
            raise Refused(f"{', '.join(held)} is flagged for a recruiter and cannot be shortlisted")
        shortlist = {
            "req": requisition,
            "candidates": candidate_ids,
            "by": submitted_by,
            "manager": role["manager"],
        }
        data.setdefault("shortlists", {})[card_id] = shortlist
        self._write(data)
        return shortlist

    def shortlist(self, card_id: str) -> dict | None:
        return self._read().get("shortlists", {}).get(card_id)
