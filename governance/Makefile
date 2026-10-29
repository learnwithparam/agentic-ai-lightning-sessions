.DEFAULT_GOAL := help
SHELL := /bin/bash
# The board and its pages come from mission-control, the app every Lightning extends, and so does its
# .env, so the model key and the sign-in passphrases live in one place. The employees name their model in
# operating/crew.py; the recipes are silent, so a recording shows the week, never a model name.
RUN := PYTHONPATH=../mission-control uv run --quiet
KEYS := --env-file ../mission-control/.env
DECK := ../../../internal-apps/lwp-deck

.PHONY: help check demo naive decisions board rehearse record

help: ## List every target
	@grep -E '^[a-z0-9-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[33m%-9s\033[0m %s\n", $$1, $$2}'

check: ## Lint, format, and the policy's acceptance tests (scripted models, no keys)
	uv run --quiet ruff check .
	uv run --quiet ruff format --check .
	uv run --quiet pytest

demo: ## Seven jobs from a fresh board: each worked under the policy, every verdict printed
	@$(RUN) $(KEYS) python -m operating.run demo

naive: ## The same rate job with the gate off, offline: a price under the floor is written (no keys)
	@$(RUN) python -m operating.run naive

decisions: ## Check every write in the hotels' systems against the decision record
	@$(RUN) python -m operating.run decisions

board: ## Serve the signed-in board, Team and Month on http://127.0.0.1:8096
	@$(RUN) $(KEYS) python -m operating.run serve --port 8096

rehearse: ## Sign in as Marta, Ingrid and Lars on the running board
	@$(RUN) $(KEYS) python rehearse.py

record: ## Record a deck clip signed in to the board (make record CLIP=board); stop make board first
	@$(RUN) $(KEYS) bash -c 'cd $(DECK) && node scripts/record-screen.mjs build-an-ai-agent-governance-framework-with-policy-as-code $(CLIP)'
