"""Mission Control's board, with what a brain adds: each answer's citations and the notes behind them.

A card page shows the answer and every citation the brain accepted, each linked to its note, then the
brain's call log, refusals included. A note page shows the note as filed, its source and its facts, with
the cited fact marked. Both read from where they are true: the server's log and the notes on disk.
"""

from __future__ import annotations

import html
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote as q

from aiohttp import web

from brain.server import calls, citations
from brain.vault import BRAIN, Refused, Vault, flat
from workforce.board import CHECK, WEB, Board, Role, chips, detail, page, plain, render, team

E = html.escape
CSS = Path(__file__).resolve().parent.parent / "web" / "brain.css"
WORKSPACE = "KaaS Consulting"


def cited_html(vault: Vault, state: Path, card_id: str, answer: str) -> str:
    rows = "".join(
        f'<li><a href="/notes/{E(c["note"])}?q={q(c["quote"])}">{E(vault.note(c["note"]).title)}</a>'
        f"<blockquote>{E(c['quote'])}</blockquote>"
        f'<span class="src">{E(vault.note(c["note"]).source)}</span></li>'
        for c in citations(state, card_id)
    )
    if not rows:
        return ""
    return (
        f'<div class="receipt cited"><h4>{CHECK}Answered from the brain</h4><p>{E(plain(answer)[:600])}</p>'
        f'<ol class="cites">{rows}</ol></div>'
    )


def calls_html(rows: list[dict]) -> str:
    body = (
        "".join(
            f'<tr class="{"" if c["allowed"] else "no"}"><td><code>{E(c["tool"])}</code></td>'
            f'<td class="args">{E(", ".join(c["args"].values()))}</td>'
            f"<td>{chips(['allowed'], 'green') if c['allowed'] else chips(['refused'], 'red')}</td>"
            f'<td class="said">{E(c["said"].splitlines()[0] if c["said"] else "")}</td></tr>'
            for c in rows
        )
        or '<tr><td colspan="4">No calls yet.</td></tr>'
    )
    return (
        '<h2 class="sec" id="calls">Brain calls</h2><p class="sub">Every call the researcher made to the '
        "brain, in order. A refused citation is one the brain could not find in a note.</p>"
        '<table class="tbl calls"><thead><tr><th>Tool</th><th>With</th><th>Brain</th><th>Said</th></tr>'
        f"</thead><tbody>{body}</tbody></table>"
    )


def card_page(state: Path, card_id: str, vault: Vault) -> str:
    c = Board.open(state / "board.json").card(card_id)
    # The board's own receipt shows chips only; the brain's shows each quote, so it replaces it.
    page_ = detail(replace(c, sources=[]), None, workspace=WORKSPACE)
    extra = cited_html(vault, state, c.id, c.answer) + calls_html(calls(state, c.id))
    return with_css(page_.replace('<h2 class="sec">Runs</h2>', extra + '<h2 class="sec">Runs</h2>', 1))


def note_page(vault: Vault, name: str, mark: str) -> str:
    n = vault.note(name)
    want = flat(mark)
    facts = "".join(f"<li{' class=hit' if want and want in f else ''}>{E(f)}</li>" for f in n.facts)
    live = (
        '<div class="wrap tight detail note"><p class="eyebrow">Brain note</p>'
        f'<h1 class="page">{E(n.title)}</h1>'
        f'<div class="refs">{chips([n.source])}{chips([n.date], "blue")}</div>'
        f'<h2 class="sec">Facts</h2><p class="sub">Each line is quoted word for word from {E(n.source)}.</p>'
        f'<ul class="facts">{facts}</ul></div>'
    )
    return with_css(page(n.title, live, "", workspace=WORKSPACE))


def with_css(p: str) -> str:
    return p.replace("</head>", '<link rel="stylesheet" href="/brain.css"></head>', 1)


def app(state: Path, roster: tuple[Role, ...], brain: Path = BRAIN) -> web.Application:
    vault = Vault(brain)

    async def index(_: web.Request) -> web.Response:
        return web.Response(
            text=with_css(render(Board.open(state / "board.json"), workspace=WORKSPACE)),
            content_type="text/html",
        )

    async def one(req: web.Request) -> web.Response:
        try:
            return web.Response(text=card_page(state, req.match_info["id"], vault), content_type="text/html")
        except StopIteration:
            raise web.HTTPNotFound() from None

    async def note(req: web.Request) -> web.Response:
        try:
            text = note_page(vault, req.match_info["name"], req.query.get("q", ""))
        except Refused:
            raise web.HTTPNotFound() from None
        return web.Response(text=text, content_type="text/html")

    async def roster_page(_: web.Request) -> web.Response:
        return web.Response(
            text=with_css(team(Board.open(state / "board.json"), roster, workspace=WORKSPACE)),
            content_type="text/html",
        )

    async def css(_: web.Request) -> web.FileResponse:
        return web.FileResponse(CSS)

    a = web.Application()
    a.router.add_get("/", index)
    a.router.add_get("/team", roster_page)
    a.router.add_get("/cards/{id}", one)
    a.router.add_get("/notes/{name}", note)
    a.router.add_get("/brain.css", css)
    a.router.add_static("/static", WEB)
    a.router.add_static("/fonts", WEB / "fonts")
    return a


def serve(state: Path, roster: tuple[Role, ...], port: int) -> None:
    web.run_app(app(state, roster), host="127.0.0.1", port=port)
