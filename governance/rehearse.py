"""Drive the running board as real people: Marta is refused, Ingrid and Lars sign, Marta says no.

Run `make demo` and `make board` first, then `make rehearse`. The passphrases come from the
environment the make target loads, so they never appear on a command line.
"""

import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

from operating import run

BOARD = os.environ.get("BOARD", "http://127.0.0.1:8096")
RATE = "J-02"
ORDER = "J-04"
SHIFT = "J-06"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


def sign_in(name: str, passphrase: str):
    jar = CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar), NoRedirect
    )
    body = urllib.parse.urlencode({"name": name, "passphrase": passphrase}).encode()
    try:
        opener.open(f"{BOARD}/login", body)
    except urllib.error.HTTPError as redirect:
        assert redirect.code == 303, f"{name} sign-in answered {redirect.code}"
    assert any(cookie.name == "board_session" for cookie in jar), f"{name}: no cookie"
    return opener


def get(opener, path: str) -> str:
    return opener.open(f"{BOARD}{path}").read().decode()


def decide(opener, card: str, yes: bool) -> None:
    body = urllib.parse.urlencode({"yes": "1" if yes else "0"}).encode()
    try:
        opener.open(f"{BOARD}/cards/{card}/decide", body)
    except urllib.error.HTTPError as redirect:
        assert redirect.code == 303


def text_of(page: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))


def lane_holds(board_page: str, lane: str, card: str) -> bool:
    start = board_page.find(f'aria-label="{lane}"')
    end = board_page.find("</section>", start)
    return start != -1 and card in board_page[start:end]


def check(claim: str, holds: bool) -> None:
    print(("ok   " if holds else "FAIL ") + claim)
    if not holds:
        sys.exit(1)


def wrong_passphrase_refused() -> bool:
    body = urllib.parse.urlencode(
        {"name": "Marta Berg", "passphrase": "wrong"}
    ).encode()
    try:
        urllib.request.build_opener(NoRedirect).open(f"{BOARD}/login", body)
    except urllib.error.HTTPError as refusal:
        return refusal.code == 401
    return False


def main() -> None:
    marta = sign_in("Marta Berg", os.environ["BOARD_PASSPHRASE_MARTA_BERG"])
    ingrid = sign_in("Ingrid Dahl", os.environ["BOARD_PASSPHRASE_INGRID_DAHL"])
    lars = sign_in("Lars Eide", os.environ["BOARD_PASSPHRASE_LARS_EIDE"])
    check("a wrong passphrase is not signed in", wrong_passphrase_refused())
    board_page = get(marta, "/")
    check(
        "three cards wait in Needs you",
        all(lane_holds(board_page, "Needs you", card) for card in (RATE, ORDER, SHIFT)),
    )
    decide(marta, RATE, True)
    after_marta = text_of(get(marta, f"/cards/{RATE}"))
    check(
        "Marta's approval of J-02 is refused",
        "only Ingrid Dahl signs a rate change over 15%" in after_marta,
    )
    check(
        "the refused approval wrote nothing",
        lane_holds(get(marta, "/"), "Needs you", RATE),
    )
    decide(marta, ORDER, False)
    check(
        "Marta's no on J-04 puts the card in Done",
        lane_holds(get(marta, "/"), "Done", ORDER)
        and "declined by Marta Berg; nothing was written"
        in text_of(get(marta, f"/cards/{ORDER}")),
    )
    writes = run.opened(run.STATE).systems.writes
    check(
        "the declined order was never written",
        not [write for write in writes if write["card"] == ORDER],
    )
    decide(ingrid, RATE, True)
    check(
        "Ingrid's approval writes EUR 119",
        "119" in text_of(get(ingrid, f"/cards/{RATE}")),
    )
    check("J-02 is in the Done lane", lane_holds(get(ingrid, "/"), "Done", RATE))
    decide(lars, SHIFT, True)
    check("Lars signs Aisha's shift on J-06", lane_holds(get(lars, "/"), "Done", SHIFT))
    check(
        "every write has exactly one decision",
        all(good for _, good in run.check(run.STATE)),
    )


if __name__ == "__main__":
    main()
