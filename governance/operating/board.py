"""Mission Control's board, with what governance adds: the signer's buttons, the record, the month.

A card waiting for a signature shows what the employee asked for, the rule that sent it to a person,
and Approve and Decline; the click writes the action only on a yes. The Team page opens with the
operating model from the policy file. The Month page is what the operations director reads: each
employee's outcomes, what was signed, declined or refused, its AI spend against budget and time saved.
"""

from __future__ import annotations

import os
from pathlib import Path

from aiohttp import web

from operating import policy, run
from operating.hotels import describe
from operating.record import Record
from workforce.board import WEB, Board, E, Role, chips, detail, page, render, team, usd

CSS = Path(__file__).resolve().parent.parent / "web" / "halden.css"
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


def ask_html(card_id: str, w: dict) -> str:
    return (
        f'<div class="ask"><b>{E(w["agent"])} asks: {E(describe(w["action"]))}</b>'
        f"<span>{E(w['rule'])}.</span>"
        f'<div class="refs">Signs {chips([policy.first(w["signer"])], "wait")}</div>'
        f"<em>Nothing is written until {E(policy.first(w['signer']))} answers.</em>"
        f'<form method="post" action="/cards/{E(card_id)}/decide" class="decide">'
        '<button class="btn dark" name="yes" value="1">Approve</button>'
        '<button class="btn" name="yes" value="0">Decline</button></form></div>'
    )


def into_card(html: str, card_id: str, extra: str) -> str:
    """Put the ask in place of the card's last line on the board."""
    at = html.find(f'<a href="/cards/{E(card_id)}">')
    start = html.find('<p class="last">', at)
    end = html.find("</p>", start) + len("</p>")
    return html[:start] + extra + html[end:] if at >= 0 and start >= 0 else html


def for_(r: dict) -> str:
    return ", for " + E(policy.first(r["signer"])) if r["signer"] and r["by"] != "policy" else ""


def record_html(rows: list[dict]) -> str:
    body = "".join(
        f'<tr><td>{E(r["at"][11:16])}</td><td><span class="verdict-chip {E(r["verdict"].replace(" ", "-"))}">'
        f"{E(r['verdict'])}</span></td><td>{E(r['action'])}</td><td>{E(r['rule'])}</td>"
        f"<td>{E(r['by'])}{for_(r)}"
        "</td></tr>"
        for r in rows
    )
    return (
        '<h2 class="sec">Decision record</h2><p class="sub">Every verdict on this card, appended and never '
        "rewritten. <code>make record</code> checks each write against it.</p>"
        '<table class="tbl record"><thead><tr><th>At</th><th>Verdict</th><th>Action</th><th>Rule</th>'
        f"<th>Decided by</th></tr></thead><tbody>{body}</tbody></table>"
    )


def dress(html: str, here: str = "") -> str:
    """Mission Control's page in Halden Hotels' workspace, with the Month page in its nav."""
    html = html.replace("Workspace <b>Acme</b>", "Workspace <b>Halden Hotels</b>", 1)
    month = (
        '<a href="/month" aria-current=page>Month</a>' if here == "Month" else '<a href="/month">Month</a>'
    )
    html = html.replace('<a href="/#activity">', month + '<a href="/#activity">', 1)
    return html.replace("</head>", '<link rel="stylesheet" href="/halden.css"></head>', 1)


def board_page(state: Path) -> str:
    html = render(Board.open(state / "board.json"))
    for card_id, w in run.waiting(state).items():
        html = into_card(html, card_id, ask_html(card_id, w))
    return dress(html)


def card_page(state: Path, card_id: str) -> str:
    card = Board.open(state / "board.json").card(card_id)
    html = detail(card, None)
    w = run.waiting(state).get(card_id)
    extra = (ask_html(card_id, w) if w else "") + record_html(Record(state / "record.jsonl").of(card_id))
    return dress(html.replace('<h2 class="sec">Runs</h2>', extra + '<h2 class="sec">Runs</h2>', 1))


