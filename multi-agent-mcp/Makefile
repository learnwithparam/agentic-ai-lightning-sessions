.DEFAULT_GOAL := help
SHELL := /bin/bash
# The board, its cards and the model come from mission-control, the app every Lightning extends, and so
# does its .env, so the model key lives in one place.
RUN := PYTHONPATH=../mission-control uv run --quiet
KEYS := --env-file ../mission-control/.env

.PHONY: help check demo solo board

help: ## List every target
	@grep -E '^[a-z0-9-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[33m%-8s\033[0m %s\n", $$1, $$2}'

check: ## Lint, format, and the crew's acceptance tests (real gateways, a scripted model, no keys)
	uv run --quiet ruff check .
	uv run --quiet ruff format --check .
	uv run --quiet pytest

demo: ## The trip planner, booker and messenger rebook Priya's cancelled flight with a real model
	$(RUN) $(KEYS) python -m crew.team

solo: ## One agent with every tool works the same card, for comparison
	$(RUN) $(KEYS) python -m crew.team --solo

board: ## Serve the signed-in board on http://127.0.0.1:8090 (make board SOLO=1 for the solo run)
	$(RUN) $(KEYS) python -m crew.team --serve $(if $(SOLO),--solo)
