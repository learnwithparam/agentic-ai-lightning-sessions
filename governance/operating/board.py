"""Mission Control's board, with what governance adds: the signer's buttons, the record, the month.

A card waiting for a signature shows what the employee asked for, the rule that sent it to a person,
and Approve and Decline; the click writes the action only on a yes. The Team page opens with the
operating model from the policy file. The Month page is what the operations director reads: each
employee's outcomes, what was signed, declined or refused, its AI spend against budget and time saved.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from aiohttp import web

from operating import policy, run
from operating.hotels import describe
from operating.record import Record
from workforce.board import WEB, Board, E, Role, chips, detail, page, render, team, usd
from workforce.people import (
    PERSON,
    Person,
    people_or_exit,
    require_sign_in,
    signed_in_html,
)

ROOT = Path(__file__).resolve().parent.parent
CSS = ROOT / "web" / "hotels.css"
PEOPLE_FILE = ROOT / "fixtures" / "people.json"
WORKSPACE = "KaaS Hotels"
TEAM = (
    Role(
        "Rate manager",
        "Each night priced to fill the hotel, from demand and the competitors' rates",
        "Revenue per available room, against last year's week",
        "Price a night under the floor the revenue manager set",
        ("read_demand", "set_price"),
    ),
    Role(
        "Supplies buyer",
        "Every item back to par before it runs out",
        "Days any item spent under par this month",
        "Order from a supplier the operations director has not approved",
        ("read_stock", "place_order"),
    ),
    Role(
        "Rota planner",
        "Every housekeeping shift covered the day it opens",
        "Shifts left uncovered at the start of the day",
        "Give anyone less than 11 hours between shifts",
        ("read_rota", "assign_shift"),
    ),
    Role(
        "Linen forecaster",
        "Next month's linen ordered from a forecast, not last month's guess",
        "Forecast against what was used, in shadow until it holds",
        "Order anything while it is paused",
        ("read_usage",),
    ),
)


def ask_html(card_id: str, waiting: dict) -> str:
    return (
        f'<div class="ask"><b>{E(waiting["agent"])} asks: {E(describe(waiting["action"]))}</b>'
        f"<span>{E(waiting['rule'])}.</span>"
        f'<div class="refs">Signs {chips([policy.first(waiting["signer"])], "wait")}</div>'
        f"<em>Nothing is written until {E(policy.first(waiting['signer']))} answers.</em>"
        f'<form method="post" action="/cards/{E(card_id)}/decide" class="decide">'
        '<button class="btn dark" name="yes" value="1">Approve</button>'
        '<button class="btn" name="yes" value="0">Decline</button></form></div>'
    )


def into_card(html: str, card_id: str, extra: str) -> str:
    """Put the ask in place of the card's last line on the board."""
    anchor = html.find(f'<a href="/cards/{E(card_id)}">')
    start = html.find('<p class="last">', anchor)
    end = html.find("</p>", start) + len("</p>")
    return html[:start] + extra + html[end:] if anchor >= 0 and start >= 0 else html


def for_(row: dict) -> str:
    return (
        ", for " + E(policy.first(row["signer"]))
        if row["signer"] and row["by"] != "policy"
        else ""
    )


def record_html(rows: list[dict]) -> str:
    body = "".join(
        f'<tr><td>{E(row["at"][11:16])}</td><td><span class="verdict-chip {E(row["verdict"].replace(" ", "-"))}">'
        f"{E(row['verdict'])}</span></td><td>{E(row['action'])}</td><td>{E(row['rule'])}</td>"
        f"<td>{E(row['by'])}{for_(row)}"
        "</td></tr>"
        for row in rows
    )
    return (
        '<h2 class="sec">Decision record</h2><p class="sub">Every verdict on this card, appended and never '
        "rewritten. <code>make decisions</code> checks each write against it.</p>"
        '<table class="tbl record"><thead><tr><th>At</th><th>Verdict</th><th>Action</th><th>Rule</th>'
        f"<th>Decided by</th></tr></thead><tbody>{body}</tbody></table>"
    )


def dress(html: str, here: str = "") -> str:
    """Mission Control's page in KaaS Hotels' workspace, with the Month page in its nav."""
    month = (
        '<a href="/month" aria-current=page>Month</a>'
        if here == "Month"
        else '<a href="/month">Month</a>'
    )
    html = html.replace('<a href="/#activity">', month + '<a href="/#activity">', 1)
    return html.replace(
        "</head>", '<link rel="stylesheet" href="/hotels.css"></head>', 1
    )


def board_page(state: Path, person: Person) -> str:
    html = render(
        Board.open(state / "board.json"),
        workspace=WORKSPACE,
        signed_in=signed_in_html(person),
    )
    for card_id, waiting in run.waiting(state).items():
        html = into_card(html, card_id, ask_html(card_id, waiting))
    return dress(html)


def refusal_html(card) -> str:
    """The latest refusal on the card, so the room sees why a signature did not count."""
    refused = [line for line in card.log if "refused:" in line]
    return (
        f'<p class="refusal">{E(refused[-1].split("refused:", 1)[1].strip())}</p>'
        if refused
        else ""
    )


def card_page(state: Path, card_id: str, person: Person) -> str:
    card = Board.open(state / "board.json").card(card_id)
    html = detail(card, None, workspace=WORKSPACE, signed_in=signed_in_html(person))
    waiting = run.waiting(state).get(card_id)
    extra = (
        refusal_html(card)
        + (ask_html(card_id, waiting) if waiting else "")
        + record_html(Record(state / "record.jsonl").for_card(card_id))
    )
    return dress(
        html.replace(
            '<h2 class="sec">Runs</h2>', extra + '<h2 class="sec">Runs</h2>', 1
        )
    )


