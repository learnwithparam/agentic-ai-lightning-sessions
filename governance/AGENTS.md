# Build an AI Agent Governance Framework

KaaS Hotels' four AI employees on `../mission-control`'s board, under one policy file the operations
director owns: what each may do alone, who signs above that, which are paused, and each one's AI budget.

- `make check` is the gate: ruff, format and pytest. The tools, the gate, the hotels' systems and the
  decision record are real; the model is the SDK's `ScriptedModel`, so it needs no keys.
- A step is built only when its promise is a test marked `@pytest.mark.lab("<slug>")`; `tests/test_steps.py`
  enforces it against `steps.toml`.
- Every write to a system of record goes through `crew.gate`, which asks `policy.check` first. No tool
  approves, signs or edits the policy, and a test walks every tool to prove it.
- Only the named signer signs: `run.sign` refuses any AI employee and any person the policy does not
  name for that write, logs the refusal, and the board takes the name from the sign-in, never the form.
- `operating/record.py` only appends. `make decisions` fails a write with no decision, or two, behind it.
- Rules live in `fixtures/policy.toml`; the prompts state none of its limits, so a policy edit changes
  behaviour with no prompt edited.
- Prices live in `operating/cost.py` alone. Printed lines stay within `run.WIDTH`, asserted by a test.
- Keys come from mission-control's env file, loaded by `uv run --env-file`; never on a command line.
