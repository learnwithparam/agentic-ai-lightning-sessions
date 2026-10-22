"""Mission Control's board, with what evals add: the swap decision above the lanes, one card per case.

A card is one labelled inquiry. It is Done when both production and the candidate pass it, and waits for
Dr. Okafor when either fails. Its two runs are production's and the candidate's, each listing its
checks. Above the lanes, the two runs side by side and the gate's answer to the swap.
"""

from __future__ import annotations

import json
from pathlib import Path

from aiohttp import web

from evals.desk import DESK, INQUIRIES
from evals.judge import TRUST
from evals.score import SAID
from evals.testset import LABELS
from workforce.board import WEB, Board, Card, E, Role, Run, detail, render, team, usd

CSS = Path(__file__).resolve().parent.parent / "web" / "evals.css"
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
    bad = [c for c in row["checks"] if not c["ok"]]
    return f"{bad[0]['name']}: {bad[0]['said']}" if bad else "passed"


def passed(row: dict) -> bool:
    return all(c["ok"] for c in row["checks"])


def cards(report: dict) -> list[Card]:
    by_id = {q["id"]: q for q in INQUIRIES}
    rows = {name: {r["id"]: r for r in runs} for name, runs in report["runs"].items()}
    out = []
    for i in LABELS:
        q, prod, cand = by_id[i], rows["production"].get(i), rows["candidate"].get(i)
        runs = [
            Run(
                NAMES[name],
                r["record"]["tokens_in"],
                r["record"]["tokens_out"],
                r["record"]["cost"],
                passed(r),
                r["record"]["text"],
                [f"{c['name']}: {c['said']}" for c in r["checks"]],
                [f"§{s}" for s in r["record"]["sections"]],
            )
            for name, r in (("production", prod), ("candidate", cand))
            if r
        ]
        ok = all(r.ok for r in runs) and len(runs) == 2
        card = Card(i, q["question"], q["sender"], i, "Done" if ok else "Needs you", DESK, runs=runs)
        card.log = [f"{NAMES[n]}: {summary(r)}" for n, r in (("production", prod), ("candidate", cand)) if r]
        if ok and prod["record"]["route"] == "answer":
            card.answer, card.sources = prod["record"]["text"], [f"§{s}" for s in prod["record"]["sections"]]
        out.append(card)
    return out


def swap_html(report: dict) -> str:
    rows = ""
    for name, runs in report["runs"].items():
        missed = sum(c["critical"] and not c["ok"] for r in runs for c in r["checks"])
        cost = sum(r["record"]["cost"] for r in runs) / max(len(runs), 1)
        rows += (
            f"<tr><td>{NAMES[name]}</td><td class=n>{sum(passed(r) for r in runs)} of {len(runs)}</td>"
            f'<td class="n{" bad" if missed else ""}">{missed}</td>'
            f"<td class=n>{usd(cost)}</td><td class=n>{usd(cost * 1000)}</td></tr>"
        )
    cal, swap = report["calibration"], report["swap"]
    judge = "counted" if cal["agreed"] / max(cal["total"], 1) >= TRUST else "shown, not counted"
    verdict = (
        '<div class="verdict ok"><b>Swap approved</b><span>No missed side effect, and no fewer passes.</span>'
        if swap["ok"]
        else '<div class="verdict no"><b>Swap refused</b>'
        + "".join(f"<span>{E(r)}</span>" for r in swap["reasons"])
    )
    return (
        '<h2 class="sec" id="evals">Evals before the swap</h2><p class="sub">'
        f"{len(report['runs']['production'])} production runs from {E(report['source'])}, "
        "each labelled by Dr. Amara Okafor, rerun on the candidate. The judge agrees with her grades on "
        f"{cal['agreed']} of {cal['total']}, so it is {judge}.</p>"
        '<table class="tbl swap"><thead><tr><th>Run</th><th class=n>Cases passed</th>'
        "<th class=n>Missed side effects</th><th class=n>Cost per inquiry</th>"
        "<th class=n>Per 1,000 inquiries</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>{verdict}</div>"
    )


def label_html(card_id: str) -> str:
    label = LABELS[card_id]
    want = SAID[label["route"]] + (f", citing §{label['section']}" if label.get("section") else "")
    return (
        f'<div class="receipt label"><h4>Labelled by Dr. Amara Okafor</h4><p>Should be {E(want)}.</p></div>'
    )


def report_of(state: Path) -> dict:
    f = state / "report.json"
    if not f.exists():
        raise web.HTTPNotFound(text="No evals yet. Run make demo first.")
    return json.loads(f.read_text())


def board_page(state: Path) -> str:
    report = report_of(state)
    page = render(Board(state / "board.json", cards(report)))
    return with_css(page.replace('<div class="lanes">', swap_html(report) + '<div class="lanes">', 1))


def card_page(state: Path, card_id: str) -> str:
    card = next(c for c in cards(report_of(state)) if c.id == card_id)
    page = detail(card, None)
    return with_css(
        page.replace('<h2 class="sec">Runs</h2>', label_html(card_id) + '<h2 class="sec">Runs</h2>', 1)
    )


def with_css(page: str) -> str:
    return page.replace("</head>", '<link rel="stylesheet" href="/evals.css"></head>', 1)


def app(state: Path) -> web.Application:
    async def index(_: web.Request) -> web.Response:
        return web.Response(text=board_page(state), content_type="text/html")

    async def one(req: web.Request) -> web.Response:
        try:
            return web.Response(text=card_page(state, req.match_info["id"]), content_type="text/html")
        except StopIteration:
            raise web.HTTPNotFound() from None

    async def roster(_: web.Request) -> web.Response:
        board = Board(state / "board.json", cards(report_of(state)))
        return web.Response(text=with_css(team(board, TEAM)), content_type="text/html")

    async def css(_: web.Request) -> web.FileResponse:
        return web.FileResponse(CSS)

    a = web.Application()
    a.router.add_get("/", index)
    a.router.add_get("/cards/{id}", one)
    a.router.add_get("/team", roster)
    a.router.add_get("/evals.css", css)
    a.router.add_static("/static", WEB)
    a.router.add_static("/fonts", WEB / "fonts")
    return a


def serve(state: Path, port: int = 8094) -> None:
    web.run_app(app(state), host="127.0.0.1", port=port)
