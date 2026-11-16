PYTHON ?= $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; else echo python3; fi)
PIP ?= $(shell if [ -x .venv/bin/pip ]; then echo .venv/bin/pip; else echo pip3; fi)
DECISION_LOG ?= DECISIONS.jsonl
HOST ?= 127.0.0.1
PORT ?= 8000

.PHONY: setup up local-up test redteam verify-log demo clean

setup:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ".[dev]"

up:
	@if command -v docker >/dev/null 2>&1; then \
		docker compose up --build; \
	else \
		echo "Docker not installed; starting Foundry locally via uvicorn on http://$(HOST):$(PORT)"; \
		$(PYTHON) -m uvicorn ui.app:app --host $(HOST) --port $(PORT) --reload; \
	fi

local-up:
	$(PYTHON) -m uvicorn ui.app:app --host $(HOST) --port $(PORT) --reload

test:
	$(PYTHON) -m pytest -q

redteam:
	$(PYTHON) -m pytest tests/test_redteam.py -v
	$(PYTHON) scripts/run_redteam.py

verify-log:
	$(PYTHON) scripts/verify_decision_log.py $(DECISION_LOG)

demo:
	$(PYTHON) scripts/run_demo.py

clean:
	rm -rf .pytest_cache data/ sandbox_runs/ exports/
