.DEFAULT_GOAL := help
SHELL := /bin/bash
# The board, the span helpers come from mission-control, the app every Lightning extends, and
# so does its .env, so the model and Langfuse keys live in one place. The crew names its model in
# upkeep/crew.py; the recipes are silent, so a recording shows the shift, never a model name.
# Langfuse's local keys from mission-control's compose.yaml, the same as its STACK_ENV; not real secrets.
LANGFUSE := LANGFUSE_HOST=http://127.0.0.1:8270 LANGFUSE_PUBLIC_KEY=pk-lf-acme-local LANGFUSE_SECRET_KEY=sk-lf-acme-local
RUN := $(LANGFUSE) PYTHONPATH=../mission-control uv run --quiet
KEYS := --env-file ../mission-control/.env

.PHONY: help check shift resume traces board

help: ## List every target
	@grep -E '^[a-z0-9-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[33m%-8s\033[0m %s\n", $$1, $$2}'

check: ## Lint, format, and the shift's acceptance tests (scripted models, a fake Langfuse, no keys)
	uv run --quiet ruff check .
	uv run --quiet ruff format --check .
	uv run --quiet pytest

shift: ## The early shift from a fresh board; the worker is killed once A-04's repair order is written
	@$(RUN) $(KEYS) python -m upkeep.run shift --kill-at A-04

resume: ## Start the worker again: the killed run is recorded from its trace, and the shift goes on
	@$(RUN) $(KEYS) python -m upkeep.run resume

traces: ## Check every card's runs against its traces in Langfuse, per employee
	@$(RUN) python -m upkeep.run traces

board: ## Serve the board on http://127.0.0.1:8095
	$(RUN) python -m upkeep.run serve
