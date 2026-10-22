.DEFAULT_GOAL := help
SHELL := /bin/bash
# The board and its pages come from mission-control, the app every Lightning extends, and so does its
# .env, so the model key lives in one place. The employees name their model in operating/crew.py; the
# recipes are silent, so a recording shows the week, never a model name.
RUN := PYTHONPATH=../mission-control uv run --quiet
KEYS := --env-file ../mission-control/.env

.PHONY: help check week record board

help: ## List every target
	@grep -E '^[a-z0-9-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[33m%-8s\033[0m %s\n", $$1, $$2}'

check: ## Lint, format, and the policy's acceptance tests (scripted models, no keys)
	uv run --quiet ruff check .
	uv run --quiet ruff format --check .
	uv run --quiet pytest

week: ## Week 51 from a fresh board: each job worked under the policy, every verdict printed
	@$(RUN) $(KEYS) python -m operating.run week

record: ## Check every write in the hotels' systems against the decision record
	@$(RUN) python -m operating.run record

board: ## Serve the board, Team and Month on http://127.0.0.1:8096
	$(RUN) python -m operating.run serve
