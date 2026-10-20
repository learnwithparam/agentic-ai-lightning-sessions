.DEFAULT_GOAL := help
SHELL := /bin/bash
BUILT := $(shell python3 -c "import tomllib;print(' '.join(s['folder'] for s in tomllib.load(open('sessions.toml','rb'))['session'] if s['built']))")

.PHONY: help check

help: ## List every target
	@grep -E '^[a-z0-9-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[33m%-8s\033[0m %s\n", $$1, $$2}'

check: ## The session index, then every built session's own gate
	python3 scripts/check_sessions.py
	@for d in $(BUILT); do echo "== $$d"; $(MAKE) -C $$d check || exit 1; done
