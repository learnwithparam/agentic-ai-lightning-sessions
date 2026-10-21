# Cut AI Agent Costs with Token Economics

The claims reader that files Acme Insurance's motor claims, on `../mission-control`'s board.

- `make check` is the gate: ruff, format and pytest. The claims system and its ledger are real; the
  model is the SDK's `ScriptedModel`, billing like a provider (prefix cache, photo detail, reasoning
  effort), so it needs no keys.
- A step is built only when its promise is a test marked `@pytest.mark.lab("<slug>")`; `tests/test_steps.py`
  enforces it against `steps.toml`.
- The budget and the filing rules live in `claims/system.py`, Done in `reader.settle`. Never move one
  into a prompt.
- A lever changes what a read costs, never what it reads. The README's table is one real `make demo`;
  rerun it before changing a number there.
- The model key is `../mission-control/.env`, loaded by `uv run --env-file`; never on a command line.
