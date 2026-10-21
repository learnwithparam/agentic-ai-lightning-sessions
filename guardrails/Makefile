.DEFAULT_GOAL := help
SHELL := /bin/bash
# The board, its cards and the model come from mission-control, the app every Lightning extends, and so
# does its .env, so the model key lives in one place.
RUN := PYTHONPATH=../mission-control uv run --quiet
KEYS := --env-file ../mission-control/.env

.PHONY: help check demo naive board

help: ## List every target
	@grep -E '^[a-z0-9-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[33m%-8s\033[0m %s\n", $$1, $$2}'

check: ## Lint, format, and the screener's acceptance tests (real hiring system, a scripted model, no keys)
	uv run --quiet ruff check .
	uv run --quiet ruff format --check .
	uv run --quiet pytest

demo: ## The screener shortlists both cards with its guardrails on, with a real model
	$(RUN) $(KEYS) python -m hiring.screener

naive: ## The same cards with both guardrails off, for comparison
	$(RUN) $(KEYS) python -m hiring.screener --naive

board: ## Serve the board from the last make demo on http://127.0.0.1:8090
	$(RUN) python -m hiring.screener --serve
