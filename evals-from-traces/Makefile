.DEFAULT_GOAL := help
SHELL := /bin/bash
# The board and the trace export come from mission-control, the app every Lightning extends, and so does
# its .env, so the model and Langfuse keys live in one place. The desk, the candidate and the judge each
# name their model in evals/; the recipes are silent, so a recording shows the evals, never a model name.
# Langfuse's local keys from mission-control's compose.yaml, the same as its STACK_ENV; not real secrets.
LANGFUSE := LANGFUSE_HOST=http://127.0.0.1:8270 LANGFUSE_PUBLIC_KEY=pk-lf-kaas-local LANGFUSE_SECRET_KEY=sk-lf-kaas-local
RUN := $(LANGFUSE) PYTHONPATH=../mission-control uv run --quiet
KEYS := --env-file ../mission-control/.env

.PHONY: help check runs demo board reset rehearse record

CANDIDATE ?= default
DECK := ../../../internal-apps/lwp-deck

help: ## List every target
	@grep -E '^[a-z0-9-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[33m%-8s\033[0m %s\n", $$1, $$2}'

check: ## Lint, format, and the evals' acceptance tests (scripted models, a fake Langfuse, no keys)
	uv run --quiet ruff check .
	uv run --quiet ruff format --check .
	uv run --quiet pytest

runs: ## A week of inquiries to the production desk, each run traced to Langfuse
	@$(RUN) $(KEYS) python -m evals.run runs

demo: ## Build the test set, score production, rerun on the candidate (CANDIDATE=safe), gate the swap
	@$(RUN) $(KEYS) python -m evals.run demo --candidate $(CANDIDATE)

reset: ## Forget the last run, the signed swap and the decisions, so the next demo starts clean
	rm -rf .evals/production.json .evals/decisions.jsonl .evals/report.json .evals/cache

board: ## Serve the evals and the board from the last make demo on http://127.0.0.1:8094
	@$(RUN) $(KEYS) python -m evals.run serve

rehearse: ## Walk the swap on the running board: Jonas refused, Dr. Okafor signs (make board first)
	@$(RUN) $(KEYS) python rehearse.py

record: ## Record one browser clip of the board: make record CLIP=board
	@$(RUN) $(KEYS) bash -c 'cd $(DECK) && node scripts/record-screen.mjs build-ai-agent-evals-with-production-traces $(CLIP)'
