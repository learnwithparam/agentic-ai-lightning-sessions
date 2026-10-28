"""Mission Control's board: every card, the column it is in, and what each run on it cost.

Cost per outcome is every run's cost, failed runs included, divided by the cards in Done. A card reaches
Done only through mission.settle or mission.answer, which ask the system of record, so the number counts
outcomes and not runs the agent called finished. The board is also where a human approves: a card in
Needs you shows the draft, what it rests on, and Approve and Decline buttons when approval was asked here.
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

COLUMNS = ("Backlog", "Needs you", "In flight", "Done")


def price(usage: Usage) -> float:
    """USD for one run, from the per-million-token prices in WORKFORCE_PRICE_IN and WORKFORCE_PRICE_OUT.

    Input the provider served from its cache is billed at WORKFORCE_PRICE_CACHED, the input price if unset.
    """
    per_in = float(os.environ.get("WORKFORCE_PRICE_IN") or 0)
    per_out = float(os.environ.get("WORKFORCE_PRICE_OUT") or 0)
    per_cached = float(os.environ.get("WORKFORCE_PRICE_CACHED") or per_in)
    cached = usage.input_tokens_details.cached_tokens or 0
    fresh = usage.input_tokens - cached
    return (fresh * per_in + cached * per_cached + usage.output_tokens * per_out) / 1_000_000


@dataclass(frozen=True)
class Role:
    """One AI employee: the outcome it is hired for, the metric that watches it, what it must never do."""

    name: str
    outcome: str
    metric: str
    never: str
    tools: tuple[str, ...]


@dataclass
class Run:
    employee: str
    input_tokens: int
    output_tokens: int
    cost: float
    ok: bool
    note: str
    steps: list[str] = field(default_factory=list)
    read: list[str] = field(default_factory=list)


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

    @property
    def github(self) -> int | None:
        """The customer's GitHub issue number, from a link like GH#412."""
        m = re.search(r"GH#(\d+)", self.issue)
        return int(m.group(1)) if m else None


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
CHECK = '<svg class="ck" viewBox="0 0 16 16"><path d="M3 8.5 6.5 12 13 4.5"/></svg>'
E = html.escape


def usd(x: float) -> str:
    return f"${x:.4f}" if x < 1 else f"${x:,.2f}"


def plain(text: str) -> str:
    """The answer as prose: models add markdown even when told not to, and the board prints text."""
    return re.sub(r"\*\*|__|^#+\s*|^\s*[-*]\s+", "", text, flags=re.M)


def chips(ids: list[str], tone: str = "grey") -> str:
    return "".join(f'<span class="chip {tone}">{E(s)}</span>' for s in ids)


def answered(c: Card) -> str:
    """What closed a question card: the answer, and only the sources the analyst read in that run."""
    return (
        f'<div class="receipt"><h4>{CHECK}Answered from sources it read</h4>'
        f'<p>{E(plain(c.answer)[:400])}</p><div class="refs">{chips(c.sources)}</div></div>'
    )


def receipt(c: Card, comment: dict) -> str:
    """The comment GitHub has on the customer's issue, read back from GitHub, and who approved it."""
    body = re.sub(r"\s*<!--.*?-->", "", comment["body"]).strip()
    rows = [("Issue", f"GH#{c.github}"), ("Posted as", comment["author"])]
    rows += [("Approved by", comment["approved_by"])] if comment.get("approved_by") else []
    cells = "".join(f"<dt>{E(k)}</dt><dd>{E(v)}</dd>" for k, v in rows)
    return (
        f'<div class="receipt"><h4>{CHECK}GitHub shows the reply</h4><p>{E(body[:400])}</p>'
        f"<dl>{cells}</dl></div>"
    )


def ask(c: Card, full: bool) -> str:
    p = c.pending
    asked = {"board": "here", "telegram": "on Telegram", "terminal": "in the terminal"}
    where = asked.get(p.get("asked"), "")
    text = p["body"] if full else p["body"][:160] + ("..." if len(p["body"]) > 160 else "")
    out = (
        f'<div class="ask"><b>Post this reply on GH#{p["issue"]}?</b><span>{E(text)}</span>'
        f'<div class="refs">Rests on {chips(p["rests_on"], "wait")}</div>'
        f"<em>Drafted by {E(p['maker'])}{', waiting ' + where if where else ''}.</em>"
    )
    if p.get("asked") == "board":
        out += (
            f'<form method="post" action="/cards/{E(c.id)}/decide" class="decide">'
            '<button class="btn dark" name="yes" value="1">Approve and post</button>'
            '<button class="btn" name="yes" value="0">Decline</button></form>'
        )
    return out + "</div>"


