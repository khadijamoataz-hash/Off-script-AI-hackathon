PYTHON ?= $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; else echo python3; fi)
HOST ?= 127.0.0.1
PORT ?= 8000

.PHONY: setup up local-up test evals verify-log

setup:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ".[dev]"

up:
	docker compose up

local-up:
	$(PYTHON) -m uvicorn app:app --host $(HOST) --port $(PORT) --reload

test:
	$(PYTHON) -m pytest -q

evals:
	$(PYTHON) -m evals.run

verify-log:
	$(PYTHON) scripts/verify_decision_log.py decision_log.json
