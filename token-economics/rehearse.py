"""Drive the running board as real people: Leo is refused, Nina raises the hail claim's budget, it files.

Run `make demo PASS=1` and `make board` first, then `make rehearse`. The passphrases come from the
environment the make target loads, so they never appear on a command line.
"""

import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

BOARD = "http://127.0.0.1:8092"
CARD = "C-9"
FIRST_BOARD = "/pass/1"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


def sign_in(name: str, passphrase: str):
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect)
    body = urllib.parse.urlencode({"name": name, "passphrase": passphrase}).encode()
    try:
        opener.open(f"{BOARD}/login", body)
    except urllib.error.HTTPError as error:
        assert error.code == 303, f"sign-in answered {error.code}"
    assert any(cookie.name == "board_session" for cookie in jar), f"{name} was not signed in"
    return opener


def get(opener, path: str) -> str:
    return opener.open(f"{BOARD}{path}").read().decode()


def decide(opener, decision: str) -> None:
    body = urllib.parse.urlencode({"decision": decision}).encode()
    try:
        opener.open(f"{BOARD}{FIRST_BOARD}/cards/{CARD}/budget", body)
    except urllib.error.HTTPError as error:
        assert error.code == 303, f"the decision answered {error.code}"


def text_of(page: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))


def lane_holds_card(board_page: str, lane: str) -> bool:
    start = board_page.find(f'aria-label="{lane}"')
    if start < 0:
        return False
    return CARD in board_page[start : board_page.find("</section>", start)]


def check(claim: str, holds: bool) -> None:
    print(("ok   " if holds else "FAIL ") + claim)
    if not holds:
        sys.exit(1)


def wrong_passphrase_refused() -> bool:
    body = urllib.parse.urlencode({"name": "Nina Kowalski", "passphrase": "wrong"}).encode()
    opener = urllib.request.build_opener(NoRedirect)
    try:
        opener.open(f"{BOARD}/login", body)
    except urllib.error.HTTPError as error:
        return error.code == 401
    return False


def documents_read(card_page: str) -> int:
    ledger = card_page[card_page.find('id="ledger"') :]
    return len(re.findall(r"<tr><td>[\w.-]+\.(?:jpg|pdf)</td>", ledger))


def main() -> None:
    leo = sign_in("Leo Grant", os.environ["BOARD_PASSPHRASE_LEO_GRANT"])
    nina = sign_in("Nina Kowalski", os.environ["BOARD_PASSPHRASE_NINA_KOWALSKI"])
    check("a wrong passphrase is not signed in", wrong_passphrase_refused())
    check("the hail claim waits in Needs you", lane_holds_card(get(nina, FIRST_BOARD), "Needs you"))
    card = f"{FIRST_BOARD}/cards/{CARD}"
    check("the card asks to raise the budget", "Raise the budget?" in get(nina, card))
    decide(leo, "approve")
    check("Leo's approval is refused", "Raise the budget?" in get(nina, card))
    check(
        "the refusal is in the budget decisions", "Leo Grant is not the adjuster" in text_of(get(nina, card))
    )
    decide(nina, "approve")
    for _ in range(180):
        if lane_holds_card(get(nina, FIRST_BOARD), "Done"):
            break
        time.sleep(1)
    check("the card is in the Done lane", lane_holds_card(get(nina, FIRST_BOARD), "Done"))
    page = get(nina, card)
    check("the hail claim read all eight photos", documents_read(page) == 8)
    check("Nina's raise is on the ledger", "Nina Kowalski raised the budget" in text_of(page))
    small = [documents_read(get(nina, f"{FIRST_BOARD}/cards/{other}")) for other in ("C-7", "C-8")]
    check("the two small claims ran once", small == [3, 3])


if __name__ == "__main__":
    main()
