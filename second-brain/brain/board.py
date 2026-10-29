"""Mission Control's board, with what a brain adds: each answer's citations and the notes behind them.

A card page shows the answer and every citation the brain accepted, each linked to its note, then the
brain's call log, refusals included. A note page shows the note as filed, its source and its facts, with
the cited fact marked. Both read from where they are true: the server's log and the notes on disk.
"""

from __future__ import annotations

import html
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from brain.server import calls, citations
from brain.vault import BRAIN, Refused, Vault, flat
from workforce.board import CHECK, WEB, Board, Role, chips, detail, page, plain, render, team
from workforce.people import PERSON, Person, people_or_exit, require_sign_in, signed_in_html

ROOT = Path(__file__).resolve().parent.parent
CSS = ROOT / "web" / "brain.css"
PEOPLE_FILE = ROOT / "fixtures" / "people.json"
WORKSPACE = "KaaS Consulting"
escape = html.escape


def cited_html(vault: Vault, state: Path, card_id: str, answer: str) -> str:
    rows = "".join(
        f'<li><a href="/notes/{escape(cite["note"])}?q={quote(cite["quote"])}">'
        f"{escape(vault.note(cite['note']).title)}</a>"
        f"<blockquote>{escape(cite['quote'])}</blockquote>"
        f'<span class="src">{escape(vault.note(cite["note"]).source)}</span></li>'
        for cite in citations(state, card_id)
    )
    if not rows:
        return ""
    return (
        f'<div class="receipt cited"><h4>{CHECK}Answered from the brain</h4>'
        f"<p>{escape(plain(answer)[:600])}</p>"
        f'<ol class="cites">{rows}</ol></div>'
    )


def calls_html(rows: list[dict]) -> str:
    body = (
        "".join(
            f'<tr class="{"" if call["allowed"] else "no"}"><td>{escape(call["by"])}</td>'
            f"<td><code>{escape(call['tool'])}</code></td>"
            f'<td class="args">{escape(", ".join(call["args"].values()))}</td>'
            f"<td>{chips(['allowed'], 'green') if call['allowed'] else chips(['refused'], 'red')}</td>"
            f'<td class="said">{escape(call["said"].splitlines()[0] if call["said"] else "")}</td></tr>'
            for call in rows
        )
        or '<tr><td colspan="5">No calls yet.</td></tr>'
    )
    return (
        '<h2 class="sec" id="calls">Brain calls</h2><p class="sub">Every call made to the brain, in order, '
        "and who made it. A refused citation is one the brain could not find in a note.</p>"
        '<table class="tbl calls"><thead><tr><th>Who</th><th>Tool</th><th>With</th><th>Brain</th>'
        f"<th>Said</th></tr></thead><tbody>{body}</tbody></table>"
    )


def answer_form_html(card_id: str) -> str:
    return (
        '<div class="receipt open"><h4>No note holds this</h4>'
        "<p>Answer it for the client, and say where the answer came from. It is filed as a note.</p>"
        f'<form method="post" action="/cards/{escape(card_id)}/answer" class="decide answer">'
        '<textarea name="answer" rows="3" placeholder="The answer"></textarea>'
        '<input name="source" placeholder="Where it came from">'
        '<button class="btn dark">File the answer</button></form></div>'
    )


def card_page(state: Path, card_id: str, vault: Vault, person: Person) -> str:
    card = Board.open(state / "board.json").card(card_id)
    # The board's own receipt shows chips only; the brain's shows each quote, so it replaces it.
    shown = detail(replace(card, sources=[]), None, workspace=WORKSPACE, signed_in=signed_in_html(person))
    extra = cited_html(vault, state, card.card_id, card.answer)
    if card.column == "Needs you":
        extra += answer_form_html(card.card_id)
    extra += calls_html(calls(state, card.card_id))
    return with_css(shown.replace('<h2 class="sec">Runs</h2>', extra + '<h2 class="sec">Runs</h2>', 1))


def note_page(vault: Vault, name: str, mark: str, person: Person) -> str:
    note = vault.note(name)
    wanted = flat(mark)
    facts = "".join(
        f"<li{' class=hit' if wanted and wanted in fact else ''}>{escape(fact)}</li>" for fact in note.facts
    )
    live = (
        '<div class="wrap tight detail note"><p class="eyebrow">Brain note</p>'
        f'<h1 class="page">{escape(note.title)}</h1>'
        f'<div class="refs">{chips([note.source])}{chips([note.date], "blue")}</div>'
        f'<h2 class="sec">Facts</h2>'
        f'<p class="sub">Each line is quoted word for word from {escape(note.source)}.</p>'
        f'<ul class="facts">{facts}</ul></div>'
    )
    return with_css(page(note.title, live, "", workspace=WORKSPACE, signed_in=signed_in_html(person)))


def with_css(text: str) -> str:
    return text.replace("</head>", '<link rel="stylesheet" href="/brain.css"></head>', 1)


def app(state: Path, roster: tuple[Role, ...], people, brain: Path = BRAIN) -> web.Application:
    vault = Vault(brain)

    async def index(request: web.Request) -> web.Response:
        board = render(
            Board.open(state / "board.json"), workspace=WORKSPACE, signed_in=signed_in_html(request[PERSON])
        )
        return web.Response(text=with_css(board), content_type="text/html")

    async def one(request: web.Request) -> web.Response:
        try:
            text = card_page(state, request.match_info["id"], vault, request[PERSON])
        except StopIteration:
            raise web.HTTPNotFound() from None
        return web.Response(text=text, content_type="text/html")

    async def answer(request: web.Request) -> web.Response:
        """File the signed-in person's answer; the vault refuses anyone but the consultant."""
        from brain.answerer import answer_for_client

        form = await request.post()
        card_id = request.match_info["id"]
        try:
            answer_for_client(
                state,
                card_id,
                str(form.get("answer", "")),
                str(form.get("source", "")),
                request[PERSON].name,
                brain,
            )
        except Refused:
            # The refusal is already in the call log, so the card page shows it.
            pass
        except StopIteration:
            raise web.HTTPNotFound() from None
        raise web.HTTPSeeOther(f"/cards/{card_id}")

    async def note(request: web.Request) -> web.Response:
        try:
            text = note_page(vault, request.match_info["name"], request.query.get("q", ""), request[PERSON])
        except Refused:
            raise web.HTTPNotFound() from None
        return web.Response(text=text, content_type="text/html")

    async def roster_page(request: web.Request) -> web.Response:
        team_page = team(
            Board.open(state / "board.json"),
            roster,
            workspace=WORKSPACE,
            signed_in=signed_in_html(request[PERSON]),
        )
        return web.Response(text=with_css(team_page), content_type="text/html")

    async def css(_: web.Request) -> web.FileResponse:
        return web.FileResponse(CSS)

    web_app = web.Application()
    require_sign_in(web_app, people, WORKSPACE, ("/brain.css",))
    web_app.router.add_get("/", index)
    web_app.router.add_get("/team", roster_page)
    web_app.router.add_get("/cards/{id}", one)
    web_app.router.add_post("/cards/{id}/answer", answer)
    web_app.router.add_get("/notes/{name}", note)
    web_app.router.add_get("/brain.css", css)
    web_app.router.add_static("/static", WEB)
    web_app.router.add_static("/fonts", WEB / "fonts")
    return web_app


def serve(state: Path, roster: tuple[Role, ...], port: int) -> None:
    web.run_app(app(state, roster, people_or_exit(PEOPLE_FILE)), host="127.0.0.1", port=port)
