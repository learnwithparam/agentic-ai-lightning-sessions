"""Mission Control's board for the crew: sign-in, the ticket, the gateway's call log, and Approve.

The card page reads the ticket and the messages from the travel system, and every tool call, refused ones
included, from the log the gateways wrote. A fare over the cap waits on the card until the signed-in
manager approves it; the person is read from the session cookie, never from the form.
"""

from __future__ import annotations

import asyncio
import html
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiohttp import web

from crew.gateway import read_call_log, write_call_log_row
from crew.travel import Refused, TicketStatus, Travel
from workforce.board import CHECK, WEB, Board, Card, Role, chips, detail, render, team
from workforce.people import COOKIE_NAME, People, Person

escape = html.escape
ROOT = Path(__file__).resolve().parent.parent
CSS = ROOT / "web" / "crew.css"
PEOPLE_FILE = ROOT / "fixtures" / "people.json"
WORKSPACE = "KaaS Travel"
PUBLIC_PATHS = ("/login", "/crew.css")
PUBLIC_FOLDERS = ("/static/", "/fonts/")

PERSON = web.RequestKey("person", Person)
TASKS = web.AppKey("tasks", set)
Resume = Callable[[Board, Card], Awaitable[Card]]


def ticket_html(ticket: dict, trip: dict, told: list[str]) -> str:
    is_ticketed = ticket["status"] == TicketStatus.TICKETED
    heading = f"{CHECK}Ticketed" if is_ticketed else f"Waiting for {trip['manager']}"
    rows = [
        ("Flight", f"{ticket['flight']}, {ticket['cabin']}"),
        ("Departs", ticket["departs"]),
        ("Lands", ticket["lands"]),
        ("Meeting", trip["meeting"]),
        ("Fare", f"£{ticket['fare']}"),
        ("Fare cap", f"£{trip['fare_cap']}"),
        ("Booked by", ticket["booked_by"]),
        ("Approved by", approval_text(ticket)),
        ("Told", ", ".join(told) or "nobody yet"),
    ]
    cells = "".join(
        f"<dt>{escape(name)}</dt><dd>{escape(value)}</dd>" for name, value in rows
    )
    open_class = "" if is_ticketed else " open"
    return f'<div class="receipt trip{open_class}"><h4>{heading}</h4><dl>{cells}</dl></div>'


def approval_text(ticket: dict) -> str:
    if ticket["approved_by"]:
        return f"{ticket['approved_by']}, signed in at {ticket['approved_at']}"
    if ticket["status"] == TicketStatus.TICKETED:
        return "not needed"
    return "nobody yet"


def call_row_html(call: dict) -> str:
    verdict = (
        chips(["allowed"], "green") if call["allowed"] else chips(["refused"], "red")
    )
    first_line_said = call["said"].splitlines()[0] if call["said"] else ""
    return (
        f'<tr class="{"" if call["allowed"] else "no"}"><td><b>{escape(call["agent"])}</b></td>'
        f"<td><code>{escape(call['tool'])}</code></td>"
        f'<td class="args">{escape(", ".join(call["args"].values()))}</td>'
        f'<td>{verdict}</td><td class="said">{escape(first_line_said)}</td></tr>'
    )


def calls_html(calls: list[dict]) -> str:
    body = "".join(call_row_html(call) for call in calls)
    body = body or '<tr><td colspan="5">No calls yet.</td></tr>'
    return (
        '<h2 class="sec" id="calls">Gateway calls</h2><p class="sub">Every tool call each employee made '
        "through its gateway, in order. A refused call changed nothing in the travel system.</p>"
        '<table class="tbl calls"><thead><tr><th>Who</th><th>Tool</th><th>With</th><th>Gateway</th>'
        f"<th>Said</th></tr></thead><tbody>{body}</tbody></table>"
    )


