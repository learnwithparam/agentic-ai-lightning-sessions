"""Mission Control's board, with what cost adds: each pass of the ladder, every read, a budget to raise.

The front page is the newest live pass with the stored ladder above it, one row per pass; a row opens that
pass's own board. A card page adds the claim file the claims system holds, its ledger, one row per document,
and the budget decisions. A stopped claim on a live pass offers Approve and Decline to every signed-in
person; the claims system decides who may use them, and the person comes from the session cookie.
"""

from __future__ import annotations

import asyncio
import html
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web

from claims.reader import LADDER, STATE, passes, resume_after_raise, settle
from claims.system import Claims, Refused
from workforce.board import CHECK, WEB, Board, Role, detail, render, team, usd
from workforce.people import (
    PERSON,
    Person,
    html_response,
    people_or_exit,
    require_sign_in,
    signed_in_html,
)

escape = html.escape
ROOT = Path(__file__).resolve().parent.parent
CSS = ROOT / "web" / "claims.css"
PEOPLE_FILE = ROOT / "fixtures" / "people.json"
WORKSPACE = "KaaS Insurance"
PUBLIC_PATHS = ("/claims.css",)
TASKS = web.AppKey("tasks", set)


@dataclass(frozen=True)
class Desk:
    """Where the passes are kept: the stored ladder, and the live passes run before or during the call."""

    stored: Path
    live: Path

    def is_live(self, number: int) -> bool:
        return (self.live / f"pass-{number}" / "board.json").exists()

    def folder(self, number: int) -> Path:
        return (self.live if self.is_live(number) else self.stored) / f"pass-{number}"

    def exists(self, number: int) -> bool:
        return (self.folder(number) / "board.json").exists()

    def newest(self) -> int:
        live = [number for number in range(1, len(LADDER) + 1) if self.is_live(number)]
        if live:
            return max(live, key=lambda number: (self.live / f"pass-{number}" / "board.json").stat().st_mtime)
        return max((result.number for result in passes(self.stored)), default=len(LADDER))


