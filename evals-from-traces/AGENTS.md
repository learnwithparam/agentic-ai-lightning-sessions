# Build AI Agent Evals from Production Traces

Quillon Pharma's medical information desk, on `../mission-control`'s board, and the evals that gate a
change to it.

- `make check` is the gate: ruff, format and pytest. The desk's tools, the checks and the gate are real;
  the models are the SDK's `ScriptedModel` and Langfuse is a fake payload, so it needs no keys.
- A step is built only when its promise is a test marked `@pytest.mark.lab("<slug>")`; `tests/test_steps.py`
  enforces it against `steps.toml`.
- The labels are Dr. Okafor's (`fixtures/labels.toml`); never relabel a case to make a run pass.
- Code checks run first and the judge reads only what they passed; it counts only above `judge.TRUST`.
- A missed side effect is critical: `score.gate` refuses it at any pass rate. Never make it a weight.
- Keys come from mission-control's env file, loaded by `uv run --env-file`; never on a command line.
