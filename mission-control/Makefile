.DEFAULT_GOAL := help
SHELL := /bin/bash

.PHONY: help check ask slack demo board

help: ## List every target
	@grep -E '^[a-z0-9-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[33m%-8s\033[0m %s\n", $$1, $$2}'

check: ## Lint, format, and every step's acceptance test (no keys, no model spend)
	uv run --quiet ruff check .
	uv run --quiet ruff format --check .
	uv run --quiet pytest

ask: ## Ask one question as a user: make ask Q="is the double charge known?" AS=U_CONTRACTOR
	WORKFORCE_USER=$(or $(AS),U_ENG) uv run --quiet --env-file .env python -m workforce.cli "$(Q)"

slack: ## Run the analyst in your Slack workspace over Socket Mode
	uv run --quiet --env-file .env python -m workforce.slack_app

demo: ## Work the board; approve on Telegram, or ASK=terminal to approve here. FRESH=1 starts over
	uv run --quiet --env-file .env python -m workforce.mission $(if $(FRESH),--fresh) $(if $(ASK),--ask $(ASK))

board: ## Serve Mission Control on http://127.0.0.1:8080
	uv run --quiet python -m workforce.mission --serve
