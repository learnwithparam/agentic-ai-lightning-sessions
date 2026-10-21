"""Mission Control's board, with what a crew adds to a card: the ticket and the gateway's call log.

The card page reads both from where they are true. The ticket and the messages come from the travel
system; every tool call, refused ones included, comes from the log the gateways wrote.
"""

from __future__ import annotations

import html
from pathlib import Path

from aiohttp import web

from crew.gateway import calls
from crew.travel import Travel
from workforce.board import CHECK, WEB, Board, Role, chips, detail, render, team

E = html.escape
CSS = Path(__file__).resolve().parent.parent / "web" / "crew.css"


def ticket_html(ticket: dict, trip: dict, told: list[str]) -> str:
    ready = ticket["status"] == "ticketed"
    head = f"{CHECK}Ticketed" if ready else f"Waiting for {trip['manager']}"
    rows = [("Flight", f"{ticket['flight']}, {ticket['cabin']}"), ("Departs", ticket["departs"])]
    rows += [("Lands", ticket["lands"]), ("Meeting", trip["meeting"]), ("Fare", f"£{ticket['fare']}")]
    rows += [("Fare cap", f"£{trip['fare_cap']}"), ("Booked by", ticket["booked_by"])]
    rows += [("Approved by", (ticket["approved_by"] or "not needed") if ready else "nobody yet")]
    rows += [("Told", ", ".join(told) or "nobody yet")]
    cells = "".join(f"<dt>{E(k)}</dt><dd>{E(v)}</dd>" for k, v in rows)
    return f'<div class="receipt trip{"" if ready else " open"}"><h4>{head}</h4><dl>{cells}</dl></div>'


def calls_html(rows: list[dict]) -> str:
    body = (
        "".join(
            f'<tr class="{"" if c["allowed"] else "no"}"><td><b>{E(c["agent"])}</b></td>'
            f'<td><code>{E(c["tool"])}</code></td><td class="args">{E(", ".join(c["args"].values()))}</td>'
            f"<td>{chips(['allowed'], 'green') if c['allowed'] else chips(['refused'], 'red')}</td>"
            f'<td class="said">{E(c["said"].splitlines()[0] if c["said"] else "")}</td></tr>'
            for c in rows
        )
        or '<tr><td colspan="5">No calls yet.</td></tr>'
    )
    return (
        '<h2 class="sec" id="calls">Gateway calls</h2><p class="sub">Every tool call each employee made '
        "through its gateway, in order. A refused call changed nothing in the travel system.</p>"
        '<table class="tbl calls"><thead><tr><th>Employee</th><th>Tool</th><th>With</th><th>Gateway</th>'
        f"<th>Said</th></tr></thead><tbody>{body}</tbody></table>"
    )


def card_page(state: Path, card_id: str) -> str:
    board = Board.open(state / "board.json")
    c = board.card(card_id)
    travel = Travel(state)
    ticket = travel.ticket(c.id)
    page = detail(c, None)
    extra = (ticket_html(ticket, travel.trip(), travel.told(c.id)) if ticket else "") + calls_html(
        calls(state, c.id)
    )
    # The crew's evidence goes above the runs, so the card reads as outcome first, then how it got there.
    page = page.replace('<h2 class="sec">Runs</h2>', extra + '<h2 class="sec">Runs</h2>', 1)
    return with_css(page)


def with_css(page: str) -> str:
    return page.replace("</head>", '<link rel="stylesheet" href="/crew.css"></head>', 1)


def app(state: Path, roster: tuple[Role, ...]) -> web.Application:
    async def index(_: web.Request) -> web.Response:
        return web.Response(text=with_css(render(Board.open(state / "board.json"))), content_type="text/html")

    async def one(req: web.Request) -> web.Response:
        try:
            return web.Response(text=card_page(state, req.match_info["id"]), content_type="text/html")
        except StopIteration:
            raise web.HTTPNotFound() from None

    async def roster_page(_: web.Request) -> web.Response:
        return web.Response(
            text=with_css(team(Board.open(state / "board.json"), roster)), content_type="text/html"
        )

    async def css(_: web.Request) -> web.FileResponse:
        return web.FileResponse(CSS)

    a = web.Application()
    a.router.add_get("/", index)
    a.router.add_get("/team", roster_page)
    a.router.add_get("/cards/{id}", one)
    a.router.add_get("/crew.css", css)
    a.router.add_static("/static", WEB)
    a.router.add_static("/fonts", WEB / "fonts")
    return a


def serve(state: Path, roster: tuple[Role, ...], port: int) -> None:
    web.run_app(app(state, roster), host="127.0.0.1", port=port)
