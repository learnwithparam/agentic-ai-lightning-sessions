"""Mission Control's board, with what screening adds to a card: the shortlist, the flagged CV, the audit log.

The card page reads each from the hiring system. A flagged CV is drawn as a recruiter sees it, with the
text only a parser gets shown in place. Reject and Clear are offered to every signed-in person; the hiring
system decides who may use them, and the person comes from the session cookie, never from the form.
"""

from __future__ import annotations

import html
from pathlib import Path

from aiohttp import web

from hiring.ats import ATS, Refused
from hiring.guard import scan
from hiring.screener import settle
from workforce.board import CHECK, WEB, Board, Role, chips, detail, render, team
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
CSS = ROOT / "web" / "hiring.css"
PEOPLE_FILE = ROOT / "fixtures" / "people.json"
WORKSPACE = "KaaS Retail"
PUBLIC_PATHS = ("/hiring.css",)
REFUSED = ("blocked", "review refused")


def shortlist_html(ats: ATS, shortlist: dict, waiting: bool) -> str:
    head = f"Held until {ats.recruiter} decides" if waiting else f"{CHECK}Sent to {shortlist['manager']}"
    ranked = "".join(f"<li>{escape(ats.candidate(item)['name'])}</li>" for item in shortlist["candidates"])
    role = ats.role(shortlist["req"])["title"]
    rows = [("Role", f"{shortlist['req']}, {role}"), ("Submitted by", shortlist["by"])]
    cells = "".join(f"<dt>{escape(label)}</dt><dd>{escape(value)}</dd>" for label, value in rows)
    open_class = " open" if waiting else ""
    return (
        f'<div class="receipt shortlist{open_class}"><h4>{head}</h4>'
        f'<ol class="ranked">{ranked}</ol><dl>{cells}</dl></div>'
    )


def flagged_html(ats: ATS, card_id: str, candidate_id: str, flag: dict) -> str:
    cv_scan, name = scan(ats.cv_html(candidate_id)), ats.candidate(candidate_id)["name"]
    lines = "".join(
        f'<p class="hid"><mark>{escape(line)}</mark></p>'
        if line in flag["hidden"]
        else f"<p>{escape(line)}</p>"
        for line in cv_scan.text.splitlines()
    )
    if flag["reviewed_by"]:
        verdict = "rejected the application" if flag.get("rejected") else "cleared the CV"
        foot = f'<p class="done">{CHECK}{escape(flag["reviewed_by"])} {verdict}.</p>'
    else:
        foot = (
            f'<form method="post" action="/cards/{escape(card_id)}/review" class="decide">'
            f'<input type="hidden" name="candidate" value="{escape(candidate_id)}">'
            '<button class="btn dark" name="decision" value="reject">Reject the application</button>'
            '<button class="btn" name="decision" value="clear">Clear the CV</button></form>'
        )
    return (
        f'<h2 class="sec" id="flagged">{escape(name)}\'s CV</h2><p class="sub">What a recruiter sees on the '
        "page. The highlighted line is in the file but drawn so a person cannot read it; the screener was "
        f'never given this CV.</p><div class="cv">{lines}</div>{foot}'
    )


def audit_html(rows: list[dict], ats: ATS) -> str:
    def chip(check: str) -> str:
        if check == "recruiter":
            return chips(["recruiter"], "blue")
        return chips([check if check in REFUSED else "blocked"], "red")

    def subject(row: dict) -> str:
        return ats.candidate(row["candidate"])["name"] if row["candidate"] else "the request"

    body = (
        "".join(
            f"<tr><td><b>{escape(row['check'])}</b></td>"
            f"<td>{escape(subject(row))}</td>"
            f"<td>{chip(row['check'])}</td>"
            f'<td class="said">{escape(row["what"])}</td></tr>'
            for row in rows
        )
        or '<tr><td colspan="4">Nothing was blocked.</td></tr>'
    )
    return (
        '<h2 class="sec" id="audit">Guardrail log</h2><p class="sub">Every block on this card, as the hiring '
        "system recorded it.</p>"
        '<table class="tbl audit"><thead><tr><th>Check</th><th>About</th><th>Result</th>'
        f"<th>What happened</th></tr></thead><tbody>{body}</tbody></table>"
    )