def signs(a: dict) -> str:
    return "The hotel manager" if a["signer"] == "hotel manager" else policy.first(a["signer"])


def model_html() -> str:
    rows = "".join(
        f"<tr><td><b>{E(a['name'])}</b></td>"
        f'<td><span class="status {E(a["status"])}">{E(a["status"])}</span></td>'
        f"<td>{E(a['owner'])}</td><td>{E(policy.alone(key))}</td>"
        f"<td>{E(signs(a))}</td>"
        f'<td class="n">{usd(a["cap"])}</td></tr>'
        for key, a in policy.AGENTS.items()
    )
    return (
        '<h2 class="sec" id="model">Operating model</h2><p class="sub">From the policy file '
        f"{E(policy.first(policy.POLICY['director']))} owns. The gate reads it before every write.</p>"
        '<table class="tbl model"><thead><tr><th>Employee</th><th>Status</th><th>Answers to</th>'
        '<th>May do alone</th><th>Signs above that</th><th class="n">AI budget a month</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )


def team_page(state: Path) -> str:
    html = team(Board.open(state / "board.json"), TEAM)
    return dress(html.replace('<div class="roster">', model_html() + '<div class="roster">', 1))


def month_page(state: Path) -> str:
    rows = run.month(state)
    done = sum(r["done"] for r in rows)
    spend = sum(r["spent"] for r in rows)
    hours = sum(r["saved"] for r in rows) / 60
    answered = sum(r["signed"] + r["declined"] for r in rows)
    approval = f"{sum(r['signed'] for r in rows) / answered:.0%}" if answered else "None yet"
    body = "".join(
        f'<tr><td><b>{E(r["name"])}</b></td><td class="n">{r["done"]}</td><td class="n">{r["allowed"]}</td>'
        f'<td class="n">{r["signed"]}</td><td class="n">{r["declined"]}</td><td class="n">{r["refused"]}</td>'
        f'<td class="n">{usd(r["spent"])} of {usd(r["cap"])}</td><td class="n">{r["saved"] / 60:.1f} h</td>'
        f'<td class="n">{usd(r["per"]) if r["per"] is not None else "None"}</td></tr>'
        for r in rows
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
    return dress(page("Month", live, ""), "Month")


def app(state: Path) -> web.Application:
    def html(render_page):
        async def handler(req: web.Request) -> web.Response:
            try:
                return web.Response(text=render_page(req), content_type="text/html")
            except StopIteration:
                raise web.HTTPNotFound() from None

        return handler

    async def decide(req: web.Request) -> web.Response:
        # Any page open in the same browser can post to 127.0.0.1, or rebind its own name to it; only the
        # board's own form, served from loopback, counts.
        local = req.host.rsplit(":", 1)[0] in ("127.0.0.1", "localhost")
        if not local or req.headers.get("Origin") != f"{req.scheme}://{req.host}":
            raise web.HTTPForbidden(text="sign from the board itself")
        form = await req.post()
        try:
            run.sign(
                state,
                req.match_info["id"],
                f"board:{os.environ.get('USER', 'human')}",
                form.get("yes") == "1",
            )
        except run.NotAllowed as e:
            raise web.HTTPConflict(text=str(e)) from None
        raise web.HTTPSeeOther("/")

    async def css(_: web.Request) -> web.FileResponse:
        return web.FileResponse(CSS)

    a = web.Application()
    a.router.add_get("/", html(lambda _: board_page(state)))
    a.router.add_get("/cards/{id}", html(lambda r: card_page(state, r.match_info["id"])))
    a.router.add_get("/team", html(lambda _: team_page(state)))
    a.router.add_get("/month", html(lambda _: month_page(state)))
    a.router.add_post("/cards/{id}/decide", decide)
    a.router.add_get("/halden.css", css)
    a.router.add_static("/static", WEB)
    a.router.add_static("/fonts", WEB / "fonts")
    return a


def serve(state: Path, port: int = 8096) -> None:
    web.run_app(app(state), host="127.0.0.1", port=port)
