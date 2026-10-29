"""The offline model for `make naive`: it reads the night, then asks for one price, whatever the policy says.

It stands in for an employee whose only limit is the sentence in its prompt, so the demo needs no key.
"""

from __future__ import annotations

from agents import Usage
from agents.testing import ModelStep, ScriptedModel, function_call

USAGE = Usage(requests=1, input_tokens=1500, output_tokens=60, total_tokens=1560)


def price_under_the_floor(price: int) -> ScriptedModel:
    def respond(call) -> ModelStep:
        earlier = call.input if isinstance(call.input, list) else []
        names = [item.get("name") for item in earlier if isinstance(item, dict)]
        if "read_demand" not in names:
            step = function_call("read_demand", {}, call_id="read")
        else:
            step = function_call("set_price", {"price": price}, call_id="write")
        return ModelStep(output=[step], usage=USAGE)

    return ScriptedModel([ModelStep.respond(respond) for _ in range(10)])
