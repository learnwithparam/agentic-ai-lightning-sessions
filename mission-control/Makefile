.DEFAULT_GOAL := help
SHELL := /bin/bash

.PHONY: help check ask slack demo naive board rehearse record stack stack-check

DECK := ../../../internal-apps/lwp-deck

# The default small model stalls on the support card (C-1) for 90 s, so the board pins one that hands
# the card over. A caller or the env file can still override it.
export WORKFORCE_MODEL ?= openai/gpt-6-luna

# The stack's addresses and its local keys from compose.yaml; nothing here is a real secret.
STACK_ENV := KEYCLOAK_URL=http://127.0.0.1:8180 OPENFGA_URL=http://127.0.0.1:8185 \
	LANGFUSE_HOST=http://127.0.0.1:8270 LANGFUSE_PUBLIC_KEY=pk-lf-kaas-local LANGFUSE_SECRET_KEY=sk-lf-kaas-local

help: ## List every target
	@grep -E '^[a-z0-9-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[33m%-8s\033[0m %s\n", $$1, $$2}'

check: ## Lint, format, and every step's acceptance test (no keys, no model spend)
	uv run --quiet ruff check .
	uv run --quiet ruff format --check .
	uv run --quiet pytest

ask: ## Ask one question as a user: make ask Q="is the export timeout a known bug?" AS=U_CONTRACTOR
	WORKFORCE_USER=$(or $(AS),U_ENG) uv run --quiet --env-file .env python -m workforce.cli "$(Q)"

slack: ## Run the analyst in your Slack workspace over Socket Mode
	uv run --quiet --env-file .env python -m workforce.slack_app

demo: ## Work the three cards, then wait for a support approver. FRESH=1 starts over, STACK=1 uses make stack
	@$(if $(STACK),$(STACK_ENV)) uv run --quiet --env-file .env python -m workforce.mission $(if $(FRESH),--fresh)

naive: ## The same issue with the check off, offline: the card is Done on the agent's word (no keys)
	@uv run --quiet python -m workforce.mission --naive

board: ## Serve Mission Control on http://127.0.0.1:8080; sign in as Param or Sam
	@uv run --quiet --env-file .env python -m workforce.mission --serve

rehearse: ## Sign in as Param and Sam on the running board and check every claim (make demo, make board first)
	@uv run --quiet --env-file .env python rehearse.py

record: ## Record a deck clip signed in to the board: make record CLIP=board; stop make board first
	@uv run --quiet --env-file .env bash -c 'cd $(DECK) && node scripts/record-screen.mjs build-a-mission-control-for-ai-agents $(CLIP)'

stack: ## Start Keycloak, OpenFGA and Langfuse; Langfuse is on http://127.0.0.1:8270 (support@kaas.example)
	docker compose up -d --wait

stack-check: stack ## The deploy promise against the running stack: real tokens, real checks, a real trace
	$(STACK_ENV) uv run --quiet pytest -m stack
