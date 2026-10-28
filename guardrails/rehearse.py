"""Drive the running board as real people: Maya is refused, Sam rejects the flagged CV, the card reaches Done.

Run `make demo` and `make board` first, then `make rehearse`. The passphrases come from the environment
the make target loads, so they never appear on a command line.
"""

import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

BOARD = "http://127.0.0.1:8091"
CARD = "C-5"
AGE_CARD = "C-6"
FLAGGED = "CAND-204"


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


def decide(opener, decision: str) -> None:
    body = urllib.parse.urlencode({"candidate": FLAGGED, "decision": decision}).encode()
    try:
        opener.open(f"{BOARD}/cards/{CARD}/review", body)
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


def wrong_passphrase_refused() -> bool:
    body = urllib.parse.urlencode({"name": "Sam Patel", "passphrase": "wrong"}).encode()
    try:
        urllib.request.build_opener(NoRedirect).open(f"{BOARD}/login", body)
    except urllib.error.HTTPError as refusal:
        return refusal.code == 401
    return False


def main() -> None:
    maya = sign_in("Maya Okafor", os.environ["BOARD_PASSPHRASE_MAYA_OKAFOR"])
    sam = sign_in("Sam Patel", os.environ["BOARD_PASSPHRASE_SAM_PATEL"])
    check("a wrong passphrase is not signed in", wrong_passphrase_refused())
    check("the card waits in Needs you", lane_holds_card(get(maya, "/"), "Needs you"))
    card = text_of(get(maya, f"/cards/{CARD}"))
    check("the card page shows the withheld line", "withheld from the screener" in card)
    age_card = text_of(get(maya, f"/cards/{AGE_CARD}"))
    check("the age request stopped at no cost", "screens by age" in age_card and "$0.0000" in age_card)
    decide(maya, "reject")
    after_maya = text_of(get(maya, f"/cards/{CARD}"))
    check("Maya's review is refused", "only the recruiter reviews a flagged CV" in after_maya)
    check("the refusal is in the audit log", "review refused" in after_maya)
    decide(sam, "reject")
    after_sam = text_of(get(sam, f"/cards/{CARD}"))
    check("Sam's decision is on the card", "Sam Patel rejected the application" in after_sam)
    in_done = False
    for _ in range(30):
        in_done = lane_holds_card(get(sam, "/"), "Done")
        if in_done:
            break
        time.sleep(1)
    check("the card is in the Done lane", in_done)
    final = text_of(get(sam, f"/cards/{CARD}"))
    check("the shortlist went to Maya Okafor", "Sent to Maya Okafor" in final)


if __name__ == "__main__":
    main()
