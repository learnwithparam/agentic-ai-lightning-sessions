"""Acme Insurance's claims system, the record the board settles on: claims, documents, a ledger, filings.

Its rules are code, so no document or prompt can move them. The ledger records what each document cost to
read from the provider's own token counts, cached tokens included. A claim reads its next document only
if it could afford another read like its last one inside the budget; otherwise it stops for the adjuster.
A claim is filed only when its total is the Total line printed on the body shop's quote, and a claim with no
quote is filed only with no total.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from agents import Usage
from pypdf import PdfReader

from workforce.board import price

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
DOCS = FIXTURES / "claims"
TOTAL = re.compile(r"^\s*Total\s+(£[\d,]+\.\d\d)\s*$", re.M)


class Refused(Exception):
    pass


def quote_text(name: str) -> str:
    """The quote as its text layer reads, the way the claims system and the reader both see it."""
    return "\n".join(p.extract_text() for p in PdfReader(DOCS / name).pages)


def quote_total(name: str) -> str:
    found = TOTAL.findall(quote_text(name))
    if len(found) != 1:
        raise Refused(f"{name} does not print exactly one Total line")
    return found[0]


@dataclass
class Claims:
    state: Path

    @classmethod
    def open(cls, state: Path) -> Claims:
        db = state / "claims.json"
        if not db.exists():
            state.mkdir(parents=True, exist_ok=True)
            shutil.copy(FIXTURES / "claims.json", db)
        return cls(state)

    def _read(self) -> dict:
        return json.loads((self.state / "claims.json").read_text())

    def _write(self, db: dict) -> None:
        (self.state / "claims.json").write_text(json.dumps(db, indent=1, ensure_ascii=False))

    @property
    def adjuster(self) -> str:
        return self._read()["adjuster"]

    @property
    def budget(self) -> float:
        return self._read()["budget_usd"]

    def claim(self, cid: str) -> dict:
        return {"id": cid, **self._read()["claims"][cid]}

    def quote(self, cid: str) -> str | None:
        return next((d for d in self.claim(cid)["documents"] if d.endswith(".pdf")), None)

    def ledger(self, cid: str) -> list[dict]:
        return self._read().get("ledger", {}).get(cid, [])

    def spent(self, cid: str) -> float:
        return sum(t["cost"] for t in self.ledger(cid))

    def allow(self, cid: str, doc: str) -> None:
        """Before each read: stop the claim if one more read like the last would cross its budget."""
        rows = self.ledger(cid)
        last = rows[-1]["cost"] if rows else 0
        if self.spent(cid) + last > self.budget:
            db = self._read()
            db.setdefault("stopped", {})[cid] = {"before": doc, "read": len(rows), "spent": self.spent(cid)}
            self._write(db)
            raise Refused(f"the next read would take {cid} past its budget")

    def record(self, cid: str, doc: str, usage: Usage) -> dict:
        row = {
            "doc": doc,
            "input": usage.input_tokens,
            "cached": usage.input_tokens_details.cached_tokens or 0,
            "output": usage.output_tokens,
            "cost": price(usage),
        }
        db = self._read()
        db.setdefault("ledger", {}).setdefault(cid, []).append(row)
        self._write(db)
        return row

    def stopped(self, cid: str) -> dict | None:
        return self._read().get("stopped", {}).get(cid)

    def file(self, cid: str, claim_file: dict) -> dict:
        quote, total = self.quote(cid), claim_file.get("total")
        if quote is None and total is not None:
            self._refuse(cid, f"{total} has no quote behind it; an assessor prices this claim")
        if quote is not None and total != (printed := quote_total(quote)):
            self._refuse(cid, f"{total or 'no total'} is not the {printed} printed on {quote}")
        db = self._read()
        db.setdefault("filed", {})[cid] = claim_file
        self._write(db)
        return claim_file

    def _refuse(self, cid: str, why: str) -> None:
        db = self._read()
        db.setdefault("refused", {})[cid] = why
        self._write(db)
        raise Refused(why)

    def filed(self, cid: str) -> dict | None:
        return self._read().get("filed", {}).get(cid)

    def refused(self, cid: str) -> str | None:
        return self._read().get("refused", {}).get(cid)
