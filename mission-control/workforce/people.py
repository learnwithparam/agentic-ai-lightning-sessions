"""The people who may sign in to a board, and the signed cookie that remembers who they are.

A person's name and role come from the session's people file. Their passphrase comes from the
environment variable the file names, so no secret is ever committed. Production swaps this module for
the company's single sign-on (Okta, Entra ID or Keycloak); the board only ever asks "who is signed in?".
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web

COOKIE_NAME = "board_session"
PUBLIC_FOLDERS = ("/static/", "/fonts/")
PERSON = web.RequestKey("person", object)
escape = html.escape


@dataclass(frozen=True)
class Person:
    name: str
    role: str
    user: str = ""


def scrypt(passphrase: str, salt: bytes) -> bytes:
    return hashlib.scrypt(passphrase.encode(), salt=salt, n=2**14, r=8, p=1)


class People:
    def __init__(self, people_file: Path, secret: str) -> None:
        self.secret = secret.encode()
        self.salt = os.urandom(16)
        self.persons: dict[str, Person] = {}
        self.passphrase_hashes: dict[str, bytes] = {}
        for entry in json.loads(people_file.read_text()):
            passphrase = os.environ.get(entry["passphrase_env"], "")
            if not passphrase:
                continue
            self.persons[entry["name"]] = Person(entry["name"], entry["role"], entry.get("user", ""))
            self.passphrase_hashes[entry["name"]] = scrypt(passphrase, self.salt)

    def names(self) -> list[str]:
        return list(self.persons)

    def sign_in(self, name: str, passphrase: str) -> Person | None:
        expected_hash = self.passphrase_hashes.get(name)
        if expected_hash is None:
            return None
        if not hmac.compare_digest(expected_hash, scrypt(passphrase, self.salt)):
            return None
        return self.persons[name]

    def sign_cookie(self, person: Person) -> str:
        signature = hmac.new(self.secret, person.name.encode(), hashlib.sha256).hexdigest()
        return f"{person.name}|{signature}"

    def person_in_cookie(self, cookie: str | None) -> Person | None:
        if not cookie or "|" not in cookie:
            return None
        name, signature = cookie.rsplit("|", 1)
        expected_signature = hmac.new(self.secret, name.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected_signature):
            return None
        return self.persons.get(name)


def people_or_exit(people_file: Path) -> People:
    secret = os.environ.get("BOARD_SECRET", "")
    people = People(people_file, secret)
    if not secret or not people.names():
        sys.exit("Set BOARD_SECRET and a BOARD_PASSPHRASE_* for each person in fixtures/people.json.")
    return people


def signed_in_person(request: web.Request) -> Person:
    return request[PERSON]


def signed_in_html(person: Person) -> str:
    return (
        f'<span class="signed-in">Signed in as <b>{escape(person.name)}</b>, {escape(person.role)}</span>'
        '<form method="post" action="/logout" class="sign-out"><button>Sign out</button></form>'
    )


def login_page(people: People, workspace: str, stylesheets: tuple[str, ...] = (), problem: str = "") -> str:
    options = "".join(f"<option>{escape(name)}</option>" for name in people.names())
    complaint = f'<p class="block">{escape(problem)}</p>' if problem else ""
    extra = "".join(f'<link rel="stylesheet" href="{escape(sheet)}">' for sheet in stylesheets)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sign in</title>
<link rel="stylesheet" href="/static/base.css"><link rel="stylesheet" href="/static/board.css">
{extra}</head><body><main class="login">
<h1 class="page">Sign in to {escape(workspace)}</h1>{complaint}
<form method="post" action="/login"><label>Who are you<select name="name">{options}</select></label>
<label>Passphrase<input type="password" name="passphrase" autocomplete="current-password"></label>
<button>Sign in</button></form></main></body></html>"""


def html_response(text: str, status: int = 200) -> web.Response:
    return web.Response(text=text, content_type="text/html", status=status)


def require_sign_in(
    web_app: web.Application,
    people: People,
    workspace: str,
    stylesheets: tuple[str, ...] = (),
    public_paths: tuple[str, ...] = (),
) -> None:
    """Sign-in for a board: every page needs a signed cookie, and who is acting always comes from it."""
    public = ("/login", *public_paths)

    @web.middleware
    async def middleware(request: web.Request, handler):
        person = people.person_in_cookie(request.cookies.get(COOKIE_NAME))
        request[PERSON] = person
        is_public = request.path in public or request.path.startswith(PUBLIC_FOLDERS)
        if person is None and not is_public:
            raise web.HTTPSeeOther("/login")
        return await handler(request)

    async def login_form(_: web.Request) -> web.Response:
        return html_response(login_page(people, workspace, stylesheets))

    async def login(request: web.Request) -> web.Response:
        form = await request.post()
        name, passphrase = str(form.get("name", "")), str(form.get("passphrase", ""))
        person = people.sign_in(name, passphrase)
        if person is None:
            problem = "That name and passphrase do not match."
            return html_response(login_page(people, workspace, stylesheets, problem), status=401)
        response = web.HTTPSeeOther("/")
        response.set_cookie(COOKIE_NAME, people.sign_cookie(person), httponly=True, samesite="Strict")
        raise response

    async def logout(_: web.Request) -> web.Response:
        response = web.HTTPSeeOther("/login")
        response.del_cookie(COOKIE_NAME)
        raise response

    web_app.middlewares.append(middleware)
    web_app.router.add_get("/login", login_form)
    web_app.router.add_post("/login", login)
    web_app.router.add_post("/logout", logout)