def card_page(state: Path, card_id: str, person: Person | None = None) -> str:
    card = Board.open(state / "board.json").card(card_id)
    ats = ATS.open(state)
    flags, shortlist = ats.flags(card.id), ats.shortlist(card.id)
    waiting = any(not flag["reviewed_by"] for flag in flags.values())
    extra = shortlist_html(ats, shortlist, waiting) if shortlist else ""
    extra += "".join(flagged_html(ats, card.id, item, flag) for item, flag in flags.items())
    extra += audit_html(ats.log(card.id), ats)
    # The hiring system's evidence goes above the runs, so the card reads as outcome first.
    page = detail(
        card, None, workspace=WORKSPACE, signed_in=signed_in_html(person) if person else ""
    ).replace('<h2 class="sec">Runs</h2>', extra + '<h2 class="sec">Runs</h2>', 1)
    return with_css(page)


def with_css(page: str) -> str:
    return page.replace("</head>", '<link rel="stylesheet" href="/hiring.css"></head>', 1)


class BoardPages:
    """The board's routes. Who is acting always comes from the signed cookie, never from a form."""

    def __init__(self, state: Path, roster: tuple[Role, ...]) -> None:
        self.state = state
        self.roster = roster

    def open_board(self) -> Board:
        return Board.open(self.state / "board.json")

    async def board_page(self, request: web.Request) -> web.Response:
        signed_in = signed_in_html(request[PERSON])
        return html_response(with_css(render(self.open_board(), workspace=WORKSPACE, signed_in=signed_in)))

    async def team_page(self, request: web.Request) -> web.Response:
        signed_in = signed_in_html(request[PERSON])
        page = team(self.open_board(), self.roster, workspace=WORKSPACE, signed_in=signed_in)
        return html_response(with_css(page))

    async def card(self, request: web.Request) -> web.Response:
        try:
            page = card_page(self.state, request.match_info["id"], request[PERSON])
        except StopIteration:
            raise web.HTTPNotFound() from None
        return html_response(page)

    async def review(self, request: web.Request) -> web.Response:
        """Reject or clear a flagged CV as the signed-in person; the hiring system rules."""
        person: Person = request[PERSON]
        form = await request.post()
        card_id = request.match_info["id"]
        board, ats = self.open_board(), ATS.open(self.state)
        try:
            card = board.card(card_id)
        except StopIteration:
            raise web.HTTPNotFound() from None
        try:
            ats.review(card_id, str(form.get("candidate", "")), person.name, form.get("decision") == "reject")
        except Refused:
            # The refusal is already in the audit log, so the card page shows it.
            raise web.HTTPSeeOther(f"/cards/{card_id}") from None
        settle(board, card, ats)
        raise web.HTTPSeeOther(f"/cards/{card_id}")

    async def css(self, _: web.Request) -> web.FileResponse:
        return web.FileResponse(CSS)


def app(state: Path, roster: tuple[Role, ...], people) -> web.Application:
    pages = BoardPages(state, roster)
    web_app = web.Application()
    require_sign_in(web_app, people, WORKSPACE, ("/hiring.css",), PUBLIC_PATHS)
    web_app.router.add_get("/", pages.board_page)
    web_app.router.add_get("/team", pages.team_page)
    web_app.router.add_get("/cards/{id}", pages.card)
    web_app.router.add_post("/cards/{id}/review", pages.review)
    web_app.router.add_get("/hiring.css", pages.css)
    web_app.router.add_static("/static", WEB)
    web_app.router.add_static("/fonts", WEB / "fonts")
    return web_app


def serve(state: Path, roster: tuple[Role, ...], port: int) -> None:
    web.run_app(app(state, roster, people_or_exit(PEOPLE_FILE)), host="127.0.0.1", port=port)
