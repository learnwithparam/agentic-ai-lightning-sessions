"""Mission Control's board, with what production adds: each employee's spend against the shift's budget.

Above the lanes, what each AI employee spent, killed runs included, and the budget alert if it went. A
card in Done shows the repair order the maintenance system holds, and every card links to its runs in
Langfuse, the card id being the session.
"""

from __future__ import annotations

import os
from pathlib import Path

from aiohttp import web

from upkeep import budget
from upkeep.crew import PLANNER, TRIAGE
from upkeep.plant import MACHINES, PROCEDURES, Cmms
from workforce.board import WEB, Board, Card, E, Role, detail, render, team, usd

CSS = Path(__file__).resolve().parent.parent / "web" / "upkeep.css"
WORKSPACE = "KaaS Dairy"
TEAM = (
    Role(
        TRIAGE,
        "Every alert joined to its open order, dismissed with the reading that cleared it, or handed on",
        "Alerts closed per shift, and its share of the shift's spend",
        "Open a second order for a fault that already has one",
        ("read_sensor", "attach_to_order", "dismiss_alert"),
    ),
    Role(
        PLANNER,
        "A repair booked once, with the manual's procedure and a priority",
        "Repair orders per alert, which must be one",
        "Book a procedure the maintenance manual does not list",
        ("look_up_manual", "book_repair"),
    ),
)


def langfuse_url(card_id: str) -> str:
    host = os.environ.get("LANGFUSE_UI", "http://localhost:8270")
    return f"{host}/project/mission-control/sessions/{card_id}"


def spend_html(board: Board, state: Path) -> str:
    rows = "".join(
        f"<tr><td>{E(who)}</td><td class=n>{n}</td><td class=n>{killed}</td>"
        f"<td class=n>{usd(spent)}</td></tr>"
        for who, (n, killed, spent) in budget.by_employee(board).items()
    )
    spent, cap, alert = sum(c.cost for c in board.cards), usd(budget.BUDGET), budget.sent(state)
    status = (
        f'<div class="verdict no"><b>Budget alert for {E(budget.LEAD)} on the board</b>'
        f"<span>{usd(alert['spent'])} spent when it went, over the {cap} budget.</span></div>"
        if alert
        else f'<div class="verdict ok"><b>Within budget</b><span>{usd(spent)} of {cap} spent.</span></div>'
    )
    return (
        '<h2 class="sec" id="spend">Spend by employee</h2><p class="sub">Every run priced from its tokens, '
        "a killed run from its trace in Langfuse. <code>make traces</code> checks each card against it.</p>"
        '<table class="tbl spend"><thead><tr><th>Employee</th><th class=n>Alerts worked</th>'
        "<th class=n>Runs killed</th><th class=n>Spent</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>{status}"
    )


def order_html(card: Card, cmms: Cmms) -> str:
    order = cmms.covering(card.id)
    trace = f'<p class="trace"><a href="{E(langfuse_url(card.id))}">Its runs in Langfuse</a></p>'
    if not order:
        why = cmms.dismissed.get(card.id)
        return trace + (f'<div class="receipt order"><h4>Dismissed</h4><p>{E(why)}</p></div>' if why else "")
    p = PROCEDURES[order.procedure]
    return trace + (
        f'<div class="receipt order"><h4>{E(order.id)} in the maintenance system</h4>'
        f"<p>{E(MACHINES[order.machine]['name'])}: {E(p['steps'])}.</p>"
        f"<p>{E(p['part'])}, {E(p['skill'])}, {p['hours']:g} h, {E(order.priority)} priority. "
        f"Alerts on this order: {E(', '.join(order.alerts))}.</p></div>"
    )


def dress(page: str) -> str:
    """Mission Control's page in KaaS Dairy's workspace, with what this session adds to its styles."""
    return page.replace("</head>", '<link rel="stylesheet" href="/upkeep.css"></head>', 1)


def board_page(state: Path) -> str:
    board = Board.open(state / "board.json")
    page = render(board, workspace=WORKSPACE).replace(
        '<div class="lanes">', spend_html(board, state) + '<div class="lanes">', 1
    )
    return dress(page)


def card_page(state: Path, card_id: str) -> str:
    card = Board.open(state / "board.json").card(card_id)
    page = detail(card, None, workspace=WORKSPACE)
    extra = order_html(card, Cmms.open(state / "cmms.json"))
    page = page.replace('<h2 class="sec">Runs</h2>', extra + '<h2 class="sec">Runs</h2>', 1)
    return dress(page)


def app(state: Path) -> web.Application:
    async def index(_: web.Request) -> web.Response:
        return web.Response(text=board_page(state), content_type="text/html")

    async def one(req: web.Request) -> web.Response:
        try:
            return web.Response(text=card_page(state, req.match_info["id"]), content_type="text/html")
        except StopIteration:
            raise web.HTTPNotFound() from None

    async def roster(_: web.Request) -> web.Response:
        page = team(Board.open(state / "board.json"), TEAM, workspace=WORKSPACE)
        return web.Response(text=dress(page), content_type="text/html")

    async def css(_: web.Request) -> web.FileResponse:
        return web.FileResponse(CSS)

    a = web.Application()
    a.router.add_get("/", index)
    a.router.add_get("/cards/{id}", one)
    a.router.add_get("/team", roster)
    a.router.add_get("/upkeep.css", css)
    a.router.add_static("/static", WEB)
    a.router.add_static("/fonts", WEB / "fonts")
    return a


def serve(state: Path, port: int = 8095) -> None:
    web.run_app(app(state), host="127.0.0.1", port=port)
