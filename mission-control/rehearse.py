"""Drive the running board as real people: Sam is refused, Param approves one reply and declines another.

Run `make demo FRESH=1` and `make board` first, then `make rehearse`. The passphrases come from the
environment the make target loads, so they never appear on a command line.
"""

import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

from workforce.issues import Issues
from workforce.mission import STATE

BOARD = os.environ.get("BOARD", "http://127.0.0.1:8080")
EXPORT = "C-1"
QUESTION = "C-2"
REFUND = "C-3"


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
    body = urllib.parse.urlencode({"name": "Param", "passphrase": "wrong"}).encode()
    try:
        urllib.request.build_opener(NoRedirect).open(f"{BOARD}/login", body)
    except urllib.error.HTTPError as refusal:
        return refusal.code == 401
    return False


def posted_on(issues: Issues, number: int) -> list[dict]:
    return [comment for comment in issues.comments(number) if comment.get("approved_by")]


def main() -> None:
    param = sign_in("Param", os.environ["BOARD_PASSPHRASE_PARAM"])
    sam = sign_in("Sam", os.environ["BOARD_PASSPHRASE_SAM"])
    issues = Issues.open(STATE / "github.json")
    check("a wrong passphrase is not signed in", wrong_passphrase_refused())
    waiting = get(param, f"/cards/{EXPORT}")
    check(
        "C-1 waits in Needs you with a cited reply",
        lane_holds(get(param, "/"), "Needs you", EXPORT) and "Rests on" in waiting,
    )
    decide(sam, EXPORT, True)
    check(
        "Sam's approval is refused",
        "only a support approver can post to a customer" in text_of(get(sam, f"/cards/{EXPORT}")),
    )
    check("the refusal posted nothing on GitHub", not posted_on(issues, 412))
    decide(param, EXPORT, True)
    check("Param's approval posts the reply", "Approved" in text_of(get(param, f"/cards/{EXPORT}")))
    check("GitHub shows the reply on GH#412", len(posted_on(issues, 412)) == 1)
    check("C-1 is in the Done lane", lane_holds(get(param, "/"), "Done", EXPORT))
    decide(param, REFUND, False)
    check("Param's decline of C-3 posts nothing", not posted_on(issues, 415))
    check(
        "C-3 is Done as declined",
        lane_holds(get(param, "/"), "Done", REFUND)
        and "declined by Param; nothing was posted" in text_of(get(param, f"/cards/{REFUND}")),
    )
    check(
        "C-2 is answered from sources the analyst read",
        lane_holds(get(param, "/"), "Done", QUESTION),
    )


if __name__ == "__main__":
    main()
