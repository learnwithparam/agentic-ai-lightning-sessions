.DEFAULT_GOAL := help
SHELL := /bin/bash
# The board, its cards and the model come from mission-control, the app every Lightning extends, and so
# does its .env, so the model key lives in one place. `make file` runs Claude Code on your own login,
# pinned to one small model in brain/filer.py. The demo recipes are silent, so a recording shows the
# brain's calls, never the model's name.
RUN := PYTHONPATH=../mission-control uv run --quiet
KEYS := --env-file ../mission-control/.env

.PHONY: help check file demo naive board

help: ## List every target
	@grep -E '^[a-z0-9-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[33m%-8s\033[0m %s\n", $$1, $$2}'

check: ## Lint, format, and the brain's acceptance tests (real vault and MCP server, a scripted model, no keys)
	uv run --quiet ruff check .
	uv run --quiet ruff format --check .
	uv run --quiet pytest

file: ## Claude Code files Hannah's inbox into a fresh brain, then the vault checks every quote
	@$(RUN) python -m brain.filer

demo: ## The researcher answers the board's three questions from the brain, over MCP, with a real model
	@$(RUN) $(KEYS) python -m brain.answerer

naive: ## The same model answers the same questions with no brain
	@$(RUN) $(KEYS) python -m brain.answerer --naive

board: ## Serve the board from the last make demo on http://127.0.0.1:8093
	$(RUN) python -m brain.answerer --serve
