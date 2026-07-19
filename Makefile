PY := .venv/bin/python
PIP := .venv/bin/pip

.PHONY: setup fmt lint type test record-tests audit scan secret-scan quality backtest-sample paper-sim fixtures archive archive-check clean

setup:
	python3 -m venv .venv
	$(PIP) install -U pip
	$(PIP) install -r requirements-dev.txt
	$(PIP) install -e .

fmt:
	.venv/bin/ruff format src tests scripts

lint:
	.venv/bin/ruff format --check src tests scripts
	.venv/bin/ruff check src tests scripts

type:
	.venv/bin/mypy

test:
	$(PY) -m pytest tests -q

record-tests:
	$(PY) scripts/record_test_run.py

audit:
	.venv/bin/pip-audit --strict -r requirements.txt

scan:
	.venv/bin/bandit -c pyproject.toml -r src -q

secret-scan:
	$(PY) scripts/secret_scan.py

quality: lint type scan audit secret-scan record-tests

archive:
	$(PY) scripts/build_archive.py

archive-check:
	$(PY) scripts/build_archive.py
	$(PY) scripts/verify_release_archive.py dist/trading-bot-source.tar.gz

fixtures:
	$(PY) scripts/generate_fixtures.py

backtest-sample:
	PYTHONPATH=src $(PY) -m trading_bot --config config/paper.yaml backtest run \
		--data data/fixtures/btcusdt_1h.csv

paper-sim:
	@# offline paper simulation over fixture data (no network, no credentials)
	PYTHONPATH=src $(PY) -m trading_bot --config config/paper.fixture.yaml paper run --cycles 500

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage var/tmp