def card_html(c: Card, replied: dict | None) -> str:
    gate = [line.removeprefix("Gate: ") for line in c.log if line.startswith("Gate: ")]
    last = next((line.split(": ", 1)[-1] for line in reversed(c.log) if not line.startswith("Gate: ")), "")
    body = (
        f'<article class="card tc"><h3><a href="/cards/{E(c.id)}">{E(c.title)}</a></h3>'
        f'<div class="refs">{chips(c.issue.split(", "))}</div>'
        f'<div class="who"><span><b>{E(c.employee or "Unassigned")}</b></span>'
        f'<span class="cost">{len(c.runs)} {"run" if len(c.runs) == 1 else "runs"}, '
        f"{usd(c.cost)}</span></div>"
    )
    if c.pending:
        body += ask(c, full=False)
    elif last:
        body += f'<p class="last">{E(last[:1].upper() + last[1:])}</p>'
    # On a Done card the refusal is history, so it reads quiet instead of as a live failure.
    tone = "block past" if c.column == "Done" else "block"
    body += "".join(f'<p class="{tone}">Gate refused: {E(g)}</p>' for g in gate[-1:])
    body += "</article>"
    if c.column == "Done" and c.sources:
        body += answered(c)
    elif c.column == "Done" and replied:
        body += receipt(c, replied)
    return body


