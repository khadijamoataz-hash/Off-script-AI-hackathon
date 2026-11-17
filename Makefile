PYTHON ?= $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; else echo python3; fi)
HOST ?= 127.0.0.1
PORT ?= 8000
# Load .env for local runs when it exists (docker compose reads it on its own).
ENV_FILE := $(if $(wildcard .env),--env-file .env,)

.PHONY: setup up local-up test evals verify-log delivery-test

setup:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ".[dev]"

up:
	docker compose up

local-up:
	$(PYTHON) -m uvicorn app:app --host $(HOST) --port $(PORT) --reload $(ENV_FILE)

test:
	$(PYTHON) -m pytest -q

evals:
	$(PYTHON) -m evals.run

verify-log:
	$(PYTHON) scripts/verify_decision_log.py decision_log.json

delivery-test:
	$(PYTHON) scripts/run_delivery_test.py
