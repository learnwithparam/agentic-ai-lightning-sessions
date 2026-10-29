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
MAX_ANSWER = 300


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
    quotes = tuple(flat(quote) for quote in re.findall(r'^\s*-\s*["“](.+)["”]\s*$', facts, re.M))
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
        return sorted(entry.name for entry in (self.root / "inbox").iterdir() if entry.is_file())

    def names(self) -> list[str]:
        return sorted(entry.stem for entry in (self.root / "notes").glob("*.md"))

    def note(self, name: str) -> Note:
        path = self.root / "notes" / f"{name}.md"
        if not NAME.fullmatch(name) or not path.is_file():
            raise Refused(f"the brain holds no note named {name!r}")
        return parse(name, path.read_text())

    def raw(self, name: str) -> str:
        self.note(name)
        return (self.root / "notes" / f"{name}.md").read_text()

    def source_text(self, source: str) -> str | None:
        path = (self.root / source).resolve()
        if not path.is_relative_to((self.root / "inbox").resolve()) or not path.is_file():
            return None
        if path.suffix == ".pdf":
            return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
        return path.read_text()

    def check(self) -> list[str]:
        """Every way a note fails its source, one line each; empty when the brain is sound."""
        problems = []
        filed = {self.note(name).source.replace("inbox/", "", 1) for name in self.names()}
        problems += [f"{file} has no note" for file in self.inbox() if file not in filed]
        for name in self.names():
            note = self.note(name)
            text = self.source_text(note.source)
            if text is None:
                problems.append(f"{name}: its source {note.source or '(none)'} is not in the inbox")
                continue
            if not note.facts:
                problems.append(f"{name}: no facts")
            body = flat(text)
            problems += [
                f'{name}: "{quote[:60]}" is not in {note.source}' for quote in note.facts if quote not in body
            ]
        return problems

    def cite(self, name: str, quote: str) -> Note:
        """The note behind a citation, or Refused when the note or the quote is not in the brain."""
        note = self.note(name)
        wanted = flat(quote).strip('"')
        if not wanted or not any(wanted in fact for fact in note.facts):
            raise Refused(f'{name} has no fact that says "{wanted[:60]}"')
        return note

    def search(self, query: str, limit: int = 3) -> list[tuple[Note, int]]:
        """Notes ranked by how many of the query's words their title and facts share."""
        words = {word for word in re.findall(r"[a-z0-9£%]+", query.lower()) if len(word) > 2}
        scored = []
        for name in self.names():
            note = self.note(name)
            have = set(re.findall(r"[a-z0-9£%]+", (note.title + " " + " ".join(note.facts)).lower()))
            shared = len(words & have)
            if shared:
                scored.append((note, shared))
        return sorted(scored, key=lambda scored_note: -scored_note[1])[:limit]

    def file_answer(
        self,
        card_id: str,
        question: str,
        asked_by: str,
        answer: str,
        source: str,
        answered_by: str,
        owner: str,
        on_date: str,
    ) -> Note:
        """The consultant's answer, filed as a note whose fact quotes it, so the check still passes."""
        if answered_by != owner:
            raise Refused(f"{answered_by} may not answer for the client; only {owner} answers for the client")
        answer, source = flat(answer), flat(source)
        if not answer or not source:
            raise Refused("An answer needs both the answer and where it came from")
        if len(answer) >= MAX_ANSWER or len(source) >= MAX_ANSWER:
            raise Refused(f"An answer and its source must each be under {MAX_ANSWER} characters")
        name = f"answer-{card_id.lower()}"
        inbox_file = f"{on_date}-{name}.md"
        (self.root / "inbox" / inbox_file).write_text(
            f"# {question}\n\nAsked by: {asked_by}\nAnswered by: {answered_by}\n"
            f"Source: {source}\n\n{answer}\n"
        )
        (self.root / "notes" / f"{name}.md").write_text(
            f"source: inbox/{inbox_file}\ndate: {on_date}\n---\n# {question}\n\n## Facts\n"
            f'- "{answer}"\n- "Source: {source}"\n'
        )
        return self.note(name)
