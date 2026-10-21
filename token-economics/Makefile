.DEFAULT_GOAL := help
SHELL := /bin/bash
# The board, its cards and the model come from mission-control, the app every Lightning extends, and so
# does its .env, so the model key lives in one place. The reader needs a model that reads photos, so this
# session names its own, with its list price per million tokens; these win over the .env. The demo
# recipe is silent, so a recording shows the reads, never the model's name.
RUN := PYTHONPATH=../mission-control uv run --quiet
KEYS := --env-file ../mission-control/.env
MODEL := WORKFORCE_MODEL=openai/gpt-6-luna WORKFORCE_PRICE_IN=0.10 WORKFORCE_PRICE_CACHED=0.01 WORKFORCE_PRICE_OUT=0.50

.PHONY: help check demo board

help: ## List every target
	@grep -E '^[a-z0-9-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[33m%-8s\033[0m %s\n", $$1, $$2}'

check: ## Lint, format, and the reader's acceptance tests (real claims system, a scripted model, no keys)
	uv run --quiet ruff check .
	uv run --quiet ruff format --check .
	uv run --quiet pytest

demo: ## Read the three claims four times, one more lever each pass, with a real model
	@$(MODEL) $(RUN) $(KEYS) python -m claims.reader

board: ## Serve the board from the last make demo on http://127.0.0.1:8092
	$(RUN) python -m claims.reader --serve
