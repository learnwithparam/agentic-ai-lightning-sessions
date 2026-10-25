.DEFAULT_GOAL := help
SHELL := /bin/bash
BUILT := $(shell python3 -c "import tomllib;print(' '.join(s['folder'] for s in tomllib.load(open('sessions.toml','rb'))['session'] if s['built']))")

.PHONY: help check quarantine

# A flaky test is marked (or named) quarantine: check skips it and writes reports/<session>.xml for the CTRF step
# in check.yml; quarantine, a check.yml job that never blocks a push, runs only those (pytest exits 5 on none).
# It is -k, not -m: a second -m would replace a session's own -m 'not stack'. A session with strict markers
# registers quarantine before it marks a test.

help: ## List every target
	@grep -E '^[a-z0-9-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[33m%-8s\033[0m %s\n", $$1, $$2}'

check: ## The session index, then every built session's own gate
	python3 scripts/check_sessions.py
	@mkdir -p reports; for d in $(BUILT); do echo "== $$d"; \
	  PYTEST_ADDOPTS="-k 'not quarantine' --junitxml=$(CURDIR)/reports/$$d.xml" $(MAKE) -C $$d check || exit 1; done

quarantine: ## Only the tests marked quarantine, in every built session
	@for d in $(BUILT); do echo "== $$d"; (cd $$d && uv run --quiet pytest -k quarantine); \
	  r=$$?; [ $$r = 0 ] || [ $$r = 5 ] || exit 1; done
