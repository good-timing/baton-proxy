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

# Fast subset for tight iteration loops — skips tests marked `slow`,
# `integration` or `perf`. Equivalent to `make test` until those markers are
# applied. Every marker whose description claims exclusion here has to be in
# this expression; tests/test_tooling.py pins that.
test-fast:
	$(BIN)/pytest -q -m "not slow and not integration and not perf"

# `.` and not `src/ tests/` — try/kit.py is shipped code a prospect's security
# reviewer reads, and it lived outside every local gate. `.` is also literally
# what CI runs, so neither side can be the wider one. pyproject's
# extend-exclude keeps the baton-spec submodule out.
lint:
	$(BIN)/ruff check .

format:
	$(BIN)/ruff format .

format-check:
	$(BIN)/ruff format --check .

# CI gate — mirrors .github/workflows/test.yml so a green local `make ci`
# predicts a green PR. No typecheck target (mypy is not configured here).
ci: lint format-check test

clean:
	rm -rf .pytest_cache .ruff_cache .coverage
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
