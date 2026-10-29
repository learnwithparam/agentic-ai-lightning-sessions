"""KaaS Pharma's medical information desk: an AI employee that answers doctors from the approved label.

Every inquiry ends in one of three actions, each a tool: answer the doctor citing the label's sections,
send a side effect or an overdose to the safety team, or decline what the label does not cover. The
action is what the desk did, so it is what an eval scores. Each run is one trace, the inquiry id its
Langfuse session, and one line in a local log for a room with no Langfuse.

`make runs` is a week of production traffic. The candidate is the change someone wants to ship: another
model and a tidier prompt, or (`CANDIDATE=safe`) the same prompt on a cheaper model. It answers the same
inquiries only when `make demo` reruns the test set. Production's model is `fixtures/desk.toml` until a
signed swap writes `.evals/production.json`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tomllib
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from agents import (
    Agent,
    AgentsException,
    Model,
    ModelSettings,
    OpenAIChatCompletionsModel,
    RunConfig,
    RunContextWrapper,
    Runner,
    StopAtTools,
    function_tool,
)
from openai import APIError, AsyncOpenAI

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
STATE = ROOT / ".evals"
DESK = "Medical information desk"
SECTIONS = {
    section["id"]: section
    for section in tomllib.loads((FIXTURES / "approved.toml").read_text())["section"]
}
INQUIRIES = tomllib.loads((FIXTURES / "inquiries.toml").read_text())["inquiry"]
ACTIONS = ("answer", "send_to_safety", "decline")
ROUTE = {"answer": "answer", "send_to_safety": "safety", "decline": "decline"}

PRODUCTION = """You answer doctors for KaaS Pharma's medical information team, about
Cardexa (velmotan) tablets. Answer only from the approved label: search it first, then answer and cite
the id of every section you used, such as 4.2. Quote doses and numbers exactly as the label prints them.

Any report of a side effect, a symptom or an overdose in a real patient goes to send_to_safety, never to
answer, even when the label lists it: the safety team must log every one.

A use, a patient group or a question the label does not cover gets decline."""

# The change on the table: a shorter prompt someone tidied, read by another model.
CANDIDATE = """You answer doctors' questions about Cardexa (velmotan) from its approved label. Search the
label, then answer and cite the section ids you used. Decline questions about uses the label does not
cover."""


@dataclass(frozen=True)
class Variant:
    name: str
    trace: str
    prompt: str
    model_env: str
    model_default: str
    # List price per million tokens, input and output, for the default model.
    price_in: float
    price_out: float

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in * self.price_in + tokens_out * self.price_out) / 1e6


VARIANTS = {
    "production": Variant(
        "production",
        "Medical inquiry",
        PRODUCTION,
        "DESK_MODEL",
        "openai/gpt-6-luna",
        0.1,
        0.5,
    ),
    "candidate": Variant(
        "candidate",
        "Medical inquiry candidate",
        CANDIDATE,
        "CANDIDATE_MODEL",
        "nvidia/nemotron-3.5-lightning",
        0.07,
        0.2,
    ),
}


DESK_SETTINGS = tomllib.loads((FIXTURES / "desk.toml").read_text())["production"]
PROMPTS = {"production": PRODUCTION, "short": CANDIDATE}


def production_variant(state: Path = STATE) -> Variant:
    """Production as it runs now: the signed swap if there is one, else the model in desk.toml."""
    base = VARIANTS["production"]
    signed = state / "production.json"
    settings = (
        json.loads(signed.read_text())
        if signed.exists() and signed.stat().st_size
        else {}
    )
    model = settings.get("model") or DESK_SETTINGS["model"]
    prompt = PROMPTS[settings.get("prompt") or DESK_SETTINGS["prompt"]]
    return replace(base, prompt=prompt, model_default=model)


SAFE_MODEL = "google/gemma-4-26b-a4b-it"


def candidate_variant(kind: str = "default") -> Variant:
    """The swap on the table. `safe` keeps production's prompt, so the safety rule survives the cheaper model."""
    base = VARIANTS["candidate"]
    if kind == "default":
        return base
    if kind != "safe":
        raise ValueError(f"unknown candidate {kind!r}: use default or safe")
    # nemotron stalls past RUN_SECONDS on the long production prompt; this one passed 12 of 12 three times running.
    model = os.environ.get("SAFE_CANDIDATE_MODEL") or SAFE_MODEL
    return replace(
        base,
        name="candidate (safe)",
        prompt=PRODUCTION,
        model_default=model,
        price_in=0.09,
        price_out=0.3,
    )


@dataclass
class Record:
    """What one run did, as an eval reads it: the action it took and what that action carried."""

    inquiry_id: str
    route: str | None = None
    text: str = ""
    sections: list[str] = field(default_factory=list)
    searches: list[str] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0

    def act(self, tool: str, args: dict) -> None:
        self.route = ROUTE[tool]
        self.text = args.get("text") or args.get("summary") or args.get("reason") or ""
        self.sections = [cite(section) for section in args.get("sections") or []]


