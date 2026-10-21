"""Mission Control's board, with what screening adds to a card: the shortlist, the flagged CV, the audit log.

The card page reads each from the hiring system. A flagged CV is drawn as a recruiter sees it, with the
text only a parser gets shown in place, and the recruiter's Reject and Clear buttons under it.
"""

from __future__ import annotations

import html
from pathlib import Path

from aiohttp import web

from hiring.ats import ATS, Refused
from hiring.guard import scan
from hiring.screener import settle
from workforce.board import CHECK, WEB, Board, Role, chips, detail, render, team

E = html.escape
CSS = Path(__file__).resolve().parent.parent / "web" / "hiring.css"


def shortlist_html(ats: ATS, sl: dict, waiting: bool) -> str:
    head = f"Held until {ats.recruiter} decides" if waiting else f"{CHECK}Sent to {sl['manager']}"
    ranked = "".join(f"<li>{E(ats.candidate(c)['name'])}</li>" for c in sl["candidates"])
    rows = [("Role", f"{sl['req']}, {ats.role(sl['req'])['title']}"), ("Submitted by", sl["by"])]
    cells = "".join(f"<dt>{E(k)}</dt><dd>{E(v)}</dd>" for k, v in rows)
    return (
        f'<div class="receipt shortlist{" open" if waiting else ""}"><h4>{head}</h4>'
        f'<ol class="ranked">{ranked}</ol><dl>{cells}</dl></div>'
    )


def flagged_html(ats: ATS, card: str, cid: str, flag: dict) -> str:
    s, name = scan(ats.cv(cid)), ats.candidate(cid)["name"]
    lines = "".join(
        f'<p class="hid"><mark>{E(t)}</mark></p>' if t in flag["hidden"] else f"<p>{E(t)}</p>"
        for t in s.text.splitlines()
    )
    if flag["reviewed_by"]:
        did = "rejected the application" if flag.get("rejected") else "cleared the CV"
        foot = f'<p class="done">{CHECK}{E(flag["reviewed_by"])} {did}.</p>'
    else:
        foot = (
            f'<form method="post" action="/cards/{E(card)}/review" class="decide">'
            f'<input type="hidden" name="candidate" value="{E(cid)}">'
            '<button class="btn dark" name="reject" value="1">Reject the application</button>'
            '<button class="btn" name="reject" value="0">Clear the CV</button></form>'
        )
    return (
        f'<h2 class="sec" id="flagged">{E(name)}\'s CV</h2><p class="sub">What a recruiter sees on the '
        "page. The highlighted line is in the file but drawn so a person cannot read it; the screener was "
        f'never given this CV.</p><div class="cv">{lines}</div>{foot}'
    )


def audit_html(rows: list[dict], ats: ATS) -> str:
    body = (
        "".join(
            f"<tr><td><b>{E(a['check'])}</b></td>"
            f"<td>{E(ats.candidate(a['candidate'])['name'] if a['candidate'] else 'the request')}</td>"
            f"<td>{chips(['recruiter'], 'blue') if a['check'] == 'recruiter' else chips(['blocked'], 'red')}"
            "</td>"
            f'<td class="said">{E(a["what"])}</td></tr>'
            for a in rows
        )
        or '<tr><td colspan="4">Nothing was blocked.</td></tr>'
    )
    return (
        '<h2 class="sec" id="audit">Guardrail log</h2><p class="sub">Every block on this card, as the hiring '
        "system recorded it.</p>"
        '<table class="tbl audit"><thead><tr><th>Check</th><th>About</th><th>Result</th>'
        f"<th>What happened</th></tr></thead><tbody>{body}</tbody></table>"
    )


def card_page(state: Path, card_id: str) -> str:
    c = Board.open(state / "board.json").card(card_id)
    ats = ATS.open(state)
    flags, sl = ats.flags(c.id), ats.shortlist(c.id)
    waiting = any(not f["reviewed_by"] for f in flags.values())
    extra = shortlist_html(ats, sl, waiting) if sl else ""
    extra += "".join(flagged_html(ats, c.id, cid, f) for cid, f in flags.items())
    extra += audit_html(ats.log(c.id), ats)
    # The hiring system's evidence goes above the runs, so the card reads as outcome first.
    page = detail(c, None).replace('<h2 class="sec">Runs</h2>', extra + '<h2 class="sec">Runs</h2>', 1)
    return with_css(page)


def with_css(page: str) -> str:
    return page.replace("</head>", '<link rel="stylesheet" href="/hiring.css"></head>', 1)


def app(state: Path, roster: tuple[Role, ...]) -> web.Application:
    async def index(_: web.Request) -> web.Response:
        return web.Response(text=with_css(render(Board.open(state / "board.json"))), content_type="text/html")

    async def one(req: web.Request) -> web.Response:
        try:
            return web.Response(text=card_page(state, req.match_info["id"]), content_type="text/html")
        except StopIteration:
            raise web.HTTPNotFound() from None

    async def review(req: web.Request) -> web.Response:
        # Any page open in the same browser can post to 127.0.0.1; only the board's own form counts.
        local = req.host.rsplit(":", 1)[0] in ("127.0.0.1", "localhost")
        if not local or req.headers.get("Origin") != f"{req.scheme}://{req.host}":
            raise web.HTTPForbidden(text="review from the board itself")
        form, card_id = await req.post(), req.match_info["id"]
        board, ats = Board.open(state / "board.json"), ATS.open(state)
        try:
            ats.review(card_id, str(form.get("candidate", "")), ats.recruiter, form.get("reject") == "1")
        except Refused as e:
            raise web.HTTPConflict(text=str(e)) from None
        settle(board, board.card(card_id), ats)
        raise web.HTTPSeeOther(f"/cards/{card_id}")

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
    a.router.add_post("/cards/{id}/review", review)
    a.router.add_get("/hiring.css", css)
    a.router.add_static("/static", WEB)
    a.router.add_static("/fonts", WEB / "fonts")
    return a


def serve(state: Path, roster: tuple[Role, ...], port: int) -> None:
    web.run_app(app(state, roster), host="127.0.0.1", port=port)
