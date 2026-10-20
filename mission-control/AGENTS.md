# Mission Control

AI employees that resolve Acme's billing issues, built across the Lightning sessions and the buildcamp,
and installed by the KaaS.team Agent Pilot (`internal-apps/plan/specs/08-owned-software.md`).

- `make check` is the gate: ruff, format and pytest. It runs offline with the SDK's `ScriptedModel`.
- A step is built only when its promise is a test marked `@pytest.mark.lab("<slug>")`; mark it
  `built = true` in `steps.toml` in the same change. `tests/test_steps.py` enforces both ways.
- Slugs are the lab slugs in `lwp-academy/src/content/labs.ts`.
- Done is decided by the ledger in `mission.settle`, approval by `mission.decide`; never by a prompt.
- Secrets live in `.env`, loaded by `uv run --env-file`; never on a command line.