def signs(employee: dict) -> str:
    return (
        "The hotel manager"
        if employee["signer"] == "hotel manager"
        else policy.first(employee["signer"])
    )


def model_html() -> str:
    rows = "".join(
        f"<tr><td><b>{E(employee['name'])}</b></td>"
        f'<td><span class="status {E(employee["status"])}">{E(employee["status"])}</span></td>'
        f"<td>{E(employee['owner'])}</td><td>{E(policy.alone(key))}</td>"
        f"<td>{E(signs(employee))}</td>"
        f'<td class="n">{usd(employee["cap"])}</td></tr>'
        for key, employee in policy.AGENTS.items()
    )
    return (
        '<h2 class="sec" id="model">Operating model</h2><p class="sub">From the policy file '
        f"{E(policy.first(policy.POLICY['director']))} owns. The gate reads it before every write.</p>"
        '<table class="tbl model"><thead><tr><th>Employee</th><th>Status</th><th>Answers to</th>'
        '<th>May do alone</th><th>Signs above that</th><th class="n">AI budget a month</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )


def team_page(state: Path, person: Person) -> str:
    html = team(
        Board.open(state / "board.json"),
        TEAM,
        workspace=WORKSPACE,
        signed_in=signed_in_html(person),
    )
    return dress(
        html.replace('<div class="roster">', model_html() + '<div class="roster">', 1)
    )


def month_page(state: Path, person: Person) -> str:
    rows = run.month(state)
    done = sum(row["done"] for row in rows)
    spend = sum(row["spent"] for row in rows)
    hours = sum(row["saved"] for row in rows) / 60
    answered = sum(row["signed"] + row["declined"] for row in rows)
    approval = (
        f"{sum(row['signed'] for row in rows) / answered:.0%}"
        if answered
        else "None yet"
    )
    body = "".join(
        f'<tr><td><b>{E(row["name"])}</b></td><td class="n">{row["done"]}</td><td class="n">{row["allowed"]}</td>'
        f'<td class="n">{row["signed"]}</td><td class="n">{row["declined"]}</td><td class="n">{row["refused"]}</td>'
        f'<td class="n">{usd(row["spent"])} of {usd(row["cap"])}</td><td class="n">{row["saved"] / 60:.1f} h</td>'
        f'<td class="n">{usd(row["per"]) if row["per"] is not None else "None"}</td></tr>'
        for row in rows
    )
    live = f"""<div class="wrap tight"><div class="head"><div><h1 class="page">Month</h1>
<p>What the AI employees did this month, read from the decision record and the board.</p></div>
<div class="stats"><div class="stat lead"><span>Outcomes</span><b>{done}</b>
<em>written to a system of record</em></div><div class="stat"><span>AI spend</span><b>{usd(spend)}</b></div>
<div class="stat"><span>Time saved</span><b>{hours:.1f} h</b>
<em>minutes per action, from the policy</em></div>
<div class="stat"><span>Signed of asked</span><b>{approval}</b></div></div></div>
<table class="tbl month"><thead><tr><th>Employee</th><th class="n">Outcomes</th><th class="n">Alone</th>
<th class="n">Signed</th><th class="n">Declined</th><th class="n">Refused</th><th class="n">AI spend</th>
<th class="n">Time saved</th><th class="n">Cost per outcome</th></tr></thead><tbody>{body}</tbody></table>
<p class="sub">Waiting for a signature: {len(run.waiting(state))}.</p></div>"""
    return dress(
        page("Month", live, "", workspace=WORKSPACE, signed_in=signed_in_html(person)),
        "Month",
    )


def app(state: Path, people: list[Person]) -> web.Application:
    def html(render_page):
        async def handler(req: web.Request) -> web.Response:
            try:
                return web.Response(text=render_page(req), content_type="text/html")
            except StopIteration:
                raise web.HTTPNotFound() from None

        return handler

    async def decide(req: web.Request) -> web.Response:
        """The signed-in person answers; a refusal is already in the record and on the card."""
        form = await req.post()
        card_id = req.match_info["id"]
        with contextlib.suppress(run.NotAllowed):
            run.sign(state, card_id, req[PERSON].name, form.get("yes") == "1")
        raise web.HTTPSeeOther(f"/cards/{card_id}")

    async def css(_: web.Request) -> web.FileResponse:
        return web.FileResponse(CSS)

    web_app = web.Application()
    require_sign_in(web_app, people, WORKSPACE, ("/hotels.css",))
    web_app.router.add_get("/", html(lambda req: board_page(state, req[PERSON])))
    web_app.router.add_get(
        "/cards/{id}",
        html(lambda req: card_page(state, req.match_info["id"], req[PERSON])),
    )
    web_app.router.add_get("/team", html(lambda req: team_page(state, req[PERSON])))
    web_app.router.add_get("/month", html(lambda req: month_page(state, req[PERSON])))
    web_app.router.add_post("/cards/{id}/decide", decide)
    web_app.router.add_get("/hotels.css", css)
    web_app.router.add_static("/static", WEB)
    web_app.router.add_static("/fonts", WEB / "fonts")
    return web_app


def serve(state: Path, port: int = 8096) -> None:
    web.run_app(app(state, people_or_exit(PEOPLE_FILE)), host="127.0.0.1", port=port)
