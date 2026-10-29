"""Drive the running board as real people: Jonas is refused, Dr. Okafor signs, production switches.

Run `make reset`, `make demo CANDIDATE=safe` and `make board` first, then `make rehearse`. The
passphrases come from the environment the make target loads, so they never appear on a command line.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

BOARD = "http://127.0.0.1:8094"
CARD = "S-1"
STATE = Path(__file__).resolve().parent / ".evals"


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


def sign(opener) -> None:
    body = urllib.parse.urlencode({"decision": "sign"}).encode()
    try:
        opener.open(f"{BOARD}/cards/{CARD}/sign", body)
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
    jonas = sign_in("Jonas Weber", os.environ["BOARD_PASSPHRASE_JONAS_WEBER"])
    amara = sign_in("Dr. Amara Okafor", os.environ["BOARD_PASSPHRASE_AMARA_OKAFOR"])
    check("a wrong passphrase is not signed in", _wrong_passphrase_refused())
    if "Switch production" not in text_of(get(jonas, "/")):
        sys.exit(
            "Run make demo CANDIDATE=safe first: the gate has not passed a candidate"
        )
    check("the swap waits for Dr. Okafor", not (STATE / "production.json").exists())
    sign(jonas)
    after_jonas = text_of(get(jonas, f"/cards/{CARD}"))
    check("Jonas's sign-off is refused", not (STATE / "production.json").exists())
    check(
        "the refusal is in the decisions log",
        "only Dr. Amara Okafor signs a model swap" in after_jonas,
    )
    sign(amara)
    final = text_of(get(amara, f"/cards/{CARD}"))
    signed = json.loads((STATE / "production.json").read_text())
    check(
        "Dr. Okafor's signature is on the swap", "signed by Dr. Amara Okafor" in final
    )
    check("the card is in the Done lane", lane_holds_card(get(amara, "/"), "Done"))
    check(
        "production now runs the candidate", signed["signed_by"] == "Dr. Amara Okafor"
    )


def _wrong_passphrase_refused() -> bool:
    body = urllib.parse.urlencode(
        {"name": "Jonas Weber", "passphrase": "wrong"}
    ).encode()
    try:
        urllib.request.build_opener(NoRedirect).open(f"{BOARD}/login", body)
    except urllib.error.HTTPError as refusal:
        return refusal.code == 401
    return False


if __name__ == "__main__":
    main()
