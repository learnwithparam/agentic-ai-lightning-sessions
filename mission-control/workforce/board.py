"""Mission Control's board: every card, the column it is in, and what each run on it cost.

Cost per outcome is every run's cost, failed runs included, divided by the cards in Done. A card reaches
Done only through mission.settle or mission.answer, which ask the system of record, so the number counts
outcomes and not runs the agent called finished. The board is also where a human approves: a card in
Needs you shows the draft, what it rests on, and Approve and Decline buttons when approval was asked here.
"""

from __future__ import annotations

import contextlib
import html
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from agents import Usage
from aiohttp import web

from workforce.access import Access
from workforce.people import PERSON, People, people_or_exit, require_sign_in, signed_in_html

PEOPLE_FILE = Path(__file__).resolve().parent.parent / "fixtures" / "people.json"
COLUMNS = ("Backlog", "Needs you", "In flight", "Done")


def is_blank(path: Path) -> bool:
    """A missing or empty state file: a run that has not started, or one killed while it wrote."""
    return not path.exists() or not path.read_text().strip()


def read_json(path: Path, empty):
    return empty if is_blank(path) else json.loads(path.read_text())


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
    succeeded: bool
    note: str
    steps: list[str] = field(default_factory=list)
    read: list[str] = field(default_factory=list)
    killed: bool = False


@dataclass
class Card:
    card_id: str
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
        return sum(run.cost for run in self.runs)

    @property
    def github(self) -> int | None:
        """The customer's GitHub issue number, from a link like GH#412."""
        found = re.search(r"GH#(\d+)", self.issue)
        return int(found.group(1)) if found else None


@dataclass
class Board:
    path: Path
    cards: list[Card] = field(default_factory=list)

    @classmethod
    def open(cls, path: Path) -> Board:
        rows = read_json(path, [])
        return cls(path, [Card(**{**row, "runs": [Run(**run) for run in row["runs"]]}) for row in rows])

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A reader never sees half a file: the temp file is complete before it takes the name.
        pending = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        pending.write_text(json.dumps([asdict(card) for card in self.cards], indent=1))
        os.replace(pending, self.path)

    def card(self, card_id: str) -> Card:
        return next(card for card in self.cards if card.card_id == card_id)

    def move(self, card: Card, column: str, note: str) -> None:
        assert column in COLUMNS, column
        card.column = column
        card.log.append(f"{column}: {note}")
        self.save()

    @property
    def cost_per_outcome(self) -> float | None:
        done = sum(card.column == "Done" for card in self.cards)
        return sum(card.cost for card in self.cards) / done if done else None


LANE = {"Backlog": "", "Needs you": "wait", "In flight": "flight", "Done": "done"}
WEB = Path(__file__).resolve().parent.parent / "web"
MARK = (
    '<svg viewBox="0 0 20 20" aria-hidden="true">'
    '<rect x="1" y="3" width="4" height="14" rx="1" fill="#bdbdbd"/>'
    '<rect x="6" y="3" width="4" height="9" rx="1" fill="#f8d749"/><rect x="11" y="3" width="4" height="11" '
    'rx="1" fill="#171717"/><rect x="16" y="3" width="3" height="6" rx="1" fill="#1f7a45"/></svg>'
)
CHECK = '<svg class="ck" viewBox="0 0 16 16"><path d="M3 8.5 6.5 12 13 4.5"/></svg>'
escape = html.escape
WORKSPACE = "KaaS"


def usd(amount: float) -> str:
    return f"${amount:.4f}" if amount < 1 else f"${amount:,.2f}"


def plain(text: str) -> str:
    """The answer as prose: models add markdown even when told not to, and the board prints text."""
    return re.sub(r"\*\*|__|^#+\s*|^\s*[-*]\s+", "", text, flags=re.M)


def chips(ids: list[str], tone: str = "grey") -> str:
    return "".join(f'<span class="chip {tone}">{escape(label)}</span>' for label in ids)


def answered(card: Card) -> str:
    """What closed a question card: the answer, and only the sources the analyst read in that run."""
    return (
        f'<div class="receipt"><h4>{CHECK}Answered from sources it read</h4>'
        f'<p>{escape(plain(card.answer)[:400])}</p><div class="refs">{chips(card.sources)}</div></div>'
    )


