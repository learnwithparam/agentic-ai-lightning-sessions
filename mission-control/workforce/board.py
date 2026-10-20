"""Mission Control's board: every card, the column it is in, and what each run on it cost.

Cost per outcome is every run's cost, failed runs included, divided by the cards in Done. A card reaches
Done only through mission.settle, which asks the system of record, so the number counts outcomes and
not runs the agent called finished.
"""

from __future__ import annotations

import html
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from agents import Usage
from aiohttp import web

from workforce.ledger import Ledger

COLUMNS = ("Backlog", "Needs you", "In flight", "Done")


def price(usage: Usage) -> float:
    """USD for one run, from the per-million-token prices in WORKFORCE_PRICE_IN and WORKFORCE_PRICE_OUT."""
    per_in = float(os.environ.get("WORKFORCE_PRICE_IN") or 0)
    per_out = float(os.environ.get("WORKFORCE_PRICE_OUT") or 0)
    return (usage.input_tokens * per_in + usage.output_tokens * per_out) / 1_000_000


@dataclass
class Run:
    employee: str
    input_tokens: int
    output_tokens: int
    cost: float
    ok: bool
    note: str


@dataclass
class Card:
    id: str
    title: str
    customer: str
    issue: str
    column: str = "Backlog"
    employee: str = ""
    pending: dict | None = None
    runs: list[Run] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    answer: str = ""
    sources: list[str] = field(default_factory=list)

    @property
    def cost(self) -> float:
        return sum(r.cost for r in self.runs)


@dataclass
class Board:
    path: Path
    cards: list[Card] = field(default_factory=list)

    @classmethod
    def open(cls, path: Path) -> Board:
        if not path.exists():
            return cls(path)
        rows = json.loads(path.read_text())
        return cls(path, [Card(**{**r, "runs": [Run(**x) for x in r["runs"]]}) for r in rows])

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([asdict(c) for c in self.cards], indent=1))

    def card(self, card_id: str) -> Card:
        return next(c for c in self.cards if c.id == card_id)

    def move(self, card: Card, column: str, note: str) -> None:
        assert column in COLUMNS, column
        card.column = column
        card.log.append(f"{column}: {note}")
        self.save()

    @property
    def cost_per_outcome(self) -> float | None:
        done = sum(c.column == "Done" for c in self.cards)
        return sum(c.cost for c in self.cards) / done if done else None


LANE = {"Backlog": "", "Needs you": "wait", "In flight": "flight", "Done": "done"}
WEB = Path(__file__).resolve().parent.parent / "web"
MARK = (
    '<svg viewBox="0 0 20 20" aria-hidden="true">'
    '<rect x="1" y="3" width="4" height="14" rx="1" fill="#bdbdbd"/>'
    '<rect x="6" y="3" width="4" height="9" rx="1" fill="#f8d749"/><rect x="11" y="3" width="4" height="11" '
    'rx="1" fill="#171717"/><rect x="16" y="3" width="3" height="6" rx="1" fill="#1f7a45"/></svg>'
)


def usd(x: float) -> str:
    return f"${x:.4f}" if x < 1 else f"${x:,.2f}"


def plain(text: str) -> str:
    """The answer as prose: models add markdown even when told not to, and the board prints text."""
    return re.sub(r"\*\*|__|^#+\s*|^\s*[-*]\s+", "", text, flags=re.M)


def answered(c: Card) -> str:
    """What closed a question card: the answer, and only the sources the analyst read in that run."""
    refs = "".join(f'<span class="chip grey">{html.escape(s)}</span>' for s in c.sources)
    return (
        '<div class="receipt"><h4><svg class="ck" viewBox="0 0 16 16"><path d="M3 8.5 6.5 12 13 4.5"/></svg>'
        f"Answered from sources it read</h4><p>{html.escape(plain(c.answer)[:400])}</p>"
        f'<div class="refs">{refs}</div></div>'
    )


def receipt(c: Card, ledger: Ledger) -> str:
    """The ledger rows that closed the card: what was kept and what was refunded, and who approved it."""
    refunded = {r["charge"]: r for r in ledger.refunds}
    mine = [x for x in ledger.charges if x["customer"] == c.customer and x["id"] in refunded]
    rows = []
    for ch in mine:
        kept = [x for x in ledger.charges if x["order"] == ch["order"] and x["id"] not in refunded]
        rows += [("Order", ch["order"])]
        rows += [("Kept", f"{k['id']} {k['amount'] / 100:.2f}") for k in kept]
        rows += [
            ("Refunded", f"{ch['id']} {ch['amount'] / 100:.2f}"),
            ("Approved by", refunded[ch["id"]]["approved_by"]),
        ]
    cells = "".join(f"<dt>{html.escape(k)}</dt><dd>{html.escape(v)}</dd>" for k, v in rows)
    return (
        '<div class="receipt"><h4><svg class="ck" viewBox="0 0 16 16"><path d="M3 8.5 6.5 12 13 4.5"/></svg>'
        f"Ledger: one charge per order</h4><dl>{cells}</dl></div>"
    )


