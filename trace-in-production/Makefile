.DEFAULT_GOAL := help
SHELL := /bin/bash
# The board, the span helpers come from mission-control, the app every Lightning extends, and
# so does its .env, so the model, Langfuse and the sign-in passphrases live in one place. The crew names
# its model in upkeep/crew.py; the recipes are silent, so a recording shows the shift, never a model name.
# Langfuse's local keys from mission-control's compose.yaml, the same as its STACK_ENV; not real secrets.
LANGFUSE := LANGFUSE_HOST=http://127.0.0.1:8270 LANGFUSE_PUBLIC_KEY=pk-lf-kaas-local LANGFUSE_SECRET_KEY=sk-lf-kaas-local
RUN := $(LANGFUSE) PYTHONPATH=../mission-control uv run --quiet
KEYS := --env-file ../mission-control/.env
BUDGET_USD ?= 0.01
KILL_AT ?= A-04
DECK := ../../../internal-apps/lwp-deck

.PHONY: help check naive shift resume traces board rehearse record

help: ## List every target
	@grep -E '^[a-z0-9-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[33m%-8s\033[0m %s\n", $$1, $$2}'

check: ## Lint, format, and the shift's acceptance tests (scripted models, a fake Langfuse, no keys)
	uv run --quiet ruff check .
	uv run --quiet ruff format --check .
	uv run --quiet pytest

naive: ## The shift without the fixes, offline: traces posted at the end, a blind retry (no keys, no Langfuse)
	@PYTHONPATH=../mission-control uv run --quiet python -m upkeep.naive

shift: ## The early shift from a fresh board; killed once KILL_AT's order is written; pauses when over BUDGET_USD
	@BUDGET_USD=$(BUDGET_USD) $(RUN) $(KEYS) python -m upkeep.run shift --kill-at $(KILL_AT)

resume: ## Start the worker again: the killed run is recorded from its trace, and the shift goes on
	@BUDGET_USD=$(BUDGET_USD) $(RUN) $(KEYS) python -m upkeep.run resume

traces: ## Check every card's runs against its traces in Langfuse, per employee
	@$(RUN) python -m upkeep.run traces

board: ## Serve the signed-in board on http://127.0.0.1:8095
	@BUDGET_USD=$(BUDGET_USD) $(RUN) $(KEYS) python -m upkeep.run serve

rehearse: ## Sign in as Erik and Ingrid on the running board and walk the paused shift to Done
	@$(RUN) $(KEYS) python rehearse.py

record: ## Record a deck clip signed in to the board (make record CLIP=board); stop make board first
	$(RUN) $(KEYS) bash -c 'cd $(DECK) && node scripts/record-screen.mjs trace-ai-agents-in-production-with-langfuse $(CLIP)'
