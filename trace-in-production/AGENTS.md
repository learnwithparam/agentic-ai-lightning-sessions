# Trace AI Agents in Production

Brekke Dairy's Line 2 maintenance crew, on `../mission-control`'s board: alerts worked, a worker killed
mid-run and resumed, every run traced to Langfuse, and a budget alert on the board.

- `make check` is the gate: ruff, format and pytest. The crew's tools, the maintenance system, the tracer
  and the budget are real; the model is the SDK's `ScriptedModel`, Langfuse is a fake, so it
  needs no keys.
- A step is built only when its promise is a test marked `@pytest.mark.lab("<slug>")`; `tests/test_steps.py`
  enforces it against `steps.toml`.
- The alert id is the maintenance system's idempotency key and comes from code, never a tool argument.
- A card is Done only when `Cmms.outcome` holds an order or a dismissal, whatever the crew said it did.
- Every span is posted when it starts and when it ends (`upkeep/stream.py`); posting only at the end loses
  a killed run's spend, and a test proves it.
- Prices live in `upkeep/cost.py` alone. Printed lines stay within `run.WIDTH`, asserted by a test.
- Keys come from mission-control's env file, loaded by `uv run --env-file`; never on a command line.
