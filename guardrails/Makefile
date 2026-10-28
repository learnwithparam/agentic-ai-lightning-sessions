.DEFAULT_GOAL := help
SHELL := /bin/bash
# The board, its cards and the model come from mission-control, the app every Lightning extends, and so
# does its .env, so the model key lives in one place.
RUN := PYTHONPATH=../mission-control uv run --quiet
KEYS := --env-file ../mission-control/.env

.PHONY: help check demo naive board rehearse record

help: ## List every target
	@grep -E '^[a-z0-9-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[33m%-8s\033[0m %s\n", $$1, $$2}'

check: ## Lint, format, and the screener's acceptance tests (real hiring system, a scripted model, no keys)
	uv run --quiet ruff check .
	uv run --quiet ruff format --check .
	uv run --quiet pytest

demo: ## The screener shortlists both cards with its guardrails on, with a real model
	@$(RUN) $(KEYS) python -m hiring.screener

naive: ## The same card with both guardrails off, for comparison
	@$(RUN) $(KEYS) python -m hiring.screener --naive

board: ## Serve the signed-in board from the last make demo on http://127.0.0.1:8091
	$(RUN) $(KEYS) python -m hiring.screener --serve

rehearse: ## Sign in as Maya and Sam on the running board and walk the flagged CV to Done
	$(RUN) $(KEYS) python rehearse.py

DECK := ../../../internal-apps/lwp-deck
record: ## Record a deck clip signed in to the board (make record CLIP=screener-decide); stop make board first
	$(RUN) $(KEYS) bash -c 'cd $(DECK) && node scripts/record-screen.mjs secure-ai-agents-with-guardrails $(CLIP)'
