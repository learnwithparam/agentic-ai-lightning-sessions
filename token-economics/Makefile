.DEFAULT_GOAL := help
SHELL := /bin/bash
# The board, its cards and the model come from mission-control, the app every Lightning extends, and so
# does its .env, so the model key lives in one place. The reader needs a model that reads photos, so this
# session names its own, with its list price per million tokens; these win over the .env. The demo
# recipe is silent, so a recording shows the reads, never the model's name.
RUN := PYTHONPATH=../mission-control uv run --quiet
KEYS := --env-file ../mission-control/.env
MODEL := WORKFORCE_MODEL=openai/gpt-6-luna WORKFORCE_PRICE_IN=0.10 WORKFORCE_PRICE_CACHED=0.01 WORKFORCE_PRICE_OUT=0.50
PASS ?= 4

.PHONY: help check demo passes ladder board rehearse record

help: ## List every target
	@grep -E '^[a-z0-9-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[33m%-8s\033[0m %s\n", $$1, $$2}'

check: ## Lint, format, and the reader's acceptance tests (real claims system, a scripted model, no keys)
	uv run --quiet ruff check .
	uv run --quiet ruff format --check .
	uv run --quiet pytest

demo: ## Read the three claims once with the levers of one pass (make demo PASS=1 to 4), with a real model
	@$(MODEL) $(RUN) $(KEYS) python -m claims.reader --pass $(PASS)

passes: ## Read the three claims four times into the stored ladder, about 7 minutes; run before the call
	@$(MODEL) $(RUN) $(KEYS) python -m claims.reader --passes

ladder: ## Print the stored ladder, with no model and no keys
	@$(RUN) python -m claims.reader --ladder

board: ## Serve the signed-in board on http://127.0.0.1:8092; an approval reads the rest of the claim
	$(MODEL) $(RUN) $(KEYS) python -m claims.reader --serve

rehearse: ## Sign in as Leo and Nina on the running board and walk the hail claim to Done
	$(RUN) $(KEYS) python rehearse.py

DECK := ../../../internal-apps/lwp-deck
record: ## Record a deck clip signed in to the board (make record CLIP=claims-approve); stop make board first
	$(RUN) $(KEYS) bash -c 'cd $(DECK) && node scripts/record-screen.mjs cut-ai-agent-costs-with-token-economics $(CLIP)'