def receipt(card: Card, comment: dict) -> str:
    """The comment GitHub has on the customer's issue, read back from GitHub, and who approved it."""
    body = re.sub(r"\s*<!--.*?-->", "", comment["body"]).strip()
    rows = [("Issue", f"GH#{card.github}"), ("Posted as", comment["author"])]
    rows += [("Approved by", comment["approved_by"])] if comment.get("approved_by") else []
    cells = "".join(f"<dt>{escape(label)}</dt><dd>{escape(value)}</dd>" for label, value in rows)
    return (
        f'<div class="receipt"><h4>{CHECK}GitHub shows the reply</h4><p>{escape(body[:400])}</p>'
        f"<dl>{cells}</dl></div>"
    )


def ask(card: Card, full: bool) -> str:
    pending = card.pending
    body = pending["body"]
    text = body if full else body[:160] + ("..." if len(body) > 160 else "")
    out = (
        f'<div class="ask"><b>Post this reply on GH#{pending["issue"]}?</b><span>{escape(text)}</span>'
        f'<div class="refs">Rests on {chips(pending["rests_on"], "wait")}</div>'
        f"<em>Drafted by {escape(pending['maker'])}. Approve here, signed in.</em>"
    )
    out += (
        f'<form method="post" action="/cards/{escape(card.card_id)}/decide" class="decide">'
        '<button class="btn dark" name="yes" value="1">Approve and post</button>'
        '<button class="btn" name="yes" value="0">Decline</button></form>'
    )
    return out + "</div>"


def card_html(card: Card, replied: dict | None) -> str:
    gate = [line[len("Gate: ") :] for line in card.log if line.startswith("Gate: ")]
    last = next((line.split(": ", 1)[-1] for line in reversed(card.log) if not line.startswith("Gate: ")), "")
    body = (
        f'<article class="card tc"><h3><a href="/cards/{escape(card.card_id)}">{escape(card.title)}</a></h3>'
        f'<div class="refs">{chips(card.issue.split(", "))}</div>'
        f'<div class="who"><span><b>{escape(card.employee or "Unassigned")}</b></span>'
        f'<span class="cost">{len(card.runs)} {"run" if len(card.runs) == 1 else "runs"}, '
        f"{usd(card.cost)}</span></div>"
    )
    if card.pending:
        body += ask(card, full=False)
    elif last:
        body += f'<p class="last">{escape(last[:1].upper() + last[1:])}</p>'
    # On a Done card the refusal is history, so it reads quiet instead of as a live failure.
    tone = "block past" if card.column == "Done" else "block"
    body += "".join(f'<p class="{tone}">Gate refused: {escape(note)}</p>' for note in gate[-1:])
    body += "</article>"
    if card.column == "Done" and card.sources:
        body += answered(card)
    elif card.column == "Done" and replied:
        body += receipt(card, replied)
    return body


