"""The two guardrails on the screener, and the hidden-text scan behind one of them.

A CV parser reads every character in the file; a recruiter reads what the page shows. Text one sees and
the other does not is the attack: the 2026 Duke study of 196,682 CVs found about 1% hiding prompts this
way, most of them as white or tiny text. The scan compares the two readings, so it needs no model.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser

from agents import (
    Agent,
    GuardrailFunctionOutput,
    RunContextWrapper,
    ToolGuardrailFunctionOutput,
    ToolOutputGuardrailData,
    input_guardrail,
    tool_output_guardrail,
)

# Text a person on a white page cannot read: not drawn, see-through, white, or a pixel or two high.
INVISIBLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(\.0+)?(?![.\d])"
    r"|font-size\s*:\s*[0-2](\.\d+)?(px|pt)|(?<![-\w])color\s*:\s*(#fff\b|#ffffff\b|white\b)",
    re.I,
)
VOID = {"br", "img", "hr", "meta", "link", "input"}
# The UK Equality Act 2010's protected characteristics a shortlist request names most often.
PROTECTED = {
    "age": r"\b(under|over|aged?)\s+\d{2}\b|\byoung\b|\bage\b|\bmature\b|\bdigital natives?\b",
    "sex": r"\b(male|female|men|women)\s+only\b|\bgender\b",
    "nationality": r"\bnationality\b|\b(british|english)\s+(only|born)\b|\bnative\b",
    "pregnancy": r"\bpregnan|\bmaternity\b|\bno mums\b",
    "religion": r"\breligio",
}


@dataclass(frozen=True)
class Scan:
    text: str
    visible: str
    hidden: list[str]


class _Reader(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[bool] = []
        self.parts: list[tuple[str, bool]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in VOID:
            return
        style = dict(attrs).get("style") or ""
        self.stack.append(bool(self.stack and self.stack[-1]) or bool(INVISIBLE.search(style)))

    def handle_endtag(self, tag: str) -> None:
        if tag not in VOID and self.stack:
            self.stack.pop()

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append((" ".join(data.split()), bool(self.stack and self.stack[-1])))


def scan(html: str) -> Scan:
    """What a parser extracts, what a person sees, and the text only the parser gets."""
    r = _Reader()
    r.feed(html)
    return Scan(
        "\n".join(t for t, _ in r.parts),
        "\n".join(t for t, hid in r.parts if not hid),
        [t for t, hid in r.parts if hid],
    )


def protected(text: str) -> list[str]:
    return [name for name, pattern in PROTECTED.items() if re.search(pattern, text, re.I)]


def said(items: str | list) -> str:
    if isinstance(items, str):
        return items
    return "\n".join(str(i.get("content", "")) for i in items if isinstance(i, dict))


@input_guardrail(name="fair request", run_in_parallel=False)
def fair_request(ctx: RunContextWrapper, agent: Agent, items: str | list) -> GuardrailFunctionOutput:
    """A request that screens by a protected characteristic stops before the model is called."""
    found = protected(said(items))
    if found:
        desk = ctx.context
        desk.ats.audit(
            desk.card.id, "fair request", "", f"blocked: the request screens by {', '.join(found)}"
        )
    return GuardrailFunctionOutput(found, tripwire_triggered=bool(found))


@tool_output_guardrail(name="hidden text")
def hidden_text(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
    """A CV whose text differs from what a person sees is withheld from the model and sent to a person."""
    desk = data.context.context
    cid = json.loads(data.context.tool_arguments or "{}").get("candidate_id", "")
    found = [h for h in scan(desk.ats.cv(cid)).hidden if h in str(data.output)] if cid else []
    if not found:
        return ToolGuardrailFunctionOutput.allow()
    name = desk.ats.candidate(cid)["name"]
    desk.ats.flag(desk.card.id, cid, found)
    return ToolGuardrailFunctionOutput.reject_content(
        f"{name}'s CV is withheld for a recruiter: it holds text a person reading it cannot see. "
        f"Do not shortlist {name}; judge the others."
    )