def ladder_html(desk: Desk, here: int) -> str:
    results = passes(desk.stored)
    if not results:
        return ""
    rows = "".join(
        f"<tr{' class=here' if result.number == here else ''}>"
        f'<td><a href="/pass/{result.number}">{escape(result.name)}</a></td>'
        f"<td class=n>{result.filed} of {len(result.board.cards)}</td><td class=n>{result.fresh:,}</td>"
        f"<td class=n>{result.cached:,}</td><td class=n>{result.output:,}</td>"
        f'<td class="n lead">{"none filed" if result.per_claim is None else usd(result.per_claim)}</td>'
        f"<td class=n>{result.per_thousand}</td></tr>"
        for result in results
    )
    return (
        '<h2 class="sec" id="ladder">Before and after</h2><p class="sub">The same three claims read four '
        "times before this call; nobody raised a budget. Each pass adds one lever, and the ledger prices "
        "every read from the provider's own token counts.</p>"
        '<table class="tbl ladder"><thead><tr><th>Pass</th><th class=n>Filed</th>'
        "<th class=n>Fresh input</th><th class=n>Cached input</th><th class=n>Output</th>"
        "<th class=n>Cost per claim filed</th><th class=n>Per 1,000 claims</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def on_pass(page: str, number: int) -> str:
    return page.replace('href="/cards/', f'href="/pass/{number}/cards/')


def with_css(page: str) -> str:
    return page.replace("</head>", '<link rel="stylesheet" href="/claims.css"></head>', 1)


def filed_html(claims: Claims, claim_id: str) -> str:
    filed = claims.filed(claim_id)
    if filed is None:
        return ""
    total = filed["total"] or "none until an assessor prices it"
    rows = [("Parts", ", ".join(filed["parts"])), ("Vehicles", str(filed["vehicles"])), ("Total", total)]
    cells = "".join(f"<dt>{escape(label)}</dt><dd>{escape(value)}</dd>" for label, value in rows)
    return (
        f'<div class="receipt filed"><h4>{CHECK}Filed in the claims system</h4><dl>{cells}</dl>'
        f'<p class="last">{escape(filed["note"])}</p></div>'
    )


def decide_html(claims: Claims, claim_id: str, card_id: str, number: int) -> str:
    stop = claims.stopped(claim_id)
    if stop is None:
        return ""
    total = len(claims.claim(claim_id)["documents"])
    sentence = (
        f"{claim_id} has spent {usd(stop['spent'])} of its {usd(claims.budget_for(claim_id))} budget, "
        f"with {stop['read']} of {total} documents read. Raising it to {usd(claims.raised_budget)} lets it "
        f"read {stop['before']} and the rest. {claims.adjuster} decides."
    )
    return (
        '<div class="receipt open"><h4>Raise the budget?</h4>'
        f"<p>{escape(sentence)}</p>"
        f'<form method="post" action="/pass/{number}/cards/{escape(card_id)}/budget" class="decide">'
        '<button class="btn dark" name="decision" value="approve">Approve</button>'
        '<button class="btn" name="decision" value="decline">Decline</button></form></div>'
    )


def decisions_html(claims: Claims, claim_id: str) -> str:
    rows = claims.decisions(claim_id)
    if not rows:
        return ""
    body = "".join(
        f"<tr{' class=no' if row['decision'] == 'refused' else ''}><td><b>{escape(row['by'])}</b></td>"
        f'<td>{escape(row["decision"])}</td><td class="said">{escape(row["said"])}</td></tr>'
        for row in rows
    )
    return (
        '<h2 class="sec" id="decisions">Budget decisions</h2><p class="sub">Every attempt to change this '
        "claim's budget, as the claims system recorded it.</p>"
        '<table class="tbl decisions"><thead><tr><th>Who</th><th>Decision</th><th>Said</th></tr></thead>'
        f"<tbody>{body}</tbody></table>"
    )


def ledger_html(claims: Claims, claim_id: str) -> str:
    rows = claims.ledger(claim_id)
    raised = [row for row in claims.decisions(claim_id) if row["decision"] == "raised"]
    body = ""
    for index in range(len(rows) + 1):
        for decision in raised:
            if decision["after_read"] == index:
                body += (
                    f'<tr class="raised"><td colspan="5">{escape(decision["by"])} raised the budget to '
                    f"{usd(claims.budget_for(claim_id))} at {escape(decision['at'])}</td></tr>"
                )
        if index < len(rows):
            row = rows[index]
            body += (
                f"<tr><td>{escape(row['doc'])}</td><td class=n>{row['input']:,}</td>"
                f"<td class=n>{row['cached']:,}</td><td class=n>{row['output']:,}</td>"
                f"<td class=n>{usd(row['cost'])}</td></tr>"
            )
    stop = claims.stopped(claim_id)
    if stop:
        body += (
            f'<tr class="stop"><td>{escape(stop["before"])}</td><td colspan="4">Not read: one more read '
            f"would cross the {usd(claims.budget_for(claim_id))} budget. "
            f"{escape(claims.adjuster)} decides.</td></tr>"
        )
    return (
        '<h2 class="sec" id="ledger">Ledger</h2><p class="sub">Every document on this claim and what reading '
        f"it cost, against a budget of {usd(claims.budget_for(claim_id))}.</p>"
        '<table class="tbl ledger"><thead><tr><th>Document</th><th class=n>Tokens in</th>'
        "<th class=n>Cached</th><th class=n>Tokens out</th><th class=n>Cost</th></tr></thead>"
        f'<tbody>{body}</tbody><tfoot><tr><td>Spent</td><td colspan="3"></td>'
        f"<td class=n>{usd(claims.spent(claim_id))}</td></tr></tfoot>"
        "</table>"
    )


class BoardPages:
    """The board's routes. Who is acting always comes from the signed cookie, never from a form."""

    def __init__(self, desk: Desk, roster: tuple[Role, ...], model=None) -> None:
        self.desk = desk
        self.roster = roster
        self.model = model

    def number(self, request: web.Request) -> int:
        number = int(request.match_info.get("n", self.desk.newest()))
        if not self.desk.exists(number):
            raise web.HTTPNotFound()
        return number

    def board_of(self, number: int) -> Board:
        return Board.open(self.desk.folder(number) / "board.json")

    def card_page(self, number: int, card_id: str, person: Person) -> str:
        card = self.board_of(number).card(card_id)
        claims = Claims.open(self.desk.folder(number))
        extra = filed_html(claims, card.issue)
        if self.desk.is_live(number):
            extra += decide_html(claims, card.issue, card.card_id, number)
        extra += decisions_html(claims, card.issue) + ledger_html(claims, card.issue)
        page = detail(card, None, workspace=WORKSPACE, signed_in=signed_in_html(person)).replace(
            '<h2 class="sec">Runs</h2>', extra + '<h2 class="sec">Runs</h2>', 1
        )
        return with_css(on_pass(page, number))

    async def board_page(self, request: web.Request) -> web.Response:
        number = self.number(request)
        page = render(self.board_of(number), workspace=WORKSPACE, signed_in=signed_in_html(request[PERSON]))
        page = page.replace('<div class="lanes">', ladder_html(self.desk, number) + '<div class="lanes">', 1)
        return html_response(with_css(on_pass(page, number)))

    async def team_page(self, request: web.Request) -> web.Response:
        board = self.board_of(self.desk.newest())
        page = team(board, self.roster, workspace=WORKSPACE, signed_in=signed_in_html(request[PERSON]))
        return html_response(with_css(page))

    async def card(self, request: web.Request) -> web.Response:
        try:
            page = self.card_page(self.number(request), request.match_info["id"], request[PERSON])
        except StopIteration:
            raise web.HTTPNotFound() from None
        return html_response(page)

    async def budget(self, request: web.Request) -> web.Response:
        """Approve or decline a stopped claim's budget as the signed-in person; the claims system rules."""
        person: Person = request[PERSON]
        number = self.number(request)
        form = await request.post()
        card_id = request.match_info["id"]
        folder = self.desk.folder(number)
        board = self.board_of(number)
        try:
            card = board.card(card_id)
        except StopIteration:
            raise web.HTTPNotFound() from None
        claims = Claims.open(folder)
        approve = form.get("decision") == "approve"
        where = f"/pass/{number}/cards/{card_id}"
        try:
            claims.decide_budget(card.issue, person.name, approve)
        except Refused:
            # The refusal is already in the budget decisions, so the card page shows it.
            raise web.HTTPSeeOther(where) from None
        if approve:
            task = asyncio.create_task(resume_after_raise(board, card, LADDER[number - 1][1], self.model))
            request.app[TASKS].add(task)
            task.add_done_callback(request.app[TASKS].discard)
        else:
            settle(board, card, claims)
        raise web.HTTPSeeOther(where)

    async def css(self, _: web.Request) -> web.FileResponse:
        return web.FileResponse(CSS)


def app(desk: Desk, roster: tuple[Role, ...], people, model=None) -> web.Application:
    pages = BoardPages(desk, roster, model)
    web_app = web.Application()
    web_app[TASKS] = set()
    require_sign_in(web_app, people, WORKSPACE, ("/claims.css",), PUBLIC_PATHS)
    web_app.router.add_get("/", pages.board_page)
    web_app.router.add_get("/pass/{n:[1-9]}", pages.board_page)
    web_app.router.add_get("/cards/{id}", pages.card)
    web_app.router.add_get("/pass/{n:[1-9]}/cards/{id}", pages.card)
    web_app.router.add_post("/pass/{n:[1-9]}/cards/{id}/budget", pages.budget)
    web_app.router.add_get("/team", pages.team_page)
    web_app.router.add_get("/claims.css", pages.css)
    web_app.router.add_static("/static", WEB)
    web_app.router.add_static("/fonts", WEB / "fonts")
    return web_app


def serve(desk: Desk | None = None, roster: tuple[Role, ...] = (), port: int = 8092) -> None:
    web.run_app(
        app(desk or Desk(STATE, STATE.parent / ".claims-live"), roster, people_or_exit(PEOPLE_FILE)),
        host="127.0.0.1",
        port=port,
    )
