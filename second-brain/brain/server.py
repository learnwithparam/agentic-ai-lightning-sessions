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
# A small model that finds nothing searches again and again; the brain stops it here, not in a prompt.
SEARCHES = 3
RESEARCHER = "Researcher"


class BrainServer(MCPServer):
    def __init__(self, root: Path, state: Path, card: str) -> None:
        super().__init__(name="brain", instructions="Hannah Moreau's brain of client notes")
        self.vault = Vault(root)
        self.card = card
        self.calls = state / "calls.jsonl"
        tools(self)

    def log(self, tool: str, args: dict, allowed: bool, said: str, caller: str = RESEARCHER) -> str:
        return log_call(self.calls.parent, self.card, tool, args, allowed, said, caller)


def log_call(state: Path, card: str, tool: str, args: dict, allowed: bool, said: str, caller: str) -> str:
    """One row in the card's call log, whoever made the call: the researcher or a person on the board."""
    row = {"at": time.time(), "card": card, "tool": tool, "by": caller}
    row |= {"args": {key: str(value)[:160] for key, value in args.items()}, "allowed": allowed}
    row["said"] = said[:200]
    log_file = state / "calls.jsonl"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a") as handle:
        handle.write(json.dumps(row) + "\n")
    return said


def calls(state: Path, card: str | None = None) -> list[dict]:
    log_file = state / "calls.jsonl"
    rows = [json.loads(line) for line in log_file.read_text().splitlines()] if log_file.exists() else []
    return [{"by": RESEARCHER, **row} for row in rows if card is None or row["card"] == card]


def citations(state: Path, card: str) -> list[dict]:
    """The citations the brain accepted on one card, each once, in the order they were made."""
    seen: dict[tuple[str, str], dict] = {}
    for call in calls(state, card):
        if call["tool"] == "cite" and call["allowed"]:
            seen.setdefault((call["args"]["note"], call["args"]["quote"]), call["args"])
    return list(seen.values())


def tools(server: BrainServer) -> None:
    @server.tool()
    def search_notes(query: str) -> str:
        """Find the notes that match a few keywords. Returns each note's name, title and quoted facts."""
        done = sum(call["tool"] == "search_notes" for call in calls(server.calls.parent, server.card))
        if done >= SEARCHES:
            said = f"Refused: {SEARCHES} searches is the limit. Cite what you found or say it is not held."
            return server.log("search_notes", {"query": query}, False, said)
        hits = server.vault.search(query)
        said = (
            "\n\n".join(
                f"{note.name}: {note.title} ({note.date})\n" + "\n".join(f'- "{fact}"' for fact in note.facts)
                for note, _ in hits
            )
            or "No note matches."
        )
        if done:
            said += "\n\nYou have searched already. Cite the facts that answer, or say the brain lacks it."
        return server.log("search_notes", {"query": query}, True, said)

    @server.tool()
    def read_note(name: str) -> str:
        """Read one note by the name search_notes gave, with its source, date and quoted facts."""
        try:
            return server.log("read_note", {"name": name}, True, server.vault.raw(name))
        except Refused as refusal:
            return server.log("read_note", {"name": name}, False, f"Refused: {refusal}")

    @server.tool()
    def cite(note: str, quote: str) -> str:
        """Rest the answer on one fact: the note's name and the fact, copied exactly from the note."""
        args = {"note": note, "quote": quote}
        try:
            cited = server.vault.cite(note, quote)
            return server.log("cite", args, True, f"Cited {note}, from {cited.source}")
        except Refused as refusal:
            return server.log("cite", args, False, f"Refused: {refusal}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--brain", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--card", required=True)
    args = parser.parse_args()
    BrainServer(args.brain, args.state, args.card).run("stdio")
