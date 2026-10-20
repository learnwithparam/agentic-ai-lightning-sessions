"""Who may do what: Keycloak signs each AI employee in as itself, on behalf of one person, and OpenFGA
decides whether that pair may touch a desk.

The identity in every check comes from a token Keycloak issued and introspected, never from a prompt, so
a model that claims to be someone else changes nothing. The model and its tuples are deploy/policy.json.
Without KEYCLOAK_URL and OPENFGA_URL there is no Access and the board runs as before.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

POLICY = Path(__file__).resolve().parent.parent / "deploy" / "policy.json"
# Which desk each guarded tool works on; a guarded tool missing here is refused, never let through.
DESKS = {
    "find_charges": "billing",
    "request_refund": "billing",
    "search_github": "engineering",
    "search_linear": "engineering",
    "search_docs": "engineering",
}

Check = Callable[[str, str, str], bool]


def http(url: str, body: dict | None = None, form: dict | None = None, auth: str = "") -> dict:
    data, kind = (
        (urllib.parse.urlencode(form), "application/x-www-form-urlencoded")
        if form
        else (json.dumps(body), "application/json")
    )
    headers = {"Content-Type": kind} | ({"Authorization": auth} if auth else {})
    req = urllib.request.Request(url, data=data.encode() if body or form else None, headers=headers)  # noqa: S310
    with urllib.request.urlopen(req, timeout=15) as res:  # noqa: S310 - local stack URLs from the env
        return json.load(res)


def client_id(agent: str) -> str:
    """'Support resolver' is the Keycloak client support-resolver."""
    return re.sub(r"[^a-z0-9]+", "-", agent.lower()).strip("-")


@dataclass(frozen=True)
class Badge:
    """What Keycloak says about one exchanged token: the employee holding it and the person it acts for."""

    agent: str
    user: str


@dataclass
class Keycloak:
    url: str
    realm: str = "acme"
    secrets: dict[str, str] = field(default_factory=dict)

    @property
    def base(self) -> str:
        return f"{self.url}/realms/{self.realm}/protocol/openid-connect"

    def sign_in(self, user: str, password: str) -> str:
        form = {
            "grant_type": "password",
            "client_id": "mission-control",
            "username": user,
            "password": password,
        }
        return http(f"{self.base}/token", form=form)["access_token"]

    def badge(self, user_token: str, agent: str) -> Badge:
        """Exchange the person's token for one the employee holds, then ask Keycloak who holds it."""
        cid = client_id(agent)
        creds = {"client_id": cid, "client_secret": self.secrets[cid]}
        token = http(
            f"{self.base}/token",
            form=creds
            | {
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "subject_token": user_token,
                "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            },
        )["access_token"]
        seen = http(f"{self.base}/token/introspect", form=creds | {"token": token})
        if not seen.get("active"):
            raise PermissionError(f"Keycloak did not accept {cid}'s token")
        return Badge(client_id(seen["azp"]), seen["preferred_username"])


@dataclass
class OpenFGA:
    url: str
    store: str = ""
    model: str = ""

    def setup(self, policy: Path = POLICY) -> OpenFGA:
        """Find or create the acme store, write the model and its tuples. Safe to run on every start."""
        spec = json.loads(policy.read_text())
        stores = http(f"{self.url}/stores")["stores"]
        self.store = (
            next((s["id"] for s in stores if s["name"] == "acme"), "")
            or http(f"{self.url}/stores", {"name": "acme"})["id"]
        )
        self.model = http(f"{self.url}/stores/{self.store}/authorization-models", spec["model"])[
            "authorization_model_id"
        ]
        http(
            f"{self.url}/stores/{self.store}/write",
            {
                "authorization_model_id": self.model,
                "writes": {"tuple_keys": spec["tuples"], "on_duplicate": "ignore"},
            },
        )
        return self

    def check(self, who: str, relation: str, obj: str) -> bool:
        body = {
            "authorization_model_id": self.model,
            "tuple_key": {"user": who, "relation": relation, "object": obj},
        }
        return http(f"{self.url}/stores/{self.store}/check", body)["allowed"]


@dataclass
class Access:
    """One person's session: their Keycloak token, a badge per employee, and the OpenFGA check."""

    check: Check
    badge: Callable[[str], Badge]
    user: str = ""
    approvers: dict[str, str] = field(default_factory=dict)

    def refuse(self, agent: str, tool: str) -> str | None:
        """Why this employee may not run this tool for this person, or None when it may."""
        desk = DESKS.get(tool)
        if desk is None:
            return f"{tool} is on no desk"
        b = self.badge(agent)
        if not self.check(f"agent:{b.agent}", "employee", f"desk:{desk}"):
            return f"{agent} does not work the {desk} desk"
        if not self.check(f"user:{b.user}", "staff", f"desk:{desk}"):
            return f"{b.user} has no access to the {desk} desk, so {agent} may not act there for them"
        return None

    def may_approve(self, who: str, desk: str = "billing") -> bool:
        # A channel id (telegram:<numeric id>) is not a person until policy.json or MISSION_APPROVERS
        # names one, so a renamed Telegram account or an unknown tapper is refused.
        user = self.approvers.get(who)
        return user is not None and self.check(f"user:{user}", "can_approve", f"desk:{desk}")

    @classmethod
    def from_env(cls, user: str = "", password: str = "") -> Access | None:
        kc, fga = os.environ.get("KEYCLOAK_URL"), os.environ.get("OPENFGA_URL")
        if not (kc and fga):
            return None
        user = user or os.environ.get("MISSION_USER") or "param"
        password = password or os.environ.get("MISSION_PASSWORD") or f"{user}-local"
        secrets = {
            c: os.environ.get(f"KEYCLOAK_SECRET_{c.upper().replace('-', '_')}") or f"{c}-local"
            for c in ("manager", "support-resolver", "engineering-analyst")
        }
        keycloak, policy = Keycloak(kc, secrets=secrets), OpenFGA(fga).setup()
        token, badges = keycloak.sign_in(user, password), {}

        def badge(agent: str) -> Badge:
            if agent not in badges:
                badges[agent] = keycloak.badge(token, agent)
            return badges[agent]

        approvers = json.loads(POLICY.read_text())["approvers"] | dict(
            pair.split("=", 1) for pair in os.environ.get("MISSION_APPROVERS", "").split(",") if "=" in pair
        )
        return cls(policy.check, badge, user, approvers)
