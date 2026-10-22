"""The brain on disk: an inbox Hannah drops files into, and the notes Claude Code files from them.

A note is Markdown with frontmatter naming its source, and a `## Facts` list of lines quoted from that
source. `check` holds every note to its source: the file exists and every quote is in it, word for word.
`cite` holds an answer to the notes: the note exists and the quote is one of its facts. Together they
mean an answer that cites the brain cites something Hannah captured, never something a model wrote.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
BRAIN = ROOT / ".brain"
NAME = re.compile(r"[a-z0-9][a-z0-9-]*")


class Refused(Exception):
    """The brain will not stand behind this citation."""


def flat(text: str) -> str:
    """Text as compared: one space between words and straight quotes, so line breaks never fail a quote."""
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return " ".join(text.split())


@dataclass(frozen=True)
class Note:
    name: str
    title: str
    source: str
    date: str
    facts: tuple[str, ...]


def parse(name: str, text: str) -> Note:
    head = dict(re.findall(r"^(\w+):\s*(.+)$", text.split("\n---", 1)[0], re.M))
    title = next(iter(re.findall(r"^# (.+)$", text, re.M)), name)
    facts = text.split("## Facts", 1)[-1] if "## Facts" in text else ""
    quotes = tuple(flat(q) for q in re.findall(r'^\s*-\s*["“](.+)["”]\s*$', facts, re.M))
    return Note(name, title.strip(), head.get("source", "").strip(), head.get("date", "").strip(), quotes)


@dataclass
class Vault:
    root: Path

    @classmethod
    def seed(cls, root: Path = BRAIN) -> Vault:
        """A fresh brain: the filing rules and Hannah's inbox, no notes yet."""
        shutil.rmtree(root, ignore_errors=True)
        (root / "notes").mkdir(parents=True)
        shutil.copy(FIXTURES / "brain" / "CLAUDE.md", root / "CLAUDE.md")
        shutil.copytree(FIXTURES / "inbox", root / "inbox")
        return cls(root)

    def inbox(self) -> list[str]:
        return sorted(p.name for p in (self.root / "inbox").iterdir() if p.is_file())

    def names(self) -> list[str]:
        return sorted(p.stem for p in (self.root / "notes").glob("*.md"))

    def note(self, name: str) -> Note:
        if not NAME.fullmatch(name) or not (f := self.root / "notes" / f"{name}.md").is_file():
            raise Refused(f"the brain holds no note named {name!r}")
        return parse(name, f.read_text())

    def raw(self, name: str) -> str:
        self.note(name)
        return (self.root / "notes" / f"{name}.md").read_text()

    def source_text(self, source: str) -> str | None:
        f = (self.root / source).resolve()
        if not f.is_relative_to((self.root / "inbox").resolve()) or not f.is_file():
            return None
        if f.suffix == ".pdf":
            return "\n".join(p.extract_text() or "" for p in PdfReader(f).pages)
        return f.read_text()

    def check(self) -> list[str]:
        """Every way a note fails its source, one line each; empty when the brain is sound."""
        problems = []
        filed = {self.note(n).source.removeprefix("inbox/") for n in self.names()}
        problems += [f"{f} has no note" for f in self.inbox() if f not in filed]
        for name in self.names():
            n = self.note(name)
            text = self.source_text(n.source)
            if text is None:
                problems.append(f"{name}: its source {n.source or '(none)'} is not in the inbox")
                continue
            if not n.facts:
                problems.append(f"{name}: no facts")
            body = flat(text)
            problems += [f'{name}: "{q[:60]}" is not in {n.source}' for q in n.facts if q not in body]
        return problems

    def cite(self, name: str, quote: str) -> Note:
        """The note behind a citation, or Refused when the note or the quote is not in the brain."""
        n = self.note(name)
        q = flat(quote).strip('"')
        if not q or not any(q in fact for fact in n.facts):
            raise Refused(f'{name} has no fact that says "{q[:60]}"')
        return n

    def search(self, query: str, limit: int = 3) -> list[tuple[Note, int]]:
        """Notes ranked by how many of the query's words their title and facts share."""
        words = {w for w in re.findall(r"[a-z0-9£%]+", query.lower()) if len(w) > 2}
        scored = []
        for name in self.names():
            n = self.note(name)
            have = set(re.findall(r"[a-z0-9£%]+", (n.title + " " + " ".join(n.facts)).lower()))
            if hit := len(words & have):
                scored.append((n, hit))
        return sorted(scored, key=lambda s: -s[1])[:limit]