def card_html(c: Card, ledger: Ledger | None) -> str:
    refs = "".join(f'<span class="chip grey">{html.escape(r)}</span>' for r in c.issue.split(", "))
    gate = [line.removeprefix("Gate: ") for line in c.log if line.startswith("Gate: ")]
    last = next((line.split(": ", 1)[-1] for line in reversed(c.log) if not line.startswith("Gate: ")), "")
    body = (
        f'<article class="card tc"><h3>{html.escape(c.title)}</h3><div class="refs">{refs}</div>'
        f'<div class="who"><span><b>{html.escape(c.employee or "Unassigned")}</b></span>'
        f'<span class="cost">{len(c.runs)} {"run" if len(c.runs) == 1 else "runs"}, '
        f"{usd(c.cost)}</span></div>"
    )
    if c.pending:
        body += (
            f'<div class="ask"><b>Approve refund of {html.escape(c.pending["charge"])}?</b>'
            f"<span>{html.escape(c.pending['reason'])}. Requested by {html.escape(c.pending['maker'])}, "
            "sent to Telegram.</span></div>"
        )
    elif last:
        body += f'<p class="last">{html.escape(last[:1].upper() + last[1:])}</p>'
    # On a Done card the refusal is history, so it reads quiet instead of as a live failure.
    tone = "block past" if c.column == "Done" else "block"
    body += "".join(f'<p class="{tone}">Gate refused: {html.escape(g)}</p>' for g in gate[-1:])
    body += "</article>"
    if c.column == "Done" and c.sources:
        body += answered(c)
    elif c.column == "Done" and ledger is not None:
        body += receipt(c, ledger)
    return body


def render(board: Board, ledger: Ledger | None = None) -> str:
    lanes = ""
    for name in COLUMNS:
        cards = [c for c in board.cards if c.column == name]
        inner = "".join(card_html(c, ledger) for c in cards) or '<p class="none">Nothing here</p>'
        lanes += (
            f'<section class="lane {LANE[name]}" aria-label="{name}"><h2><i></i>{name}'
            f"<small>{len(cards)}</small></h2>{inner}</section>"
        )
    runs = [(c, r) for c in board.cards for r in c.runs]
    failed = sum(not r.ok for _, r in runs)
    log = (
        "".join(
            f"<tr><td>{html.escape(c.title)}</td><td>{html.escape(r.employee)}</td>"
            f'<td class="n hide">{r.input_tokens:,}</td><td class="n hide">{r.output_tokens:,}</td>'
            f'<td class="n">{usd(r.cost)}</td><td>{"Finished" if r.ok else "Failed"}</td>'
            f'<td class="said hide">{html.escape(r.note[:140])}</td></tr>'
            for c, r in reversed(runs)
        )
        or '<tr><td colspan="7">No runs yet. Run make demo to work the backlog.</td></tr>'
    )
    cpo, done = board.cost_per_outcome, sum(c.column == "Done" for c in board.cards)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Mission Control</title>
<link rel="stylesheet" href="/static/base.css"><link rel="stylesheet" href="/static/board.css"></head><body>
<nav class="top"><span class="brand">{MARK}Mission Control</span><a href="#board">Board</a>
<a href="#activity">Activity</a><span class="grow"></span>
<span class="tenant">Workspace <b>Acme</b></span></nav>
<div id="live"><div class="wrap tight" id="board"><div class="head"><div><h1 class="page">Board</h1>
<p>Each card is done only when the system of record agrees. Anything that cannot be undone waits for you.</p>
</div><div class="stats"><div class="stat lead"><span>Cost per outcome</span>
<b>{"None yet" if cpo is None else usd(cpo)}</b><em>failed runs included</em></div>
<div class="stat"><span>Outcomes</span><b>{done}</b></div>
<div class="stat"><span>Spent</span><b>{usd(sum(r.cost for _, r in runs))}</b>
<em>{len(runs)} runs, {failed} failed</em></div></div></div>
<div class="lanes">{lanes}</div>
<h2 class="sec" id="activity">Activity</h2><p class="sub">Every run on the board and what it cost.</p>
<table class="tbl"><thead><tr><th>Card</th><th>Employee</th><th class="n hide">Tokens in</th>
<th class="n hide">Tokens out</th><th class="n">Cost</th><th>Result</th><th class="hide">Said</th>
</tr></thead>
<tbody>{log}</tbody></table></div></div>
<script>setInterval(async()=>{{try{{const t=await (await fetch(location.pathname)).text();
const n=new DOMParser().parseFromString(t,"text/html").getElementById("live");
if(n)document.getElementById("live").replaceWith(n)}}catch(e){{}}}},2000)</script></body></html>"""


def serve(state: Path, port: int = 8080) -> None:
    async def page(_: web.Request) -> web.Response:
        ledger = Ledger.open(state / "ledger.json")
        return web.Response(text=render(Board.open(state / "board.json"), ledger), content_type="text/html")

    app = web.Application()
    app.router.add_get("/", page)
    app.router.add_static("/static", WEB)
    app.router.add_static("/fonts", WEB / "fonts")
    web.run_app(app, host="127.0.0.1", port=port)
