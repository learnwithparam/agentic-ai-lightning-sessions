# Build a Multi-Agent System with MCP

The crew that rebooks Priya's cancelled flight, on `../mission-control`'s board.

- `make check` is the gate: ruff, format and pytest. The gateways and the travel system are real; the
  model is the SDK's `ScriptedModel`, so it needs no keys.
- A step is built only when its promise is a test marked `@pytest.mark.lab("<slug>")`; `tests/test_steps.py`
  enforces it against `steps.toml`.
- Scopes and every refusal live in `crew/gateway.py`, the travel rules and the human approval in
  `crew/travel.py`, and Done in `team.settle`. Never move one into a prompt.
- The model key is `../mission-control/.env`, loaded by `uv run --env-file`; never on a command line.
