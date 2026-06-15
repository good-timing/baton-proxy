# Baton Proxy — subprocess-wrap MCP proxy with annotation injection.
# Mirrors baton's Makefile shape so the same allowlisted `make <target>`
# invocations work across the workplace.

PYTHON ?= $(shell command -v python3.13 >/dev/null 2>&1 && echo python3.13 || echo python3)
VENV ?= .venv
BIN = $(VENV)/bin

.PHONY: install test test-fast lint format format-check ci clean

install:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e ".[dev]"

test:
	$(BIN)/pytest -q

# Fast subset for tight iteration loops — skips tests marked `slow` or
# `integration`. Equivalent to `make test` until those markers are applied.
test-fast:
	$(BIN)/pytest -q -m "not slow and not integration"

lint:
	$(BIN)/ruff check src/ tests/

format:
	$(BIN)/ruff format src/ tests/

format-check:
	$(BIN)/ruff format --check src/ tests/

# CI gate — mirrors .github/workflows/test.yml so a green local `make ci`
# predicts a green PR. No typecheck target (mypy is not configured here).
ci: lint format-check test

clean:
	rm -rf .pytest_cache .ruff_cache .coverage
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
