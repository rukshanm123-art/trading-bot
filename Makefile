PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
VENV_PY := .venv/bin/python

.PHONY: setup fmt lint type test record-tests audit scan secret-scan quality backtest-sample paper-sim fixtures archive archive-check clean

setup:
	python3 -m venv .venv
	$(VENV_PY) -m pip install -U pip
	$(VENV_PY) -m pip install -r requirements-dev.txt
	$(VENV_PY) -m pip install -e .

fmt:
	$(PYTHON) -m ruff format src tests scripts

lint:
	$(PYTHON) -m ruff format --check src tests scripts
	$(PYTHON) -m ruff check src tests scripts

type:
	$(PYTHON) -m mypy

test:
	$(PYTHON) -m pytest tests -q

record-tests:
	$(PYTHON) scripts/record_test_run.py

audit:
	$(PYTHON) -m pip_audit --strict -r requirements.txt

scan:
	$(PYTHON) -m bandit -c pyproject.toml -r src -q

secret-scan:
	$(PYTHON) scripts/secret_scan.py

quality: lint type scan audit secret-scan record-tests

archive:
	$(PYTHON) scripts/build_archive.py

archive-check:
	$(PYTHON) scripts/build_archive.py
	$(PYTHON) scripts/verify_release_archive.py dist/trading-bot-source.tar.gz

fixtures:
	$(PYTHON) scripts/generate_fixtures.py

backtest-sample:
	PYTHONPATH=src $(PYTHON) -m trading_bot --config config/paper.yaml backtest run \
		--data data/fixtures/btcusdt_1h.csv

paper-sim:
	@# offline paper simulation over fixture data (no network, no credentials)
	PYTHONPATH=src $(PYTHON) -m trading_bot --config config/paper.fixture.yaml paper run --cycles 500

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage var/tmp
