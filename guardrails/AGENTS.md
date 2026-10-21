# Secure AI Agents with Guardrails

The screener that shortlists Acme's payroll applicants, on `../mission-control`'s board.

- `make check` is the gate: ruff, format and pytest. The guardrails and the hiring system are real; the
  model is the SDK's `ScriptedModel`, so it needs no keys.
- A step is built only when its promise is a test marked `@pytest.mark.lab("<slug>")`; `tests/test_steps.py`
  enforces it against `steps.toml`.
- Both guardrails live in `hiring/guard.py`, the private fields and the shortlist rules in `hiring/ats.py`,
  and Done in `screener.settle`. Never move one into a prompt.
- The model key is `../mission-control/.env`, loaded by `uv run --env-file`; never on a command line.
