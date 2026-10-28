"""Acme's hiring system, the record the board settles on: roles, applicants, CVs, shortlists, audit log.

Its rules are code, so no CV or prompt can move them. A candidate's date of birth, nationality and home
address are never handed to the screener. A shortlist must be the size the role asks for, from people
who applied, and holds nobody whose CV is flagged and not yet cleared by a recruiter.
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
        db = state / "ats.json"
        if is_blank(db):
            state.mkdir(parents=True, exist_ok=True)
            shutil.copy(FIXTURES / "ats.json", db)
        return cls(state)

    def _read(self) -> dict:
        return json.loads((self.state / "ats.json").read_text())

    def _write(self, db: dict) -> None:
        (self.state / "ats.json").write_text(json.dumps(db, indent=1, ensure_ascii=False))

    @property
    def recruiter(self) -> str:
        return self._read()["recruiter"]

    def role(self, req: str) -> dict:
        return self._read()["roles"][req]

    def candidate(self, cid: str) -> dict:
        c = self._read()["candidates"][cid]
        return {"id": cid, "name": c["name"]}

    def cv(self, cid: str) -> str:
        return (FIXTURES / "cvs" / self._read()["candidates"][cid]["cv"]).read_text()

    def field(self, card: str, cid: str, name: str, by: str) -> str:
        c = self._read()["candidates"][cid]
        if name in PRIVATE:
            self.audit(card, "private fields", cid, f"refused {by}: {name.replace('_', ' ')}")
            raise Refused(f"{name} is private: screening may not use it")
        if name != "name":
            raise Refused(f"no field called {name}")
        return c["name"]

    def audit(self, card: str, check: str, cid: str, what: str) -> None:
        db = self._read()
        db.setdefault("audit", []).append({"card": card, "check": check, "candidate": cid, "what": what})
        self._write(db)

    def log(self, card: str) -> list[dict]:
        return [a for a in self._read().get("audit", []) if a["card"] == card]

    def flag(self, card: str, cid: str, hidden: list[str]) -> None:
        db = self._read()
        if cid in db.get("flags", {}).get(card, {}):
            return
        db.setdefault("flags", {}).setdefault(card, {})[cid] = {"hidden": hidden, "reviewed_by": None}
        self._write(db)
        self.audit(card, "hidden text", cid, "withheld from the screener: text a person cannot see")

    def flags(self, card: str) -> dict[str, dict]:
        return self._read().get("flags", {}).get(card, {})

    def review(self, card: str, cid: str, by: str, reject: bool) -> None:
        """A recruiter's call on a flagged CV. Only a person named in the system may make it."""
        db = self._read()
        if by != db["recruiter"]:
            raise Refused(f"{by} is not a recruiter on this role")
        f = db.get("flags", {}).get(card, {}).get(cid)
        if f is None:
            raise Refused(f"{cid} is not flagged on {card}")
        f.update(reviewed_by=by, rejected=reject)
        self._write(db)
        self.audit(
            card, "recruiter", cid, f"{by} {'rejected the application' if reject else 'cleared the CV'}"
        )

    def submit(self, card: str, req: str, ids: list[str], by: str) -> dict:
        db = self._read()
        role, flags = db["roles"][req], db.get("flags", {}).get(card, {})
        if len(set(ids)) != role["shortlist"] or len(ids) != role["shortlist"]:
            raise Refused(f"{role['title']} needs a shortlist of {role['shortlist']} different people")
        if stray := [i for i in ids if i not in role["applicants"]]:
            raise Refused(f"{', '.join(stray)} did not apply to {req}")
        if held := [i for i in ids if i in flags and (not flags[i]["reviewed_by"] or flags[i]["rejected"])]:
            raise Refused(f"{', '.join(held)} is flagged for a recruiter and cannot be shortlisted")
        sl = {"req": req, "candidates": ids, "by": by, "manager": role["manager"]}
        db.setdefault("shortlists", {})[card] = sl
        self._write(db)
        return sl

    def shortlist(self, card: str) -> dict | None:
        return self._read().get("shortlists", {}).get(card)
