# Trace AI Agents in Production

KaaS Dairy's Line 2 maintenance crew, on `../mission-control`'s board: alerts worked, a worker killed
mid-run and resumed, every run traced to Langfuse, and a shift over its budget paused on the board until
Ingrid Halvorsen, signed in, continues it.

- `make check` is the gate: ruff, format and pytest. The crew's tools, the maintenance system, the tracer
  and the budget are real; the model is the SDK's `ScriptedModel`, Langfuse is a fake, so it
  needs no keys.
- A step is built only when its promise is a test marked `@pytest.mark.lab("<slug>")`; `tests/test_steps.py`
  enforces it against `steps.toml`.
- The alert id is the maintenance system's idempotency key and comes from code, never a tool argument.
- A card is Done only when `Cmms.outcome` holds an order or a dismissal, whatever the crew said it did.
- Who continues a paused shift comes from the board's sign-in, never from the form;
  `run.allow_continue` refuses anyone but the maintenance lead and logs the refusal first.
- Every span is posted when it starts and when it ends (`upkeep/stream.py`); posting only at the end loses
  a killed run's spend, and a test proves it.
- Prices live in `upkeep/cost.py` alone. Printed lines stay within `run.WIDTH`, asserted by a test.
- Keys and passphrases come from mission-control's env file, loaded by `uv run --env-file`; never on a
  command line.
