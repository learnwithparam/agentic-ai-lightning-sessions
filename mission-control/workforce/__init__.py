"""AI employees for Acme's billing: the analyst, the manager and the support resolver."""

import os

from agents import Model, OpenAIChatCompletionsModel, set_tracing_disabled
from openai import AsyncOpenAI


def default_model() -> Model | str:
    """WORKFORCE_MODEL, through OpenRouter when OPENROUTER_API_KEY is set, else through OpenAI.

    OpenRouter ids such as nvidia/nemotron-3.5-lightning look like an SDK provider prefix, so the model
    is built here instead of passed as a string. The SDK's tracing would post the key to OpenAI, so it
    goes to Langfuse when LANGFUSE_HOST is set, and is off otherwise.
    """
    from workforce.traces import install

    name = os.environ.get("WORKFORCE_MODEL", "nvidia/nemotron-3.5-lightning")
    key = os.environ.get("OPENROUTER_API_KEY")
    langfuse = install()
    if not key:
        return name
    if not langfuse:
        set_tracing_disabled(True)
    client = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=key)
    return OpenAIChatCompletionsModel(model=name, openai_client=client)
