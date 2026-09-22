SHELL := /bin/bash
MAKEFLAGS += --no-print-directory

# Prefer the project virtualenv when present; otherwise fall back to system python3.
PYTHON ?= $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)
PREFLIGHT := scripts/preflight.py
ARCH_CHECK := scripts/arch-check.py

.PHONY: help preflight env-check deps db-ping app-check arch-check check

help:
	@echo "DanyAPI targets:"
	@echo "  preflight    Run all readiness checks"
	@echo "  env-check    Validate provider credentials"
	@echo "  deps         Install and verify runtime dependencies"
	@echo "  db-ping      Verify JsonStore persistence"
	@echo "  app-check    Verify the FastAPI app imports (no DB migrations in this project)"
	@echo "  arch-check   Verify architectural invariants"
	@echo "  check        Lint, then arch-check, then syntax check"

preflight:
	@echo "== preflight =="
	@$(MAKE) env-check
	@$(MAKE) deps
	@$(MAKE) db-ping
	@$(MAKE) app-check
	@echo "Preflight OK"

env-check:
	@$(PYTHON) $(PREFLIGHT) env-check

deps:
	@$(PYTHON) $(PREFLIGHT) deps

db-ping:
	@$(PYTHON) $(PREFLIGHT) db-ping

app-check:
	@$(PYTHON) $(PREFLIGHT) app-check

arch-check:
	@$(PYTHON) $(ARCH_CHECK)

check:
	@ruff check
	@$(MAKE) arch-check
	@$(PYTHON) -m compileall -q danyapi