def approval_form_html(card: Card, ticket: dict, trip: dict) -> str:
    return (
        f'<form method="post" action="/cards/{escape(card.id)}/approve" class="decide">'
        f"<p>£{ticket['fare']} on {escape(ticket['flight'])} is over the £{trip['fare_cap']} cap. "
        f"Only {escape(trip['manager'])} can approve it.</p>"
        '<button name="decision" value="approve">Approve</button> '
        '<button name="decision" value="decline" class="ghost">Decline</button></form>'
    )


def signed_in_html(person: Person) -> str:
    return (
        f'<span class="signed-in">Signed in as <b>{escape(person.name)}</b>, {escape(person.role)}</span>'
        '<form method="post" action="/logout" class="sign-out"><button>Sign out</button></form>'
    )


def with_css(page: str) -> str:
    return page.replace("</head>", '<link rel="stylesheet" href="/crew.css"></head>', 1)


def card_page(state: Path, card_id: str, person: Person | None = None) -> str:
    board = Board.open(state / "board.json")
    card = board.card(card_id)
    travel = Travel(state)
    ticket = travel.ticket(card.id)
    trip = travel.trip() if ticket else {}
    page = detail(card, None, WORKSPACE, signed_in_html(person) if person else "")
    waiting = (
        ticket is not None and ticket["status"] == TicketStatus.WAITING_FOR_MANAGER
    )
    extra = ticket_html(ticket, trip, travel.told(card.id)) if ticket else ""
    extra += approval_form_html(card, ticket, trip) if waiting and person else ""
    extra += calls_html(read_call_log(state, card.id))
    # The crew's evidence goes above the runs, so the card reads as outcome first, then how it got there.
    page = page.replace(
        '<h2 class="sec">Runs</h2>', extra + '<h2 class="sec">Runs</h2>', 1
    )
    return with_css(page)


def login_page(people: People, problem: str = "") -> str:
    options = "".join(f"<option>{escape(name)}</option>" for name in people.names())
    complaint = f'<p class="block">{escape(problem)}</p>' if problem else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sign in</title>
