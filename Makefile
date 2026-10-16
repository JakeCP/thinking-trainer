.DEFAULT_GOAL := help
.PHONY: help setup start ingest run test

PORT ?= 8000
UV_RUN := uv run --env-file .env

help: ## list commands
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  make %-8s %s\n", $$1, $$2}'

start: setup ingest run ## first time: install, build the problem bank, start the app

setup: .env ## install dependencies and create .env
	@command -v uv >/dev/null || { echo "uv is not installed: https://docs.astral.sh/uv/getting-started/installation/"; exit 1; }
	uv sync

ingest: .env ## build the problem bank (LENS="17" or LENS="3 17" for some lenses, ARGS="--re-extract")
	$(UV_RUN) python -m app.ingest $(foreach l,$(LENS),--lens $(l)) $(ARGS)

run: .env ## start the app at http://127.0.0.1:8000 (PORT=8001 to change)
	$(UV_RUN) uvicorn app.web.main:app_factory --factory --reload --port $(PORT)

test: ## run the tests (no network, no API key needed)
	uv run pytest

.env:
	cp .env.example .env
	@echo "Created .env. Optional: add ANTHROPIC_API_KEY to it for Claude extraction and grading."
	@echo "Without a key you grade yourself and copy problems in by hand at /review."