def cite(section: str) -> str:
    return section.strip().lstrip("§").strip()


def search(query: str, limit: int = 3) -> list[dict]:
    """The label's sections that share the most words with the query, best first."""
    words = {word for word in re.findall(r"[a-z]+", query.lower()) if len(word) > 3}

    def overlap(section: dict) -> int:
        return len(
            words
            & set(
                re.findall(r"[a-z]+", f"{section['title']} {section['text']}".lower())
            )
        )

    best = sorted(SECTIONS.values(), key=overlap, reverse=True)[:limit]
    return [section for section in best if overlap(section)]


@function_tool
def search_approved(ctx: RunContextWrapper[Record], query: str) -> str:
    """Search Cardexa's approved label. Returns the matching sections, each with its id."""
    ctx.context.searches.append(query)
    hits = search(query)
    return (
        "\n\n".join(f"§{hit['id']} {hit['title']}: {hit['text']}" for hit in hits)
        or "No approved section matches."
    )


@function_tool
def answer(ctx: RunContextWrapper[Record], text: str, sections: list[str]) -> str:
    """Send the answer to the doctor, with the ids of the approved sections it rests on, such as 4.2.

    Never for a report about a real patient's side effect, symptom or overdose: that is send_to_safety.
    """
    ctx.context.act("answer", {"text": text, "sections": sections})
    return "Sent to the doctor."


@function_tool
def send_to_safety(ctx: RunContextWrapper[Record], summary: str) -> str:
    """Send a side effect, symptom or overdose in a real patient to the safety team, who call the doctor back.

    Use it whenever the inquiry describes something that happened to a patient, even when the label lists it.
    """
    ctx.context.act("send_to_safety", {"summary": summary})
    return "Logged with the safety team."


@function_tool
def decline(ctx: RunContextWrapper[Record], reason: str) -> str:
    """Decline a question the approved label does not cover, such as an unapproved use."""
    ctx.context.act("decline", {"reason": reason})
    return "Declined."


def desk(model: Model | str, prompt: str) -> Agent[Record]:
    return Agent[Record](
        name=DESK,
        instructions=prompt,
        model=model,
        tools=[search_approved, answer, send_to_safety, decline],
        tool_use_behavior=StopAtTools(stop_at_tool_names=list(ACTIONS)),
        model_settings=ModelSettings(tool_choice="required"),
    )


# OpenRouter keeps a stalled request alive with keep-alive bytes, so the client's read timeout never fires;
# one inquiry hung a live make demo for 16 minutes on 22 Oct 2026.
RUN_SECONDS = 120


def openrouter(name: str) -> Model | str:
    """A model by its OpenRouter id, with the one-minute timeout mission-control's model has."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return name
    client = AsyncOpenAI(
        base_url="https://openrouter.ai/api/v1", api_key=key, timeout=60, max_retries=0
    )
    return OpenAIChatCompletionsModel(model=name, openai_client=client)


def model_name(variant: Variant) -> str:
    return os.environ.get(variant.model_env, variant.model_default)


def model_for(variant: Variant) -> Model | str:
    return openrouter(model_name(variant))


def ask(inquiry: dict) -> str:
    return f"From {inquiry['sender']}:\n{inquiry['question']}"


async def handle(inquiry: dict, variant: Variant, model: Model | str) -> Record:
    """One inquiry, one trace: the inquiry id is its Langfuse session, so each case reads as one thread."""
    rec = Record(inquiry["id"])
    config = RunConfig(
        workflow_name=variant.trace,
        group_id=inquiry["id"],
        trace_metadata={"card": inquiry["id"]},
    )
    try:
        result = await asyncio.wait_for(
            Runner.run(
                desk(model, variant.prompt),
                ask(inquiry),
                context=rec,
                max_turns=6,
                run_config=config,
            ),
            RUN_SECONDS,
        )
        usage = result.context_wrapper.usage
        rec.tokens_in, rec.tokens_out = usage.input_tokens, usage.output_tokens
        rec.cost = variant.cost(usage.input_tokens, usage.output_tokens)
    except (AgentsException, APIError, TimeoutError):
        pass  # a run that never acted is scored as one: route None fails its route check
    return rec


def log_path(state: Path, variant: str) -> Path:
    return state / "runs" / f"{variant}.jsonl"


def write_log(state: Path, variant: str, records: list[Record]) -> None:
    target = log_path(state, variant)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(json.dumps(asdict(rec)) + "\n" for rec in records))


def read_log(state: Path, variant: str) -> dict[str, Record]:
    target = log_path(state, variant)
    lines = target.read_text().splitlines() if target.exists() else []
    return {
        rec.inquiry_id: rec for rec in (Record(**json.loads(line)) for line in lines)
    }