<link rel="stylesheet" href="/static/base.css"><link rel="stylesheet" href="/static/board.css">
<link rel="stylesheet" href="/crew.css"></head><body><main class="login">
<h1 class="page">Sign in to {WORKSPACE}</h1>{complaint}
<form method="post" action="/login"><label>Who are you<select name="name">{options}</select></label>
<label>Passphrase<input type="password" name="passphrase" autocomplete="current-password"></label>
<button>Sign in</button></form></main></body></html>"""


def html_response(text: str) -> web.Response:
    return web.Response(text=text, content_type="text/html")


class BoardPages:
    """The board's routes. Who is acting always comes from the signed cookie, never from a form."""

    def __init__(
        self,
        state: Path,
        roster: tuple[Role, ...],
        people: People,
        resume: Resume | None,
    ) -> None:
        self.state = state
        self.roster = roster
        self.people = people
        self.resume = resume

    def open_board(self) -> Board:
        return Board.open(self.state / "board.json")

    @web.middleware
    async def require_sign_in(self, request: web.Request, handler):
        person = self.people.person_in_cookie(request.cookies.get(COOKIE_NAME))
        request[PERSON] = person
        is_public = request.path in PUBLIC_PATHS or request.path.startswith(
            PUBLIC_FOLDERS
        )
        if person is None and not is_public:
            raise web.HTTPSeeOther("/login")
        return await handler(request)

    async def login_form(self, _: web.Request) -> web.Response:
        return html_response(login_page(self.people))

    async def login(self, request: web.Request) -> web.Response:
        form = await request.post()
        name, passphrase = str(form.get("name", "")), str(form.get("passphrase", ""))
        person = self.people.sign_in(name, passphrase)
        if person is None:
            problem = "That name and passphrase do not match."
            return web.Response(
                text=login_page(self.people, problem),
                content_type="text/html",
                status=401,
            )
        response = web.HTTPSeeOther("/")
        cookie = self.people.sign_cookie(person)
        response.set_cookie(COOKIE_NAME, cookie, httponly=True, samesite="Strict")
        raise response

    async def logout(self, _: web.Request) -> web.Response:
        response = web.HTTPSeeOther("/login")
        response.del_cookie(COOKIE_NAME)
        raise response

    async def board_page(self, request: web.Request) -> web.Response:
        signed_in = signed_in_html(request[PERSON])
        return html_response(
            with_css(render(self.open_board(), None, WORKSPACE, signed_in))
        )

    async def team_page(self, request: web.Request) -> web.Response:
        signed_in = signed_in_html(request[PERSON])
        return html_response(
            with_css(team(self.open_board(), self.roster, WORKSPACE, signed_in))
        )

    async def card(self, request: web.Request) -> web.Response:
        try:
            page = card_page(self.state, request.match_info["id"], request[PERSON])
        except StopIteration:
            raise web.HTTPNotFound() from None
        return html_response(page)

    async def decide(self, request: web.Request) -> web.Response:
        """Approve or decline the held fare as the signed-in person; the travel system rules."""
        person: Person = request[PERSON]
        form = await request.post()
        board = self.open_board()
        try:
            card = board.card(request.match_info["id"])
        except StopIteration:
            raise web.HTTPNotFound() from None
        travel = Travel(self.state)
        ticket = travel.ticket(card.id)
        card_url = f"/cards/{card.id}"
        if ticket is None:
            raise web.HTTPSeeOther(card_url)
        approving = form.get("decision") == "approve"
        call_log = self.state / "calls.jsonl"
        arguments = {"ticket_id": ticket["id"]}
        try:
            travel.approve(ticket["id"], approved_by=person.name, approve=approving)
        except Refused as refusal:
            write_call_log_row(
                call_log,
                card.id,
                person.name,
                "approve_fare",
                arguments,
                False,
                str(refusal),
            )
            raise web.HTTPSeeOther(card_url) from None
        verb = "approved" if approving else "declined"
        said = f"{verb} {ticket['id']}"
        write_call_log_row(
            call_log, card.id, person.name, "approve_fare", arguments, True, said
        )
        self.carry_on(request.app, board, card, approving)
        raise web.HTTPSeeOther(card_url)

    def carry_on(
        self, web_app: web.Application, board: Board, card: Card, approving: bool
    ):
        """After an approval only the messenger runs; after a decline the card just settles."""
        if approving and self.resume is not None:
            task = asyncio.create_task(self.resume(board, card))
            web_app[TASKS].add(task)
            task.add_done_callback(web_app[TASKS].discard)
            return
        from crew.team import settle

        settle(board, card, Travel(self.state))

    async def css(self, _: web.Request) -> web.FileResponse:
        return web.FileResponse(CSS)


def app(
    state: Path, roster: tuple[Role, ...], people: People, resume: Resume | None = None
) -> web.Application:
    pages = BoardPages(state, roster, people, resume)
    web_app = web.Application(middlewares=[pages.require_sign_in])
    web_app[TASKS] = set()
    web_app.router.add_get("/login", pages.login_form)
    web_app.router.add_post("/login", pages.login)
    web_app.router.add_post("/logout", pages.logout)
    web_app.router.add_get("/", pages.board_page)
    web_app.router.add_get("/team", pages.team_page)
    web_app.router.add_get("/cards/{id}", pages.card)
    web_app.router.add_post("/cards/{id}/approve", pages.decide)
    web_app.router.add_get("/crew.css", pages.css)
    web_app.router.add_static("/static", WEB)
    web_app.router.add_static("/fonts", WEB / "fonts")
    return web_app


def serve(state: Path, roster: tuple[Role, ...], port: int) -> None:
    from crew.team import resume_after_approval

    secret = os.environ.get("BOARD_SECRET", "")
    people = People(PEOPLE_FILE, secret)
    if not secret or not people.names():
        sys.exit(
            "Set BOARD_SECRET and a BOARD_PASSPHRASE_* for each person in fixtures/people.json."
        )

    async def resume(board: Board, card: Card) -> Card:
        return await resume_after_approval(board, card, live=True)

    web.run_app(app(state, roster, people, resume), host="127.0.0.1", port=port)
