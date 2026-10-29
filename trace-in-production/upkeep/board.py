"""Mission Control's board, signed in, with what production adds: each employee's spend against the budget.

Above the lanes, what each AI employee spent, killed runs included, and whether the shift is paused for the
maintenance lead. The pause card holds the one form that continues it. A Done card shows the repair order
the maintenance system holds, and every card links to its runs in Langfuse, the card id being the session.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from aiohttp import web

from upkeep import budget
from upkeep.crew import PLANNER, TRIAGE
from upkeep.ledger import langfuse
from upkeep.plant import MACHINES, PROCEDURES, Cmms, Refused
from upkeep.run import PAUSE_CARD, PAUSED, allow_continue, continue_shift
from workforce.board import WEB, Board, Card, E, Role, chips, detail, render, team, usd
from workforce.people import PERSON, Person, people_or_exit, require_sign_in, signed_in_html

ROOT = Path(__file__).resolve().parent.parent
CSS = ROOT / "web" / "upkeep.css"
PEOPLE_FILE = ROOT / "fixtures" / "people.json"
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
        f"<tr><td>{E(who)}</td><td class=n>{count}</td><td class=n>{killed}</td>"
        f"<td class=n>{usd(spent)}</td></tr>"
        for who, (count, killed, spent) in budget.by_employee(board).items()
    )
    total, cap = budget.spent(board), usd(budget.budget_usd())
    status = (
        f'<div class="verdict no"><b>Over budget: paused for {E(budget.LEAD)}</b>'
        f"<span>{usd(total)} spent of the {cap} budget.</span></div>"
        if (state / PAUSED).exists()
        else f'<div class="verdict ok"><b>Within budget</b><span>{usd(total)} of {cap} spent.</span></div>'
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
    procedure = PROCEDURES[order.procedure]
    return trace + (
        f'<div class="receipt order"><h4>{E(order.order_id)} in the maintenance system</h4>'
        f"<p>{E(MACHINES[order.machine]['name'])}: {E(procedure['steps'])}.</p>"
        f"<p>{E(procedure['part'])}, {E(procedure['skill'])}, {procedure['hours']:g} h, "
        f"{E(order.priority)} priority. Alerts on this order: {E(', '.join(order.alerts))}.</p></div>"
    )


def pause_html(card: Card, state: Path) -> str:
    """The pause card: the question, the form while the shift waits, and every attempt on it."""
    rows = (
        "".join(
            f'<tr class="{"no" if row["decision"] == "refused" else ""}"><td>{E(row["by"])}</td>'
            f"<td>{chips([row['decision']], 'red' if row['decision'] == 'refused' else 'green')}</td>"
            f'<td class="said">{E(row["said"])}</td><td>{E(row["at"])}</td></tr>'
            for row in budget.decisions(state)
        )
        or '<tr><td colspan="4">No decisions yet.</td></tr>'
    )
    form = (
        '<div class="receipt open"><h4>The shift is paused</h4>'
        f"<p>{E(budget.LEAD)} decides whether the rest of the alerts run past the budget.</p>"
        f'<form method="post" action="/cards/{E(card.id)}/continue" class="decide">'
        '<button class="btn dark">Continue the shift</button></form></div>'
        if (state / PAUSED).exists()
        else ""
    )
    return (
        form + '<h2 class="sec" id="decisions">Decisions</h2><p class="sub">Every attempt to continue the '
        "shift, refusals included.</p>"
        '<table class="tbl decisions"><thead><tr><th>Who</th><th>Decision</th><th>Said</th><th>At</th></tr>'
        f"</thead><tbody>{rows}</tbody></table>"
    )


def with_css(text: str) -> str:
    return text.replace("</head>", '<link rel="stylesheet" href="/upkeep.css"></head>', 1)


def board_page(state: Path, person: Person) -> str:
    board = Board.open(state / "board.json")
    page = render(board, workspace=WORKSPACE, signed_in=signed_in_html(person)).replace(
        '<div class="lanes">', spend_html(board, state) + '<div class="lanes">', 1
    )
    return with_css(page)


def card_page(state: Path, card_id: str, person: Person) -> str:
    card = Board.open(state / "board.json").card(card_id)
    page = detail(card, None, workspace=WORKSPACE, signed_in=signed_in_html(person))
    extra = (
        pause_html(card, state) if card.id == PAUSE_CARD else order_html(card, Cmms.open(state / "cmms.json"))
    )
    return with_css(page.replace('<h2 class="sec">Runs</h2>', extra + '<h2 class="sec">Runs</h2>', 1))


def app(state: Path, people) -> web.Application:
    tasks: set[asyncio.Task] = set()

    async def index(request: web.Request) -> web.Response:
        return web.Response(text=board_page(state, request[PERSON]), content_type="text/html")

    async def one(request: web.Request) -> web.Response:
        try:
            text = card_page(state, request.match_info["id"], request[PERSON])
        except StopIteration:
            raise web.HTTPNotFound() from None
        return web.Response(text=text, content_type="text/html")

    async def roster(request: web.Request) -> web.Response:
        page = team(
            Board.open(state / "board.json"),
            TEAM,
            workspace=WORKSPACE,
            signed_in=signed_in_html(request[PERSON]),
        )
        return web.Response(text=with_css(page), content_type="text/html")

    async def proceed(request: web.Request) -> web.Response:
        """The signed-in person asks to continue; the refusal is already logged, so the card page shows it."""
        name = request[PERSON].name
        try:
            allow_continue(state, name)
        except Refused:
            raise web.HTTPSeeOther(f"/cards/{PAUSE_CARD}") from None
        task = asyncio.create_task(continue_shift(state, name, get=langfuse()))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        raise web.HTTPSeeOther(f"/cards/{PAUSE_CARD}")

    async def css(_: web.Request) -> web.FileResponse:
        return web.FileResponse(CSS)

    web_app = web.Application()
    require_sign_in(web_app, people, WORKSPACE, ("/upkeep.css",))
    web_app.router.add_get("/", index)
    web_app.router.add_get("/cards/{id}", one)
    web_app.router.add_post(f"/cards/{PAUSE_CARD}/continue", proceed)
    web_app.router.add_get("/team", roster)
    web_app.router.add_get("/upkeep.css", css)
    web_app.router.add_static("/static", WEB)
    web_app.router.add_static("/fonts", WEB / "fonts")
    return web_app


def serve(state: Path, port: int = 8095) -> None:
    web.run_app(app(state, people_or_exit(PEOPLE_FILE)), host="127.0.0.1", port=port)
