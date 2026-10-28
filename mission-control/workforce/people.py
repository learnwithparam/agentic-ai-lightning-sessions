"""The people who may sign in to a board, and the signed cookie that remembers who they are.

A person's name and role come from the session's people file. Their passphrase comes from the
environment variable the file names, so no secret is ever committed. Production swaps this module for
the company's single sign-on (Okta, Entra ID or Keycloak); the board only ever asks "who is signed in?".
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from pathlib import Path

COOKIE_NAME = "board_session"


@dataclass(frozen=True)
class Person:
    name: str
    role: str


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
            self.persons[entry["name"]] = Person(entry["name"], entry["role"])
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
