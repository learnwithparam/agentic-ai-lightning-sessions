"""The brain's MCP server: the only way an AI employee reads Hannah's brain.

It lists three tools. `search_notes` and `read_note` read the notes, never the inbox or anything outside
`notes/`. `cite` is how an answer rests on the brain: the vault checks the note and the quote, and the
server refuses a citation it cannot find. Every call, allowed or refused, goes to the card's call log.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from brain.vault import Refused, Vault

TOOLS = ("search_notes", "read_note", "cite")


class BrainServer(MCPServer):
    def __init__(self, root: Path, state: Path, card: str) -> None:
        super().__init__(name="brain", instructions="Hannah Moreau's brain of client notes")
        self.vault, self.card, self.calls = Vault(root), card, state / "calls.jsonl"
        tools(self)

    def log(self, tool: str, args: dict, allowed: bool, said: str) -> str:
        row = {"at": time.time(), "card": self.card, "tool": tool}
        row |= {"args": {k: str(v)[:160] for k, v in args.items()}, "allowed": allowed, "said": said[:200]}
        self.calls.parent.mkdir(parents=True, exist_ok=True)
        with self.calls.open("a") as f:
            f.write(json.dumps(row) + "\n")
        return said


def calls(state: Path, card: str | None = None) -> list[dict]:
    f = state / "calls.jsonl"
    rows = [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []
    return [r for r in rows if card is None or r["card"] == card]


def citations(state: Path, card: str) -> list[dict]:
    """The citations the brain accepted on one card, each once, in the order they were made."""
    seen: dict[tuple[str, str], dict] = {}
    for c in calls(state, card):
        if c["tool"] == "cite" and c["allowed"]:
            seen.setdefault((c["args"]["note"], c["args"]["quote"]), c["args"])
    return list(seen.values())


def tools(s: BrainServer) -> None:
    @s.tool()
    def search_notes(query: str) -> str:
        """Find the notes that match a few keywords. Returns each note's name, title and quoted facts."""
        hits = s.vault.search(query)
        said = (
            "\n\n".join(
                f"{n.name}: {n.title} ({n.date})\n" + "\n".join(f'- "{f}"' for f in n.facts) for n, _ in hits
            )
            or "No note matches."
        )
        # A small model that finds nothing searches again forever; tell it where it stands.
        if any(c["tool"] == "search_notes" for c in calls(s.calls.parent, s.card)):
            said += "\n\nYou have searched already. Cite the facts that answer, or say the brain lacks it."
        return s.log("search_notes", {"query": query}, True, said)

    @s.tool()
    def read_note(name: str) -> str:
        """Read one note by the name search_notes gave, with its source, date and quoted facts."""
        try:
            return s.log("read_note", {"name": name}, True, s.vault.raw(name))
        except Refused as e:
            return s.log("read_note", {"name": name}, False, f"Refused: {e}")

    @s.tool()
    def cite(note: str, quote: str) -> str:
        """Rest the answer on one fact: the note's name and the fact, copied exactly from the note."""
        args = {"note": note, "quote": quote}
        try:
            n = s.vault.cite(note, quote)
            return s.log("cite", args, True, f"Cited {note}, from {n.source}")
        except Refused as e:
            return s.log("cite", args, False, f"Refused: {e}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--brain", type=Path, required=True)
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--card", required=True)
    a = p.parse_args()
    BrainServer(a.brain, a.state, a.card).run("stdio")
