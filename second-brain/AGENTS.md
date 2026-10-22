# Build a Second Brain with Claude Code

Hannah's brain and the researcher that answers from it, on `../mission-control`'s board.

- `make check` is the gate: ruff, format and pytest. The vault and the brain's MCP server are real; the
  model is the SDK's `ScriptedModel`, so it needs no keys.
- A step is built only when its promise is a test marked `@pytest.mark.lab("<slug>")`; `tests/test_steps.py`
  enforces it against `steps.toml`.
- The filing rules live in `fixtures/brain/CLAUDE.md`, the checks in `brain/vault.py`, Done in
  `answerer.settle`. Never move a check into a prompt.
- `make file` runs Claude Code on the user's login, pinned to one model in `brain/filer.py`; never widen
  its tools or drop a pin.
- The model key is `../mission-control/.env`, loaded by `uv run --env-file`; never on a command line.
