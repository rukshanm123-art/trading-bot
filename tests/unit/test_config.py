"""Config validation: floats refused, hard caps enforced, mode rules applied,
tamper-detection via sidecar checksum."""

import pytest

from tests.helpers import make_config
from trading_bot.config.loader import ConfigError, load_config, write_sidecar_checksum


def test_defaults_are_the_conservative_spec_values(base_cfg):
    r = base_cfg.risk
    assert str(r.max_position_allocation_pct) == "20"
    assert str(r.min_cash_reserve_pct) == "50"
    assert str(r.max_risk_per_trade_pct) == "0.5"
    assert str(r.max_daily_loss_pct) == "2"
    assert str(r.max_7d_loss_pct) == "5"
    assert str(r.max_drawdown_pct) == "8"
    assert r.max_entries_per_day == 2
    assert r.cooldown_after_loss_hours == 12
    assert r.pause_after_consecutive_losses == 3


def test_floats_rejected_for_financial_values():
    with pytest.raises(Exception, match="quoted strings"):
        make_config(risk={"max_daily_loss_pct": 2.5})


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_risk_per_trade_pct", "2"),  # cap 1.0
        ("max_daily_loss_pct", "5"),  # cap 3
        ("max_drawdown_pct", "15"),  # cap 10
        ("max_position_allocation_pct", "40"),  # cap 25
        ("min_cash_reserve_pct", "10"),  # floor 30
        ("max_7d_loss_pct", "9"),  # cap 6
    ],
)
def test_hard_caps_cannot_be_loosened(field, value):
    with pytest.raises(Exception, match="hard safety caps"):
        make_config(risk={field: value})


def test_max_open_positions_capped_at_one():
    with pytest.raises(Exception, match="hard safety caps"):
        make_config(risk={"max_open_positions": 2})


def test_live_requires_daily_approval_unless_acknowledged():
    with pytest.raises(Exception, match="DAILY_APPROVAL"):
        make_config(mode="live", continuation={"mode": "auto_continue"})
    cfg = make_config(
        mode="live",
        db={"url": "postgresql://localhost/trading_bot"},
        notifications={"telegram": {"enabled": True}},
        continuation={"mode": "auto_continue", "acknowledge_auto_continue_risk": True},
    )
    assert cfg.mode.value == "live"


def test_live_configuration_requires_postgres_and_external_alerts():
    with pytest.raises(Exception, match="PostgreSQL"):
        make_config(mode="live", continuation={"mode": "daily_approval"})
    with pytest.raises(Exception, match="external notification"):
        make_config(
            mode="live",
            db={"url": "postgresql://localhost/trading_bot"},
            continuation={"mode": "daily_approval"},
        )


def test_locked_live_config_is_structurally_safe_and_sqlite_override_is_rejected():
    from pathlib import Path

    live_path = Path(__file__).resolve().parents[2] / "config" / "live.locked.yaml"
    cfg = load_config(live_path, env={})
    assert cfg.mode.value == "live"
    assert cfg.db.url.startswith("postgresql://")
    assert cfg.notifications.telegram.enabled

    with pytest.raises(ConfigError, match="PostgreSQL"):
        load_config(live_path, env={"DATABASE_URL": "sqlite:///unsafe-live.db"})


def test_fixture_source_paper_only():
    with pytest.raises(Exception, match="fixture"):
        make_config(mode="testnet", data={"source": "fixture", "fixture_path": "x.csv"})


def test_symbol_normalised():
    assert make_config(symbol="btc/usdt").symbol == "BTCUSDT"


def test_bad_interval_rejected():
    with pytest.raises(Exception):
        make_config(interval="7m")


def test_bad_timezone_rejected():
    with pytest.raises(Exception):
        make_config(timezone="Mars/OlympusMons")


def test_unknown_keys_rejected():
    with pytest.raises(Exception):
        make_config(surprise_flag=True)


# ------------------------------------------------------------- loader
BASIC_YAML = """
mode: paper
symbol: BTCUSDT
interval: 1h
"""


def test_loader_reads_yaml(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(BASIC_YAML, encoding="utf-8")
    cfg = load_config(p, env={})
    assert cfg.symbol == "BTCUSDT"


def test_loader_env_database_url_override(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(BASIC_YAML, encoding="utf-8")
    cfg = load_config(p, env={"DATABASE_URL": "sqlite:///elsewhere.db"})
    assert cfg.db.url == "sqlite:///elsewhere.db"


def test_sidecar_checksum_detects_tampering(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(BASIC_YAML, encoding="utf-8")
    write_sidecar_checksum(p)
    load_config(p, env={})  # ok
    p.write_text(BASIC_YAML + "\n# tampered\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="checksum"):
        load_config(p, env={})


def test_missing_config_errors(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml", env={})