def page(title: str, live: str, here: str, *, workspace: str, signed_in: str = "") -> str:
    links = "".join(
        f'<a href="{href}"{" aria-current=page" if name == here else ""}>{name}</a>'
        for name, href in (("Board", "/"), ("Team", "/team"), ("Activity", "/#activity"))
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{escape(title)}</title>
<link rel="stylesheet" href="/static/base.css"><link rel="stylesheet" href="/static/board.css"></head><body>
<nav class="top"><a class="brand" href="/">{MARK}Mission Control</a>{links}<span class="grow"></span>
<span class="tenant">Workspace <b>{escape(workspace)}</b></span>{signed_in}</nav>
<div id="live">{live}</div>
<script>setInterval(async()=>{{try{{
// Swapping an unchanged page, a page being typed in, or the buttons under the pointer eats the click.
if(document.hidden||document.querySelector("form:focus-within,.decide:hover"))return;
const text=await (await fetch(location.pathname)).text();
const fresh=new DOMParser().parseFromString(text,"text/html").getElementById("live");
const old=document.getElementById("live");
if(fresh&&fresh.innerHTML!==old.innerHTML)old.replaceWith(fresh)}}catch(error){{}}}},2000)
</script></body></html>"""


Replies = dict[str, dict]


class View(NamedTuple):
    """What one page load reads: the board, GitHub's reply on each card, and the issues."""

    board: Board
    replies: Replies
    issues: Any


def render(board: Board, replies: Replies | None = None, *, workspace: str, signed_in: str = "") -> str:
    replies = replies or {}
    lanes = ""
    for name in COLUMNS:
        cards = [card for card in board.cards if card.column == name]
        inner = (
            "".join(card_html(card, replies.get(card.card_id)) for card in cards)
            or '<p class="none">Nothing here</p>'
        )
        lanes += (
            f'<section class="lane {LANE[name]}" aria-label="{name}"><h2><i></i>{name}'
            f"<small>{len(cards)}</small></h2>{inner}</section>"
        )
    runs = [(card, run) for card in board.cards for run in card.runs]
    failed = sum(not run.succeeded for _, run in runs)
    log = (
        "".join(
            f'<tr><td><a href="/cards/{escape(card.card_id)}">{escape(card.title)}</a></td>'
            f"<td>{escape(run.employee)}</td>"
            f'<td class="n hide">{run.input_tokens:,}</td><td class="n hide">{run.output_tokens:,}</td>'
            f'<td class="n">{usd(run.cost)}</td><td>{"Finished" if run.succeeded else "Failed"}</td>'
            f'<td class="said hide">{escape(run.note[:140])}</td></tr>'
            for card, run in reversed(runs)
        )
        or '<tr><td colspan="7">No runs yet. Run make demo to work the backlog.</td></tr>'
    )
    per_outcome, done = board.cost_per_outcome, sum(card.column == "Done" for card in board.cards)
    live = f"""<div class="wrap tight" id="board"><div class="head"><div><h1 class="page">Board</h1>
<p>Each card is done only when the system of record agrees. Anything that cannot be undone waits for you.</p>
</div><div class="stats"><div class="stat lead"><span>Cost per outcome</span>
<b>{"None yet" if per_outcome is None else usd(per_outcome)}</b><em>failed runs included</em></div>
<div class="stat"><span>Outcomes</span><b>{done}</b></div>
<div class="stat"><span>Spent</span><b>{usd(sum(run.cost for _, run in runs))}</b>
<em>{len(runs)} runs, {failed} failed</em></div></div></div>
<div class="lanes">{lanes}</div>
<h2 class="sec" id="activity">Activity</h2><p class="sub">Every run on the board and what it cost.</p>
<table class="tbl"><thead><tr><th>Card</th><th>Employee</th><th class="n hide">Tokens in</th>
<th class="n hide">Tokens out</th><th class="n">Cost</th><th>Result</th><th class="hide">Said</th>
</tr></thead><tbody>{log}</tbody></table></div>"""
    return page("Mission Control", live, "Board", workspace=workspace, signed_in=signed_in)


def detail(card: Card, replied: dict | None, *, workspace: str, signed_in: str = "") -> str:
    """One card: where it stands, every run with its steps and cost, and what closed it."""
    runs = (
        "".join(
            f'<li class="card run"><div class="who"><b>{escape(run.employee)}</b><span class="cost">'
            f"{run.input_tokens:,} in, {run.output_tokens:,} out, {usd(run.cost)}, "
            f"{'finished' if run.succeeded else 'failed'}</span></div>"
            f'<ol class="steps">{"".join(f"<li>{escape(step)}</li>" for step in run.steps)}</ol>'
            f"{'<div class=refs>Read ' + chips(run.read) + '</div>' if run.read else ''}"
            f'<p class="last">{escape(plain(run.note)[:300])}</p></li>'
            for run in card.runs
        )
        or '<li class="none">No runs yet.</li>'
    )
    timeline = (
        "".join(f"<li>{escape(line)}</li>" for line in card.log) or '<li class="none">Not started.</li>'
    )
    closed = ""
    if card.pending:
        closed = ask(card, full=True)
    elif card.column == "Done" and card.sources:
        closed = answered(card)
    elif card.column == "Done" and replied:
        closed = receipt(card, replied)
    live = f"""<div class="wrap tight detail">
<p class="eyebrow">{escape(card.card_id)} &middot; {escape(card.column)}</p>
<h1 class="page">{escape(card.title)}</h1><div class="refs">{chips(card.issue.split(", "))}
{chips([card.customer], "blue") if card.customer else ""}</div>
<div class="stats"><div class="stat"><span>Employee</span><b>{escape(card.employee or "Unassigned")}</b></div>
<div class="stat"><span>Runs</span><b>{len(card.runs)}</b></div>
<div class="stat"><span>Cost</span><b>{usd(card.cost)}</b><em>failed runs included</em></div></div>
{closed}<h2 class="sec">Runs</h2><ol class="runs">{runs}</ol>
<h2 class="sec">Timeline</h2><ol class="timeline">{timeline}</ol></div>"""
    return page(card.title, live, "", workspace=workspace, signed_in=signed_in)


def team(board: Board, roster: tuple[Role, ...], *, workspace: str, signed_in: str = "") -> str:
    """Every AI employee: what it is hired for, what it must never do, and what its outcomes cost."""
    rows = ""
    for role in roster:
        # A crew card names every employee who worked it, and each is credited with its outcome.
        mine = [card for card in board.cards if role.name in card.employee.split(", ")]
        done = sum(card.column == "Done" for card in mine)
        spent = sum(card.cost for card in mine)
        rows += (
            f'<article class="card role"><h3>{escape(role.name)}</h3><dl>'
            f"<dt>Hired for</dt><dd>{escape(role.outcome)}</dd>"
            f"<dt>Watched by</dt><dd>{escape(role.metric)}</dd>"
            f'<dt>Must never</dt><dd class="never">{escape(role.never)}</dd></dl>'
            f'<div class="refs">{chips(list(role.tools))}</div><div class="who">'
            f"<span><b>{done}</b> done of {len(mine)}</span>"
            f'<span class="cost">{usd(spent / done) + " per outcome" if done else "no outcome yet"}</span>'
            "</div></article>"
        )
    live = f"""<div class="wrap tight"><div class="head"><div><h1 class="page">Team</h1>
<p>Each AI employee is hired for one outcome and has one thing it must never do. A test holds each line.</p>
</div></div><div class="roster">{rows}</div></div>"""
    return page("Team", live, "Team", workspace=workspace, signed_in=signed_in)


def refusal_html(card: Card) -> str:
    """The latest refusal on a card still open, so the room sees why a click did not count."""
    refused = [line for line in card.log if "refused:" in line]
    if not refused or card.column == "Done":
        return ""
    return f'<p class="refusal">{escape(refused[-1].split("refused:", 1)[1].strip())}</p>'


def app(
    state: Path, roster: tuple[Role, ...], people: People, access: Access | None = None
) -> web.Application:
    from workforce.issues import Issues
    from workforce.mission import NotAllowed, sign

    def read() -> View:
        board, issues = Board.open(state / "board.json"), Issues.open(state / "github.json")
        replies = {}
        for card in board.cards:
            found = issues.replied(card.github, card.card_id) if card.github else None
            if found:
                replies[card.card_id] = found
        return View(board, replies, issues)

    async def index(req: web.Request) -> web.Response:
        view = read()
        text = render(view.board, view.replies, workspace=WORKSPACE, signed_in=signed_in_html(req[PERSON]))
        return web.Response(text=text, content_type="text/html")

    async def one(req: web.Request) -> web.Response:
        view = read()
        try:
            card = view.board.card(req.match_info["id"])
        except StopIteration:
            raise web.HTTPNotFound() from None
        text = detail(
            card, view.replies.get(card.card_id), workspace=WORKSPACE, signed_in=signed_in_html(req[PERSON])
        )
        return web.Response(
            text=text.replace('<h2 class="sec">Runs', refusal_html(card) + '<h2 class="sec">Runs', 1),
            content_type="text/html",
        )

    async def roster_page(req: web.Request) -> web.Response:
        text = team(read()[0], roster, workspace=WORKSPACE, signed_in=signed_in_html(req[PERSON]))
        return web.Response(text=text, content_type="text/html")

    async def decide(req: web.Request) -> web.Response:
        """The signed-in person answers; a refusal is written to the card, so the page shows it."""
        view = read()
        try:
            card = view.board.card(req.match_info["id"])
        except StopIteration:
            raise web.HTTPNotFound() from None
        if not card.pending:
            raise web.HTTPConflict(text="nothing is waiting on this card")
        form = await req.post()
        with contextlib.suppress(NotAllowed):
            sign(view.board, card, view.issues, req[PERSON], form.get("yes") == "1", access)
        raise web.HTTPSeeOther(f"/cards/{card.card_id}")

    web_app = web.Application()
    require_sign_in(web_app, people, WORKSPACE)
    web_app.router.add_get("/", index)
    web_app.router.add_get("/team", roster_page)
    web_app.router.add_get("/cards/{id}", one)
    web_app.router.add_post("/cards/{id}/decide", decide)
    web_app.router.add_static("/static", WEB)
    web_app.router.add_static("/fonts", WEB / "fonts")
    return web_app


def serve(state: Path, roster: tuple[Role, ...], port: int = 8080) -> None:
    web.run_app(
        app(state, roster, people_or_exit(PEOPLE_FILE), Access.from_env()), host="127.0.0.1", port=port
    )
