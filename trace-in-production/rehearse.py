"""Drive the running board as real people: Erik is refused, Ingrid continues the paused shift to Done.

Run `make shift BUDGET_USD=0.0006 KILL_AT=none` and `make board` first, then `make rehearse`. The
passphrases come from the environment the make target loads, so they never appear on a command line.
"""

import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

BOARD = "http://127.0.0.1:8095"
CARD = "P-1"
LANGFUSE = os.environ.get("LANGFUSE_HOST", "http://127.0.0.1:8270")
ALERTS_AFTER_PAUSE = ("A-04", "A-05", "A-06", "A-07")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


def sign_in(name: str, passphrase: str):
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect)
    body = urllib.parse.urlencode({"name": name, "passphrase": passphrase}).encode()
    try:
        opener.open(f"{BOARD}/login", body)
    except urllib.error.HTTPError as redirect:
        assert redirect.code == 303, f"{name} sign-in answered {redirect.code}"
    assert any(cookie.name == "board_session" for cookie in jar), f"{name}: no cookie"
    return opener


def get(opener, path: str) -> str:
    return opener.open(f"{BOARD}{path}").read().decode()


def press_continue(opener) -> None:
    try:
        opener.open(f"{BOARD}/cards/{CARD}/continue", b"")
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
    body = urllib.parse.urlencode({"name": "Ingrid Halvorsen", "passphrase": "wrong"}).encode()
    try:
        urllib.request.build_opener(NoRedirect).open(f"{BOARD}/login", body)
    except urllib.error.HTTPError as refusal:
        return refusal.code == 401
    return False


def langfuse_is_up() -> bool:
    try:
        urllib.request.urlopen(f"{LANGFUSE}/api/public/health", timeout=3).close()  # noqa: S310
    except OSError:
        return False
    return True


def main() -> None:
    if not langfuse_is_up():
        sys.exit("Langfuse is not running: make -C ../mission-control stack")
    erik = sign_in("Erik Lund", os.environ["BOARD_PASSPHRASE_ERIK_LUND"])
    ingrid = sign_in("Ingrid Halvorsen", os.environ["BOARD_PASSPHRASE_INGRID_HALVORSEN"])
    check("a wrong passphrase is not signed in", wrong_passphrase_refused())
    waiting = text_of(get(erik, f"/cards/{CARD}"))
    check("the shift waits for Ingrid", "The shift is paused" in waiting)
    press_continue(erik)
    after_erik = text_of(get(erik, f"/cards/{CARD}"))
    check("Erik's approval is refused", "The shift is paused" in after_erik)
    check("the refusal is in the call log", "only Ingrid Halvorsen can raise the shift budget" in after_erik)
    press_continue(ingrid)
    for _ in range(90):
        final = text_of(get(ingrid, f"/cards/{CARD}"))
        if "approved by Ingrid Halvorsen" in final:
            break
        time.sleep(2)
    check("Ingrid's approval is on the card", "approved by Ingrid Halvorsen" in final)
    for _ in range(90):
        board_page = get(ingrid, "/")
        if all(lane_holds(board_page, "Done", alert) for alert in ALERTS_AFTER_PAUSE):
            break
        time.sleep(2)
    check("the card is in the Done lane", lane_holds(board_page, "Done", CARD))
    check(
        "only the paused shift resumed",
        all(lane_holds(board_page, "Done", alert) for alert in ALERTS_AFTER_PAUSE),
    )


if __name__ == "__main__":
    main()
