"""Acme's payments ledger: the system of record a refund card is checked against.

The demo works on a copy of fixtures/<tenant>/ledger.json, so a run never edits the fixture. A refund is
keyed by the charge it returns: approving the same refund twice writes one row, which is what lets an
approval be retried after a crash or a double tap on the phone.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from workforce.sources import FIXTURES


@dataclass
class Ledger:
    path: Path
    charges: list[dict]
    refunds: list[dict]

    @classmethod
    def open(cls, path: Path, tenant: str = "acme") -> Ledger:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(FIXTURES / tenant / "ledger.json", path)
        d = json.loads(path.read_text())
        return cls(path, d["charges"], d["refunds"])

    def save(self) -> None:
        self.path.write_text(json.dumps({"charges": self.charges, "refunds": self.refunds}, indent=1))

    def charge(self, charge_id: str) -> dict:
        for c in self.charges:
            if c["id"] == charge_id:
                return c
        raise KeyError(f"no charge {charge_id}")

    def refunded(self, charge_id: str) -> bool:
        return any(r["charge"] == charge_id for r in self.refunds)

    def refund(self, charge_id: str, approved_by: str) -> dict:
        for r in self.refunds:
            if r["charge"] == charge_id:
                return r
        row = {"charge": charge_id, "amount": self.charge(charge_id)["amount"], "approved_by": approved_by}
        self.refunds.append(row)
        self.save()
        return row

    def duplicates(self, customer: str) -> list[str]:
        """Charges after the first on the same order that are not refunded. Empty means the customer was
        charged once per order, which is the only thing that closes a double charge card."""
        seen, extra = set(), []
        for c in sorted(self.charges, key=lambda c: c["at"]):
            if c["customer"] != customer:
                continue
            if c["order"] in seen and not self.refunded(c["id"]):
                extra.append(c["id"])
            seen.add(c["order"])
        return extra
