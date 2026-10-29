"""Drive the running board as real people: Ravi is refused, Hannah answers what no note holds.

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

BOARD = "http://127.0.0.1:8093"
CARD = "C-3"
ANSWER = "The 2027 IT budget is £1.2m, agreed with Marcus Bell by phone on 27 October."
SOURCE = "Phone call with Marcus Bell, 27 October 2026"


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


def answer(opener) -> None:
    body = urllib.parse.urlencode({"answer": ANSWER, "source": SOURCE}).encode()
    try:
        opener.open(f"{BOARD}/cards/{CARD}/answer", body)
    except urllib.error.HTTPError as error:
        assert error.code == 303, f"the answer returned {error.code}"


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
    body = urllib.parse.urlencode({"name": "Hannah Moreau", "passphrase": "wrong"}).encode()
    opener = urllib.request.build_opener(NoRedirect)
    try:
        opener.open(f"{BOARD}/login", body)
    except urllib.error.HTTPError as error:
        return error.code == 401
    return False


def main() -> None:
    ravi = sign_in("Ravi Shah", os.environ["BOARD_PASSPHRASE_RAVI_SHAH"])
    hannah = sign_in("Hannah Moreau", os.environ["BOARD_PASSPHRASE_HANNAH_MOREAU"])
    check("a wrong passphrase is not signed in", wrong_passphrase_refused())
    check("the card waits for Hannah", lane_holds_card(get(hannah, "/"), "Needs you"))
    answer(ravi)
    page = text_of(get(hannah, f"/cards/{CARD}"))
    check("Ravi's answer is refused", lane_holds_card(get(hannah, "/"), "Needs you"))
    check("the refusal is in the call log", "Ravi Shah may not answer for the client" in page)
    answer(hannah)
    check("Hannah's answer is filed", "Hannah Moreau" in text_of(get(hannah, f"/cards/{CARD}")))
    check("the card is in the Done lane", lane_holds_card(get(hannah, "/"), "Done"))
    check("the brain still checks", ANSWER in get(hannah, "/notes/answer-c-3"))


if __name__ == "__main__":
    main()
