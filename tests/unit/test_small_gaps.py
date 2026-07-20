"""Cheap coverage for small, high-value branches: kill-switch file reason,
config loader edge cases, market-data recovery, DB helpers."""

import pytest

from tests.helpers import make_config
from trading_bot.config import constants as C
from trading_bot.control.killswitch import KillSwitch
from trading_bot.core.enums import KillSwitchSource


# ---------------------------------------------------------- kill switch
def test_file_reason_is_read_from_stop_file(repos, tmp_path):
    (tmp_path / C.STOP_FILE_NAME).write_text("market looks wrong", encoding="utf-8")
    ks = KillSwitch(repos, tmp_path, env={})
    active, reason = ks.check()
    assert active
    assert "market looks wrong" in reason


def test_db_flag_non_json_legacy_value(repos, tmp_path):
    # a legacy/plain "true" flag (not JSON) must still be honoured
    repos.flags.set(repos.flags.KILL_SWITCH, "true")
    ks = KillSwitch(repos, tmp_path, env={})
    assert ks.check()[0]


def test_activate_writes_file_and_event(repos, tmp_path):
    ks = KillSwitch(repos, tmp_path, env={})
    ks.activate(KillSwitchSource.CIRCUIT_BREAKER, "drawdown breach")
    assert (tmp_path / C.STOP_FILE_NAME).exists()
    active, reason = ks.check()
    assert active and "drawdown breach" in reason
    events = repos.db.query("SELECT * FROM killswitch_events")
    assert events


# ---------------------------------------------------------- config loader
def test_loader_bad_yaml_and_env_override(tmp_path):
    from trading_bot.config.loader import ConfigError, load_config

    bad = tmp_path / "bad.yaml"
    bad.write_text("mode: paper\n  bad: : :\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(bad, env={})

    good = tmp_path / "good.yaml"
    good.write_text("mode: paper\nsymbol: BTCUSDT\ninterval: 1h\n", encoding="utf-8")
    cfg = load_config(good, env={"DATABASE_URL": "sqlite:///override.db"})
    assert cfg.db.url == "sqlite:///override.db"


def test_loader_non_mapping_top_level(tmp_path):
    from trading_bot.config.loader import ConfigError, load_config

    p = tmp_path / "list.yaml"
    p.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(p, env={})


def test_config_hash_stable():
    from trading_bot.config.loader import config_hash

    cfg = make_config()
    assert config_hash(cfg) == config_hash(cfg)
    assert config_hash(cfg) != config_hash(make_config(symbol="ETHUSDT"))


# ---------------------------------------------------------- market data
def test_market_data_recovers_after_failures(base_cfg):
    from datetime import timedelta

    from tests.helpers import make_candles, make_quote
    from trading_bot.exchange.errors import ExchangeUnavailable
    from trading_bot.exchange.interface import FrozenClock
    from trading_bot.market_data.service import MarketDataService

    class FlakySource:
        def __init__(self):
            self.fail = True

        def get_candles(self, symbol, interval, limit=200):
            if self.fail:
                raise ExchangeUnavailable("down")
            return make_candles(["100"] * 40)

        def get_price(self, symbol):
            if self.fail:
                raise ExchangeUnavailable("down")
            return make_quote("100", ts=self.now)

        now = None

    src = FlakySource()
    clock = FrozenClock(make_candles(["100"] * 40)[-1].close_time + timedelta(seconds=1))
    src.now = clock.now()
    service = MarketDataService(src, base_cfg, clock, min_candles=10)
    service.closed_candles()
    assert service.consecutive_failures >= 1
    # recovery requires N consecutive successes (config default 2)
    src.fail = False
    for _ in range(base_cfg.risk.market_data_recovery_successes):
        service.closed_candles()
    assert service.candle_health.consecutive_failures == 0


# ---------------------------------------------------------- db helpers
def test_db_context_manager_and_rowcount(tmp_path):
    from trading_bot.storage.db import Database, DatabaseError

    db = Database(f"sqlite:///{tmp_path}/ctx.db")
    with db:
        db.execute("CREATE TABLE t (k TEXT PRIMARY KEY)")
        n = db.execute_rowcount("INSERT INTO t (k) VALUES ('a')")
        assert n == 1
    assert db.closed
    with pytest.raises(DatabaseError):
        db.execute("SELECT 1")


def test_db_unsupported_scheme():
    from trading_bot.storage.db import Database, DatabaseError

    with pytest.raises(DatabaseError, match="Unsupported"):
        Database("mysql://nope")


def test_db_rollback_on_error(tmp_path):
    from trading_bot.storage.db import Database

    db = Database(f"sqlite:///{tmp_path}/rb.db")
    db.execute("CREATE TABLE t (k TEXT PRIMARY KEY)")
    db.execute("INSERT INTO t (k) VALUES ('a')")
    with pytest.raises(Exception):
        db.execute("INSERT INTO t (k) VALUES ('a')")  # duplicate PK
    rows = db.query("SELECT COUNT(*) AS n FROM t")
    assert rows[0]["n"] == 1
    db.close()
