# AI workforce

The system taught in the buildcamp, the workshops and the Live Labs, and installed by the KaaS.team
Agent Pilot (`internal-apps/plan/specs/08-owned-software.md`).

- `make check` is the gate: ruff, format and pytest. It runs offline with the SDK's `ScriptedModel`.
- A step is built only when its Live Lab promise is a test marked `@pytest.mark.lab("<slug>")`; mark
  it `built = true` in `steps.toml` in the same change. `tests/test_steps.py` enforces both ways.
- Slugs are the Live Lab slugs in `lwp-academy/src/content/labs.ts`.
- Secrets live in `.env`, loaded by `uv run --env-file`; never on a command line.