def page(title: str, live: str, here: str, workspace: str = "Acme", signed_in: str = "") -> str:
    links = "".join(
        f'<a href="{href}"{" aria-current=page" if name == here else ""}>{name}</a>'
        for name, href in (("Board", "/"), ("Team", "/team"), ("Activity", "/#activity"))
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{E(title)}</title>
<link rel="stylesheet" href="/static/base.css"><link rel="stylesheet" href="/static/board.css"></head><body>
<nav class="top"><a class="brand" href="/">{MARK}Mission Control</a>{links}<span class="grow"></span>
<span class="tenant">Workspace <b>{E(workspace)}</b></span>{signed_in}</nav>
<div id="live">{live}</div>
<script>setInterval(async()=>{{try{{const t=await (await fetch(location.pathname)).text();
const n=new DOMParser().parseFromString(t,"text/html").getElementById("live");
const o=document.getElementById("live");
// Swapping an unchanged page, or the buttons under the pointer, eats the click on Approve.
if(n&&n.innerHTML!==o.innerHTML&&!document.querySelector(".decide:hover"))o.replaceWith(n)}}catch(e){{}}}},2000)
</script></body></html>"""


Replies = dict[str, dict]


def render(board: Board, replies: Replies | None = None, workspace: str = "Acme", signed_in: str = "") -> str:
    replies = replies or {}
    lanes = ""
    for name in COLUMNS:
        cards = [c for c in board.cards if c.column == name]
        inner = "".join(card_html(c, replies.get(c.id)) for c in cards) or '<p class="none">Nothing here</p>'
        lanes += (
            f'<section class="lane {LANE[name]}" aria-label="{name}"><h2><i></i>{name}'
            f"<small>{len(cards)}</small></h2>{inner}</section>"
        )
    runs = [(c, r) for c in board.cards for r in c.runs]
    failed = sum(not r.ok for _, r in runs)
    log = (
        "".join(
            f'<tr><td><a href="/cards/{E(c.id)}">{E(c.title)}</a></td><td>{E(r.employee)}</td>'
            f'<td class="n hide">{r.input_tokens:,}</td><td class="n hide">{r.output_tokens:,}</td>'
            f'<td class="n">{usd(r.cost)}</td><td>{"Finished" if r.ok else "Failed"}</td>'
            f'<td class="said hide">{E(r.note[:140])}</td></tr>'
            for c, r in reversed(runs)
        )
        or '<tr><td colspan="7">No runs yet. Run make demo to work the backlog.</td></tr>'
    )
    cpo, done = board.cost_per_outcome, sum(c.column == "Done" for c in board.cards)
    live = f"""<div class="wrap tight" id="board"><div class="head"><div><h1 class="page">Board</h1>
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
</tr></thead><tbody>{log}</tbody></table></div>"""
    return page("Mission Control", live, "Board", workspace, signed_in)


def detail(c: Card, replied: dict | None, workspace: str = "Acme", signed_in: str = "") -> str:
    """One card: where it stands, every run with its steps and cost, and what closed it."""
    runs = (
        "".join(
            f'<li class="card run"><div class="who"><b>{E(r.employee)}</b><span class="cost">'
            f"{r.input_tokens:,} in, {r.output_tokens:,} out, {usd(r.cost)}, "
            f"{'finished' if r.ok else 'failed'}</span></div>"
            f'<ol class="steps">{"".join(f"<li>{E(s)}</li>" for s in r.steps)}</ol>'
            f"{'<div class=refs>Read ' + chips(r.read) + '</div>' if r.read else ''}"
            f'<p class="last">{E(plain(r.note)[:300])}</p></li>'
            for r in c.runs
        )
        or '<li class="none">No runs yet.</li>'
    )
    timeline = "".join(f"<li>{E(line)}</li>" for line in c.log) or '<li class="none">Not started.</li>'
    closed = ""
    if c.pending:
        closed = ask(c, full=True)
    elif c.column == "Done" and c.sources:
        closed = answered(c)
    elif c.column == "Done" and replied:
        closed = receipt(c, replied)
    live = f"""<div class="wrap tight detail"><p class="eyebrow">{E(c.id)} &middot; {E(c.column)}</p>
<h1 class="page">{E(c.title)}</h1><div class="refs">{chips(c.issue.split(", "))}
{chips([c.customer], "blue") if c.customer else ""}</div>
<div class="stats"><div class="stat"><span>Employee</span><b>{E(c.employee or "Unassigned")}</b></div>
<div class="stat"><span>Runs</span><b>{len(c.runs)}</b></div>
<div class="stat"><span>Cost</span><b>{usd(c.cost)}</b><em>failed runs included</em></div></div>
{closed}<h2 class="sec">Runs</h2><ol class="runs">{runs}</ol>
<h2 class="sec">Timeline</h2><ol class="timeline">{timeline}</ol></div>"""
    return page(c.title, live, "", workspace, signed_in)


def team(board: Board, roster: tuple[Role, ...], workspace: str = "Acme", signed_in: str = "") -> str:
    """Every AI employee: what it is hired for, what it must never do, and what its outcomes cost."""
    rows = ""
    for r in roster:
        # A crew card names every employee who worked it, and each is credited with its outcome.
        mine = [c for c in board.cards if r.name in c.employee.split(", ")]
        done = sum(c.column == "Done" for c in mine)
        spent = sum(c.cost for c in mine)
        rows += (
            f'<article class="card role"><h3>{E(r.name)}</h3><dl>'
            f"<dt>Hired for</dt><dd>{E(r.outcome)}</dd><dt>Watched by</dt><dd>{E(r.metric)}</dd>"
            f'<dt>Must never</dt><dd class="never">{E(r.never)}</dd></dl>'
            f'<div class="refs">{chips(list(r.tools))}</div><div class="who">'
            f"<span><b>{done}</b> done of {len(mine)}</span>"
            f'<span class="cost">{usd(spent / done) + " per outcome" if done else "no outcome yet"}</span>'
            "</div></article>"
        )
    live = f"""<div class="wrap tight"><div class="head"><div><h1 class="page">Team</h1>
<p>Each AI employee is hired for one outcome and has one thing it must never do. A test holds each line.</p>
</div></div><div class="roster">{rows}</div></div>"""
    return page("Team", live, "Team", workspace, signed_in)


def app(state: Path, roster: tuple[Role, ...]) -> web.Application:
    from workforce.issues import Issues

    def read() -> tuple[Board, Replies]:
        board, issues = Board.open(state / "board.json"), Issues.open(state / "github.json")
        replies = {c.id: found for c in board.cards if c.github and (found := issues.replied(c.github, c.id))}
        return board, replies

    async def index(_: web.Request) -> web.Response:
        return web.Response(text=render(*read()), content_type="text/html")

    async def one(req: web.Request) -> web.Response:
        board, replies = read()
        try:
            c = board.card(req.match_info["id"])
        except StopIteration:
            raise web.HTTPNotFound() from None
        return web.Response(text=detail(c, replies.get(c.id)), content_type="text/html")

    async def roster_page(_: web.Request) -> web.Response:
        return web.Response(text=team(read()[0], roster), content_type="text/html")

    async def decide(req: web.Request) -> web.Response:
        # The page only records the click; mission.on_board hands it to decide, which holds the rule.
        card_id = req.match_info["id"]
        board = Board.open(state / "board.json")
        if not any(c.id == card_id and c.pending for c in board.cards):
            raise web.HTTPConflict(text="nothing is waiting on this card")
        # Any page open in the same browser can post to 127.0.0.1, or rebind its own name to it; only the
        # board's own form, served from loopback, counts.
        local = req.host.rsplit(":", 1)[0] in ("127.0.0.1", "localhost")
        if not local or req.headers.get("Origin") != f"{req.scheme}://{req.host}":
            raise web.HTTPForbidden(text="approve from the board itself")
        form = await req.post()
        f = state / "decisions" / f"{card_id}.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        by = f"board:{os.environ.get('USER', 'human')}"
        f.write_text(json.dumps({"by": by, "yes": form.get("yes") == "1"}))
        raise web.HTTPSeeOther(f"/cards/{card_id}")

    a = web.Application()
    a.router.add_get("/", index)
    a.router.add_get("/team", roster_page)
    a.router.add_get("/cards/{id}", one)
    a.router.add_post("/cards/{id}/decide", decide)
    a.router.add_static("/static", WEB)
    a.router.add_static("/fonts", WEB / "fonts")
    return a


async def start(state: Path, roster: tuple[Role, ...], port: int = 8080) -> web.AppRunner:
    """Serve the board inside the running demo, so it moves while the employees work."""
    runner = web.AppRunner(app(state, roster))
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner


def serve(state: Path, roster: tuple[Role, ...], port: int = 8080) -> None:
    web.run_app(app(state, roster), host="127.0.0.1", port=port)
