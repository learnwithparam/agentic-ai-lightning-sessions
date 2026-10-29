"""Mission Control's board, with what evals add: the swap decision above the lanes, one card per case.

A card is one labelled inquiry. It is Done when both production and the candidate pass it, and waits for
Dr. Okafor when either fails. Its two runs are production's and the candidate's, each listing its
checks. Above the lanes, the two runs side by side and the gate's answer to the swap. A swap that passes
becomes card S-1, which only Dr. Okafor, signed in, can sign.
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime
from pathlib import Path

from aiohttp import web

from evals.desk import DESK, INQUIRIES
from evals.judge import TRUST
from evals.production import SIGNER, Refused, read_decisions, switch, switched
from evals.score import SAID
from evals.testset import LABELS
from workforce.board import (
    WEB,
    Board,
    Card,
    E,
    Role,
    Run,
    detail,
    is_blank,
    render,
    team,
    usd,
)
from workforce.people import (
    PERSON,
    People,
    Person,
    people_or_exit,
    require_sign_in,
    signed_in_html,
)

ROOT = Path(__file__).resolve().parent.parent
CSS = ROOT / "web" / "evals.css"
PEOPLE_FILE = ROOT / "fixtures" / "people.json"
SWAP_ID = "S-1"
PROPOSER = "Jonas Weber"
WORKSPACE = "KaaS Pharma"
TEAM = (
    Role(
        DESK,
        "An answer from the approved label that cites its section, or a side effect sent to safety",
        "Cases passed on the test set built from its own production runs",
        "Answer a side effect report instead of sending it to the safety team",
        ("search_approved", "answer", "send_to_safety", "decline"),
    ),
)
NAMES = {"production": "Production", "candidate": "Candidate"}


def summary(row: dict) -> str:
    failed = [check for check in row["checks"] if not check["passed"]]
    return f"{failed[0]['name']}: {failed[0]['said']}" if failed else "passed"


def passed(row: dict) -> bool:
    return all(check["passed"] for check in row["checks"])


def run_of(name: str, row: dict) -> Run:
    record = row["record"]
    return Run(
        NAMES[name],
        record["tokens_in"],
        record["tokens_out"],
        record["cost"],
        passed(row),
        record["text"],
        [f"{check['name']}: {check['said']}" for check in row["checks"]],
        [f"§{section}" for section in record["sections"]],
    )


def case_cards(report: dict) -> list[Card]:
    by_id = {inquiry["id"]: inquiry for inquiry in INQUIRIES}
    rows = {
        name: {row["id"]: row for row in found}
        for name, found in report["runs"].items()
    }
    out = []
    for case_id in LABELS:
        inquiry = by_id[case_id]
        pair = [
            (name, rows[name][case_id])
            for name in ("production", "candidate")
            if case_id in rows.get(name, {})
        ]
        runs = [run_of(name, row) for name, row in pair]
        done = len(runs) == 2 and all(run.ok for run in runs)
        card = Card(
            case_id,
            inquiry["question"],
            inquiry["sender"],
            case_id,
            "Done" if done else "Needs you",
            DESK,
            runs=runs,
        )
        card.log = [f"{NAMES[name]}: {summary(row)}" for name, row in pair]
        production = rows["production"].get(case_id)
        if done and production["record"]["route"] == "answer":
            card.answer = production["record"]["text"]
            card.sources = [
                f"§{section}" for section in production["record"]["sections"]
            ]
        out.append(card)
    return out


def swap_card(report: dict, state: Path) -> Card | None:
    """The signature the passed gate asks for: waiting until production holds the candidate, then Done."""
    if not (report.get("swap") or {}).get("approved"):
        return None
    signed = switched(state)
    card = Card(
        SWAP_ID,
        "Switch production to the candidate?",
        PROPOSER,
        SWAP_ID,
        "Done" if signed else "Needs you",
        DESK,
    )
    card.log = [
        "The gate passed the candidate: no missed side effect, and no fewer passes."
    ]
    if signed:
        card.log.append(f"Production switched; signed by {signed['signed_by']}")
    return card


def cards(report: dict, state: Path) -> list[Card]:
    swap = swap_card(report, state)
    return ([swap] if swap else []) + case_cards(report)


def swap_html(report: dict) -> str:
    runs = report["runs"]
    if "candidate" not in runs:
        return '<h2 class="sec" id="evals">Evals before the swap</h2><p class="sub">The candidate is still running.</p>'
    rows = ""
    for name, found in runs.items():
        missed = sum(
            check["critical"] and not check["passed"]
            for row in found
            for check in row["checks"]
        )
        cost = sum(row["record"]["cost"] for row in found) / max(len(found), 1)
        rows += (
            f"<tr><td>{NAMES[name]}</td><td class=n>{sum(passed(row) for row in found)} of {len(found)}</td>"
            f'<td class="n{" bad" if missed else ""}">{missed}</td>'
            f"<td class=n>{usd(cost)}</td><td class=n>{usd(cost * 1000)}</td></tr>"
        )
    calibration, swap = report["calibration"], report["swap"]
    judge = (
        "counted"
        if calibration["agreed"] / max(calibration["total"], 1) >= TRUST
        else "shown, not counted"
    )
    verdict = (
        '<div class="verdict ok"><b>Swap approved</b><span>No missed side effect, and no fewer passes.</span>'
        if swap["approved"]
        else '<div class="verdict no"><b>Swap refused</b>'
        + "".join(f"<span>{E(reason)}</span>" for reason in swap["reasons"])
    )
    return (
        '<h2 class="sec" id="evals">Evals before the swap</h2><p class="sub">'
        f"{len(runs['production'])} production runs from {E(report['source'])}, "
        "each labelled by Dr. Amara Okafor, rerun on the candidate. The judge agrees with her grades on "
        f"{calibration['agreed']} of {calibration['total']}, so it is {judge}.</p>"
        '<table class="tbl swap"><thead><tr><th>Run</th><th class=n>Cases passed</th>'
        "<th class=n>Missed side effects</th><th class=n>Cost per inquiry</th>"
        "<th class=n>Per 1,000 inquiries</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>{verdict}</div>"
    )


def label_html(card_id: str) -> str:
    label = LABELS[card_id]
    want = SAID[label["route"]] + (
        f", citing §{label['section']}" if label.get("section") else ""
    )
    return f'<div class="receipt label"><h4>Labelled by Dr. Amara Okafor</h4><p>Should be {E(want)}.</p></div>'


def decisions_html(state: Path) -> str:
    rows = (
        "".join(
            f'<tr class="{"no" if row["decision"] == "refused" else ""}"><td><b>{E(row["by"])}</b></td>'
            f"<td>{E(row['decision'])}</td><td>{E(row['said'])}</td></tr>"
            for row in read_decisions(state)
        )
        or '<tr><td colspan="3">No decisions yet.</td></tr>'
    )
    return (
        '<h2 class="sec" id="decisions">Swap decisions</h2><p class="sub">Every attempt to sign the swap, in order. '
        "A refused attempt changed nothing in production.</p>"
        '<table class="tbl decisions"><thead><tr><th>Who</th><th>Decision</th><th>Said</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )


def sign_form_html() -> str:
    return (
        f'<form method="post" action="/cards/{SWAP_ID}/sign" class="decide">'
        f"<p>The gate passed this candidate. Only {E(SIGNER)} can switch production to it.</p>"
        '<button name="decision" value="sign">Sign the swap</button></form>'
    )


def report_of(state: Path) -> dict:
    report_file = state / "report.json"
    if is_blank(report_file):
        raise web.HTTPNotFound(text="No evals yet. Run make demo first.")
    return json.loads(report_file.read_text())


def signed_in(person: Person | None) -> str:
    return signed_in_html(person) if person else ""


def board_page(state: Path, person: Person | None = None) -> str:
    report = report_of(state)
    board = Board(state / "board.json", cards(report, state))
    page = render(board, workspace=WORKSPACE, signed_in=signed_in(person))
    return with_css(
        page.replace(
            '<div class="lanes">', swap_html(report) + '<div class="lanes">', 1
        )
    )


def card_page(state: Path, card_id: str, person: Person | None = None) -> str:
    report = report_of(state)
    card = next(found for found in cards(report, state) if found.id == card_id)
    page = detail(card, None, workspace=WORKSPACE, signed_in=signed_in(person))
    if card_id == SWAP_ID:
        waiting = card.column == "Needs you" and person is not None
        extra = (sign_form_html() if waiting else "") + decisions_html(state)
    else:
        extra = label_html(card_id)
    return with_css(
        page.replace(
            '<h2 class="sec">Runs</h2>', extra + '<h2 class="sec">Runs</h2>', 1
        )
    )


def with_css(page: str) -> str:
    return page.replace(
        "</head>", '<link rel="stylesheet" href="/evals.css"></head>', 1
    )


class BoardPages:
    """The board's routes. Who is acting always comes from the signed cookie, never from a form."""

    def __init__(self, state: Path):
        self.state = state

    async def index(self, request: web.Request) -> web.Response:
        return web.Response(
            text=board_page(self.state, request[PERSON]), content_type="text/html"
        )

    async def one(self, request: web.Request) -> web.Response:
        try:
            page = card_page(self.state, request.match_info["id"], request[PERSON])
        except StopIteration:
            raise web.HTTPNotFound() from None
        return web.Response(text=page, content_type="text/html")

    async def roster(self, request: web.Request) -> web.Response:
        board = Board(
            self.state / "board.json", cards(report_of(self.state), self.state)
        )
        page = team(
            board, TEAM, workspace=WORKSPACE, signed_in=signed_in(request[PERSON])
        )
        return web.Response(text=with_css(page), content_type="text/html")

    async def sign(self, request: web.Request) -> web.Response:
        """Switch production to the candidate as the signed-in person; the swap rules decide."""
        person: Person = request[PERSON]
        report = report_of(self.state)
        # switch() already logged a refusal; the card page shows it.
        with contextlib.suppress(Refused):
            switch(self.state, report, person.name, datetime.now())
        raise web.HTTPSeeOther(f"/cards/{SWAP_ID}")

    async def css(self, _: web.Request) -> web.FileResponse:
        return web.FileResponse(CSS)


def app(state: Path, people: People) -> web.Application:
    pages = BoardPages(state)
    web_app = web.Application()
    require_sign_in(web_app, people, WORKSPACE, ("/evals.css",), ("/evals.css",))
    web_app.router.add_get("/", pages.index)
    web_app.router.add_get("/cards/{id}", pages.one)
    web_app.router.add_post(f"/cards/{SWAP_ID}/sign", pages.sign)
    web_app.router.add_get("/team", pages.roster)
    web_app.router.add_get("/evals.css", pages.css)
    web_app.router.add_static("/static", WEB)
    web_app.router.add_static("/fonts", WEB / "fonts")
    return web_app


def serve(state: Path, port: int = 8094) -> None:
    web.run_app(app(state, people_or_exit(PEOPLE_FILE)), host="127.0.0.1", port=port)
