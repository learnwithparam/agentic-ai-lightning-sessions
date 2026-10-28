"""Drive the running board as real people: Priya is refused, Dana approves, the card reaches Done.

Run `make demo SOLD_OUT=EW461` and `make board` first, then `make rehearse`. The passphrases come
from the environment the make target loads, so they never appear on a command line.
"""

import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

BOARD = "http://127.0.0.1:8090"
CARD = "C-3"


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


def approve(opener, decision: str = "approve") -> None:
    body = urllib.parse.urlencode({"decision": decision}).encode()
    try:
        opener.open(f"{BOARD}/cards/{CARD}/approve", body)
    except urllib.error.HTTPError as redirect:
        assert redirect.code == 303


def text_of(page: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))


def lane_holds_card(board_page: str, lane: str) -> bool:
    start = board_page.find(f'aria-label="{lane}"')
    end = board_page.find("</section>", start)
    return start != -1 and CARD in board_page[start:end]


def check(claim: str, holds: bool) -> None:
    print(("ok   " if holds else "FAIL ") + claim)
    if not holds:
        sys.exit(1)


def main() -> None:
    priya = sign_in("Priya Shah", os.environ["BOARD_PASSPHRASE_PRIYA"])
    dana = sign_in("Dana Ruiz", os.environ["BOARD_PASSPHRASE_DANA"])
    check("a wrong passphrase is not signed in", _wrong_passphrase_refused())
    waiting = text_of(get(priya, f"/cards/{CARD}"))
    check("the card waits for the manager", "waiting for the manager" in waiting)
    approve(priya)
    after_priya = text_of(get(priya, f"/cards/{CARD}"))
    check("Priya's approval is refused", "waiting for the manager" in after_priya)
    check("the refusal is in the call log", "only Dana Ruiz can" in after_priya)
    approve(dana)
    for _ in range(60):
        final = text_of(get(dana, f"/cards/{CARD}"))
        if "Approved by Dana Ruiz" in final or "Dana Ruiz, signed in" in final:
            break
        time.sleep(2)
    check("Dana's approval is on the ticket", "Dana Ruiz, signed in" in final)
    for _ in range(60):
        in_done = lane_holds_card(get(dana, "/"), "Done")
        if in_done:
            break
        time.sleep(2)
    final = text_of(get(dana, f"/cards/{CARD}"))
    check("the card is in the Done lane", in_done)
    check("the messenger told both people", "Priya Shah" in final and "Klaus" in final)
    check("the card is Done", "Done" in final)


def _wrong_passphrase_refused() -> bool:
    body = urllib.parse.urlencode({"name": "Dana Ruiz", "passphrase": "wrong"}).encode()
    try:
        urllib.request.build_opener(NoRedirect).open(f"{BOARD}/login", body)
    except urllib.error.HTTPError as refusal:
        return refusal.code == 401
    return False


if __name__ == "__main__":
    main()
