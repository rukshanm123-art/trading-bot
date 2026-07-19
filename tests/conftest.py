"""Shared test fixtures. Also bootstraps src/ onto sys.path so the suite runs
with or without an editable install."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
for p in (str(SRC), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest  # noqa: E402

from trading_bot.config.models import AppConfig  # noqa: E402
from trading_bot.core.models import set_time_provider  # noqa: E402
from trading_bot.monitoring.health import HEALTH  # noqa: E402
from trading_bot.security.redaction import GLOBAL_REDACTOR  # noqa: E402
from trading_bot.storage.db import Database  # noqa: E402
from trading_bot.storage.repositories import Repositories  # noqa: E402

MIGRATIONS = PROJECT_ROOT / "migrations"

_SENSITIVE_ENV = (
    "TRADING_KILL_SWITCH",
    "LIVE_TRADING_ENABLED",
    "BINANCE_TESTNET_API_KEY",
    "BINANCE_TESTNET_API_SECRET",
    "BINANCE_LIVE_API_KEY",
    "BINANCE_LIVE_API_SECRET",
    "DATABASE_URL",
    "MONITORING_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
)


@pytest.fixture(autouse=True)
def _clean_globals(monkeypatch):
    for var in _SENSITIVE_ENV:
        monkeypatch.delenv(var, raising=False)
    set_time_provider(None)
    GLOBAL_REDACTOR.clear()
    HEALTH.reset()
    yield
    set_time_provider(None)
    GLOBAL_REDACTOR.clear()


@pytest.fixture
def db(tmp_path):
    database = Database(f"sqlite:///{tmp_path}/test.db")
    database.migrate(MIGRATIONS)
    yield database
    database.close()


@pytest.fixture
def repos(db):
    return Repositories(db)


@pytest.fixture
def base_cfg() -> AppConfig:
    from tests.helpers import make_config

    return make_config()
