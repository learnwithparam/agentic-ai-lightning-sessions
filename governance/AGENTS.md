# Build an AI Agent Governance Framework

Halden Hotels' four AI employees on `../mission-control`'s board, under one policy file the operations
director owns: what each may do alone, who signs above that, which are paused, and each one's AI budget.

- `make check` is the gate: ruff, format and pytest. The tools, the gate, the hotels' systems and the
  decision record are real; the model is the SDK's `ScriptedModel`, so it needs no keys.
- A step is built only when its promise is a test marked `@pytest.mark.lab("<slug>")`; `tests/test_steps.py`
  enforces it against `steps.toml`.
- Every write to a system of record goes through `crew.gate`, which asks `policy.check` first. No tool
  approves, signs or edits the policy, and a test walks every tool to prove it.
- Only a person signs: `run.sign` refuses any AI employee's name, and the board's decide route refuses a
  post that is not from its own page.
- `operating/record.py` only appends. `make record` fails a write with no decision, or two, behind it.
- Rules live in `fixtures/policy.toml`; the prompts state none of its limits, so a policy edit changes
  behaviour with no prompt edited.
- Prices live in `operating/cost.py` alone. Printed lines stay within `run.WIDTH`, asserted by a test.
- Keys come from mission-control's env file, loaded by `uv run --env-file`; never on a command line.
