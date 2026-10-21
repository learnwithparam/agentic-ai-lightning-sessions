"""The engineering analyst: an agent that searches GitHub, Linear and the docs, then answers with sources.

Citations are checked, not trusted. The answer cites ids in square brackets; an id no tool returned in
this run is removed and listed in Reply.dropped, so a reply never links to a source that was not read.
Memory is the SDK session: one per Slack thread, so a follow-up question sees the earlier answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from agents import Agent, Model, Runner, Session, function_tool
from agents.tool_context import ToolContext

from workforce import default_model
from workforce.access import Access
from workforce.sources import Readers, Source

INSTRUCTIONS = """Answer engineering questions from company tools.
- Search GitHub, Linear and the docs before answering.
- Search again with other words if a search finds nothing.
- Do every search before you reply. Your reply is final, so never say you will search next.
- Use only what the searches return. End each sentence with its source id in brackets, like [EXP-41].
- If nothing relevant comes back, say what you searched and that you found nothing.
- Keep it short, in plain sentences with no markdown: the answer first, then what to do next."""


@dataclass
class Desk:
    """What one run may read, and every source it read."""

    readers: Readers
    read: dict[str, Source] = field(default_factory=dict)
    access: Access | None = None
    attempts: list[str] = field(default_factory=list)

    def refused(self, ctx: ToolContext) -> str | None:
        """Keycloak and OpenFGA's answer for the employee running this tool, when the stack is on."""
        why = self.access.refuse(ctx.agent.name, ctx.tool_name) if self.access else None
        if why:
            self.attempts.append(why)
            return f"Refused: {why}."
        return None

    def keep(self, rows: list[Source]) -> str:
        for r in rows:
            self.read[r.id] = r
        if not rows:
            return "No results."
        return "\n\n".join(f"[{r.id}] {r.title}\n{r.text}" for r in rows)


@function_tool
def search_github(ctx: ToolContext[Desk], query: str) -> str:
    """Search GitHub issues and pull requests."""
    return ctx.context.refused(ctx) or ctx.context.keep(ctx.context.readers.github(query))


@function_tool
def search_linear(ctx: ToolContext[Desk], query: str) -> str:
    """Search Linear issues: status, owner and plans."""
    return ctx.context.refused(ctx) or ctx.context.keep(ctx.context.readers.linear(query))


@function_tool
def search_docs(ctx: ToolContext[Desk], query: str) -> str:
    """Search the engineering docs and runbooks."""
    return ctx.context.refused(ctx) or ctx.context.keep(ctx.context.readers.docs(query))


def analyst(model: Model | str | None = None) -> Agent[Desk]:
    return Agent[Desk](
        name="Engineering analyst",
        instructions=INSTRUCTIONS,
        tools=[search_github, search_linear, search_docs],
        model=model or default_model(),
    )


@dataclass(frozen=True)
class Reply:
    text: str
    sources: list[Source]
    dropped: list[str]


CITE = re.compile(r"\[([^\[\]]+)\]")
# Models write ids with typographic hyphens (release\u2011process); those are the same id it read.
DASHES = str.maketrans(dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2212", "-"))


def check_citations(text: str, read: dict[str, Source]) -> Reply:
    cited, dropped = [], []
    for m in CITE.finditer(text):
        sid = m.group(1).translate(DASHES)
        if sid in read:
            if read[sid] not in cited:
                cited.append(read[sid])
        elif sid not in dropped:
            dropped.append(sid)
    # A read source named without brackets, "(GH#412)", is still a source this run read.
    plain = text.translate(DASHES)
    for sid, src in read.items():
        if src not in cited and re.search(rf"(?<![\w#-]){re.escape(sid)}(?![\w-])", plain):
            cited.append(src)
    clean = CITE.sub(lambda m: m.group(0) if m.group(1).translate(DASHES) in read else "", text)
    return Reply(re.sub(r"[ \t]+([.,;:])", r"\1", clean).strip(), cited, dropped)


def ask(question: str, thread: list[tuple[str, str]] | None = None) -> str:
    """The model's input: the Slack thread so far, then the question it was tagged with."""
    if not thread:
        return question
    lines = "\n".join(f"{who}: {text}" for who, text in thread)
    return f"Slack thread so far:\n{lines}\n\nQuestion: {question}"


async def answer(
    question: str,
    *,
    readers: Readers,
    thread: list[tuple[str, str]] | None = None,
    session: Session | None = None,
    model: Model | str | None = None,
) -> Reply:
    desk = Desk(readers)
    result = await Runner.run(analyst(model), ask(question, thread), context=desk, session=session)
    return check_citations(str(result.final_output), desk.read)
