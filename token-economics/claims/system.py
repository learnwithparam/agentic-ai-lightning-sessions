"""KaaS Insurance's claims system, the record the board settles on: claims, documents, a ledger, filings.

Its rules are code, so no document or prompt can move them. The ledger records what each document cost to
read from the provider's own token counts, cached tokens included. A claim reads its next document only
if it could afford another read like its last one inside the budget; otherwise it stops for the adjuster,
and only the adjuster can raise that claim's budget.
A claim is filed only when its total is the Total line printed on the body shop's quote, and a claim with no
quote is filed only with no total.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agents import Usage
from pypdf import PdfReader

from workforce.board import is_blank, price, usd

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
DOCS = FIXTURES / "claims"
TOTAL = re.compile(r"^\s*Total\s+(£[\d,]+\.\d\d)\s*$", re.M)


class Refused(Exception):
    pass


def quote_text(name: str) -> str:
    """The quote as its text layer reads, the way the claims system and the reader both see it."""
    return "\n".join(page.extract_text() for page in PdfReader(DOCS / name).pages)


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
        data_file = state / "claims.json"
        if is_blank(data_file):
            state.mkdir(parents=True, exist_ok=True)
            shutil.copy(FIXTURES / "claims.json", data_file)
        return cls(state)

    def _read(self) -> dict:
        return json.loads((self.state / "claims.json").read_text())

    def _write(self, data: dict) -> None:
        (self.state / "claims.json").write_text(json.dumps(data, indent=1, ensure_ascii=False))

    @property
    def adjuster(self) -> str:
        return self._read()["adjuster"]

    @property
    def budget(self) -> float:
        return self._read()["budget_usd"]

    @property
    def raised_budget(self) -> float:
        return self._read()["raised_budget_usd"]

    def budget_for(self, claim_id: str) -> float:
        data = self._read()
        return data.get("budgets", {}).get(claim_id, data["budget_usd"])

    def claim(self, claim_id: str) -> dict:
        return {"id": claim_id, **self._read()["claims"][claim_id]}

    def quote(self, claim_id: str) -> str | None:
        return next((doc for doc in self.claim(claim_id)["documents"] if doc.endswith(".pdf")), None)

    def ledger(self, claim_id: str) -> list[dict]:
        return self._read().get("ledger", {}).get(claim_id, [])

    def spent(self, claim_id: str) -> float:
        return sum(row["cost"] for row in self.ledger(claim_id))

    def allow(self, claim_id: str, doc: str) -> None:
        """Before each read: stop the claim if one more read like the last would cross its budget."""
        rows = self.ledger(claim_id)
        last = rows[-1]["cost"] if rows else 0
        if self.spent(claim_id) + last > self.budget_for(claim_id):
            data = self._read()
            data.setdefault("stopped", {})[claim_id] = {
                "before": doc,
                "read": len(rows),
                "spent": self.spent(claim_id),
            }
            self._write(data)
            raise Refused(f"the next read would take {claim_id} past its budget")

    def record(self, claim_id: str, doc: str, usage: Usage, claim_file: dict) -> dict:
        """The claim file is kept with the row so a raised budget resumes from it, not from the start."""
        row = {
            "doc": doc,
            "input": usage.input_tokens,
            "cached": usage.input_tokens_details.cached_tokens or 0,
            "output": usage.output_tokens,
            "cost": price(usage),
            "claim_file": claim_file,
        }
        data = self._read()
        data.setdefault("ledger", {}).setdefault(claim_id, []).append(row)
        self._write(data)
        return row

    def stopped(self, claim_id: str) -> dict | None:
        return self._read().get("stopped", {}).get(claim_id)

    def decide_budget(self, claim_id: str, decided_by: str, approve: bool) -> str:
        """Only the adjuster raises a claim's budget; anyone else is refused and the refusal is logged."""
        if decided_by != self.adjuster:
            said = f"{decided_by} is not the adjuster: only {self.adjuster} can raise a claim's budget"
            self._note(claim_id, decided_by, "refused", said)
            raise Refused(said)
        if self.stopped(claim_id) is None:
            raise Refused(f"{claim_id} is not waiting for a budget")
        data = self._read()
        if approve:
            data.setdefault("budgets", {})[claim_id] = data["raised_budget_usd"]
            said = f"{decided_by} raised {claim_id}'s budget to {usd(data['raised_budget_usd'])}"
        else:
            data.setdefault("declined", {})[claim_id] = decided_by
            said = f"{decided_by} kept {claim_id}'s budget at {usd(data['budget_usd'])}"
        self._write(data)
        self._note(claim_id, decided_by, "raised" if approve else "declined", said)
        data = self._read()
        del data["stopped"][claim_id]
        self._write(data)
        return said

    def _note(self, claim_id: str, decided_by: str, decision: str, said: str) -> None:
        data = self._read()
        stop = data.get("stopped", {}).get(claim_id, {})
        data.setdefault("decisions", {}).setdefault(claim_id, []).append(
            {
                "claim": claim_id,
                "by": decided_by,
                "decision": decision,
                "said": said,
                "after_read": stop.get("read", len(self.ledger(claim_id))),
                "at": datetime.now().strftime("%H:%M"),
            }
        )
        self._write(data)

    def decisions(self, claim_id: str) -> list[dict]:
        return self._read().get("decisions", {}).get(claim_id, [])

    def declined(self, claim_id: str) -> str | None:
        return self._read().get("declined", {}).get(claim_id)

    def file(self, claim_id: str, claim_file: dict) -> dict:
        quote, total = self.quote(claim_id), claim_file.get("total")
        if quote is None and total is not None:
            self._refuse(claim_id, f"{total} has no quote behind it; an assessor prices this claim")
        if quote is not None:
            printed = quote_total(quote)
            if total != printed:
                self._refuse(claim_id, f"{total or 'no total'} is not the {printed} printed on {quote}")
        data = self._read()
        data.setdefault("filed", {})[claim_id] = claim_file
        self._write(data)
        return claim_file

    def _refuse(self, claim_id: str, why: str) -> None:
        data = self._read()
        data.setdefault("refused", {})[claim_id] = why
        self._write(data)
        raise Refused(why)

    def filed(self, claim_id: str) -> dict | None:
        return self._read().get("filed", {}).get(claim_id)

    def refused(self, claim_id: str) -> str | None:
        return self._read().get("refused", {}).get(claim_id)
