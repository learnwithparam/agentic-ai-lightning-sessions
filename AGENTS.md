# Agentic AI Lightning sessions

One folder per free Lightning session; `sessions.toml` is the index.

- `make check` runs `scripts/check_sessions.py`, then each built folder's own `make check`.
- A session is `built` only when its folder exists, its gate passes and its `steps.toml` marks the
  session's slug built. A session not built has no folder. The script enforces both ways.
- Slugs and titles are Maven's; never rename one here without renaming it on Maven.
- Descriptions and READMEs open with the job: the company, the truth outside the model, the metric.
- CI (`.github/workflows/check.yml`) runs `make check` and `make -C mission-control stack-check` on every push.
