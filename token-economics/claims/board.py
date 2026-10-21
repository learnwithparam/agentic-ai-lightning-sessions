"""Mission Control's board, with what cost adds: each pass of the ladder, and every read on a claim.

The front page is the last pass's board with the ladder above it, one row per pass, each claim's cost and
the pass's cost per claim filed; a row opens that pass's own board. A card page adds the claim file the
claims system holds and its ledger, one row per document with the provider's token counts.
"""

from __future__ import annotations

import html
from pathlib import Path

from aiohttp import web

from claims.reader import LADDER, STATE, passes
from claims.system import Claims
from workforce.board import CHECK, WEB, Board, Role, detail, render, team, usd

E = html.escape
CSS = Path(__file__).resolve().parent.parent / "web" / "claims.css"


def ladder_html(state: Path, here: int) -> str:
    rows = "".join(
        f'<tr{" class=here" if p.n == here else ""}><td><a href="/pass/{p.n}">{E(p.name)}</a></td>'
        f"<td class=n>{p.filed} of {len(p.board.cards)}</td><td class=n>{p.fresh:,}</td>"
        f"<td class=n>{p.cached:,}</td><td class=n>{p.output:,}</td>"
        f'<td class="n lead">{"none filed" if p.per_claim is None else usd(p.per_claim)}</td>'
        f"<td class=n>{p.per_thousand}</td></tr>"
        for p in passes(state)
    )
    return (
        '<h2 class="sec" id="ladder">Before and after</h2><p class="sub">The same three claims, read four '
        "times. Each pass adds one lever; the ledger prices every read from the provider's own token counts."
        '</p><table class="tbl ladder"><thead><tr><th>Pass</th><th class=n>Filed</th>'
        "<th class=n>Fresh input</th><th class=n>Cached input</th><th class=n>Output</th>"
        "<th class=n>Cost per claim filed</th><th class=n>Per 1,000 claims</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def on_pass(page: str, n: int) -> str:
    return page.replace('href="/cards/', f'href="/pass/{n}/cards/')


def board_page(state: Path, n: int) -> str:
    page = render(Board.open(state / f"pass-{n}" / "board.json"))
    page = page.replace('<div class="lanes">', ladder_html(state, n) + '<div class="lanes">', 1)
    return with_css(on_pass(page, n))


def filed_html(claims: Claims, cid: str) -> str:
    f = claims.filed(cid)
    if f is None:
        return ""
    total = f["total"] or "none until an assessor prices it"
    rows = [("Parts", ", ".join(f["parts"])), ("Vehicles", str(f["vehicles"])), ("Total", total)]
    cells = "".join(f"<dt>{E(k)}</dt><dd>{E(v)}</dd>" for k, v in rows)
    return (
        f'<div class="receipt filed"><h4>{CHECK}Filed in the claims system</h4><dl>{cells}</dl>'
        f'<p class="last">{E(f["note"])}</p></div>'
    )


def ledger_html(claims: Claims, cid: str) -> str:
    rows = claims.ledger(cid)
    body = "".join(
        f"<tr><td>{E(t['doc'])}</td><td class=n>{t['input']:,}</td><td class=n>{t['cached']:,}</td>"
        f"<td class=n>{t['output']:,}</td><td class=n>{usd(t['cost'])}</td></tr>"
        for t in rows
    )
    if stop := claims.stopped(cid):
        body += (
            f'<tr class="stop"><td>{E(stop["before"])}</td><td colspan="4">Not read: one more read would '
            f"cross the {usd(claims.budget)} budget. {E(claims.adjuster)} decides.</td></tr>"
        )
    return (
        '<h2 class="sec" id="ledger">Ledger</h2><p class="sub">Every document on this claim and what reading '
        f"it cost, against a budget of {usd(claims.budget)}.</p>"
        '<table class="tbl ledger"><thead><tr><th>Document</th><th class=n>Tokens in</th>'
        "<th class=n>Cached</th><th class=n>Tokens out</th><th class=n>Cost</th></tr></thead>"
        f'<tbody>{body}</tbody><tfoot><tr><td>Spent</td><td colspan="3"></td>'
        f"<td class=n>{usd(claims.spent(cid))}</td></tr></tfoot>"
        "</table>"
    )


def card_page(state: Path, n: int, card_id: str) -> str:
    c = Board.open(state / f"pass-{n}" / "board.json").card(card_id)
    claims = Claims.open(state / f"pass-{n}")
    extra = filed_html(claims, c.issue) + ledger_html(claims, c.issue)
    page = detail(c, None).replace('<h2 class="sec">Runs</h2>', extra + '<h2 class="sec">Runs</h2>', 1)
    return with_css(on_pass(page, n))


def with_css(page: str) -> str:
    return page.replace("</head>", '<link rel="stylesheet" href="/claims.css"></head>', 1)


def app(state: Path, roster: tuple[Role, ...]) -> web.Application:
    def last() -> int:
        return max((p.n for p in passes(state)), default=len(LADDER))

    def n_of(req: web.Request) -> int:
        n = int(req.match_info.get("n", last()))
        if not (state / f"pass-{n}" / "board.json").exists():
            raise web.HTTPNotFound()
        return n

    async def index(req: web.Request) -> web.Response:
        return web.Response(text=board_page(state, n_of(req)), content_type="text/html")

    async def one(req: web.Request) -> web.Response:
        try:
            return web.Response(
                text=card_page(state, n_of(req), req.match_info["id"]), content_type="text/html"
            )
        except StopIteration:
            raise web.HTTPNotFound() from None

    async def roster_page(_: web.Request) -> web.Response:
        board = Board.open(state / f"pass-{last()}" / "board.json")
        return web.Response(text=with_css(team(board, roster)), content_type="text/html")

    async def css(_: web.Request) -> web.FileResponse:
        return web.FileResponse(CSS)

    a = web.Application()
    a.router.add_get("/", index)
    a.router.add_get("/pass/{n:[1-9]}", index)
    a.router.add_get("/cards/{id}", one)
    a.router.add_get("/pass/{n:[1-9]}/cards/{id}", one)
    a.router.add_get("/team", roster_page)
    a.router.add_get("/claims.css", css)
    a.router.add_static("/static", WEB)
    a.router.add_static("/fonts", WEB / "fonts")
    return a


def serve(state: Path = STATE, roster: tuple[Role, ...] = (), port: int = 8092) -> None:
    web.run_app(app(state, roster), host="127.0.0.1", port=port)
