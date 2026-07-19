"""Small utility behavior that supports the safety envelope."""

from __future__ import annotations

import runpy

import pytest

from tests.helpers import T0, make_candles, make_config
from trading_bot.config.models import StrategyConfig
from trading_bot.core.enums import SignalAction
from trading_bot.exchange.interface import FrozenClock
from trading_bot.security.redaction import GLOBAL_REDACTOR
from trading_bot.security.secrets import EnvSecretProvider, StaticSecretProvider
from trading_bot.strategies.benchmarks import BuyAndHoldStrategy, NoTradeStrategy
from trading_bot.strategies.ema_trend import EmaTrendStrategy
from trading_bot.strategies.registry import build_strategy


def test_strategy_registry_builds_only_known_static_strategies() -> None:
    cfg = make_config().strategy
    assert isinstance(build_strategy(cfg), EmaTrendStrategy)
    assert isinstance(
        build_strategy(StrategyConfig(name="buy_and_hold", version="1.0.0", params=cfg.params)),
        BuyAndHoldStrategy,
    )
    assert isinstance(
        build_strategy(StrategyConfig(name="no_trade", version="1.0.0", params=cfg.params)),
        NoTradeStrategy,
    )
    with pytest.raises(ValueError):
        build_strategy(
            StrategyConfig.model_construct(name="unknown", version="1", params=cfg.params)
        )


def test_benchmark_strategies_are_deterministic_non_live_references() -> None:
    candles = make_candles(["100", "101", "102"])
    buy_hold = BuyAndHoldStrategy()
    assert buy_hold.evaluate(candles, has_position=False).action == SignalAction.ENTER_LONG
    assert buy_hold.evaluate(candles, has_position=True).action == SignalAction.HOLD
    assert NoTradeStrategy().evaluate(candles, has_position=False).action == SignalAction.HOLD


def test_secret_providers_strip_register_and_require_values() -> None:
    provider = EnvSecretProvider({"TOKEN": "  secret-token-012345  ", "BLANK": "   "})
    assert provider.get("TOKEN") == "secret-token-012345"
    assert provider.get("BLANK") is None
    assert provider.require("TOKEN") == "secret-token-012345"
    with pytest.raises(KeyError):
        provider.require("MISSING")
    assert "secret-token-012345" not in GLOBAL_REDACTOR.redact("secret-token-012345")

    static = StaticSecretProvider({"KEY": "static-secret-012345"})
    assert static.get("KEY") == "static-secret-012345"
    assert "static-secret-012345" not in GLOBAL_REDACTOR.redact("static-secret-012345")


def test_python_module_entry_point_delegates_to_cli_main(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("trading_bot.cli.main.main", lambda: 7)
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("trading_bot", run_name="__main__")
    assert exc.value.code == 7


def test_instance_lock_acquire_deny_steal_and_release(db) -> None:
    from trading_bot.engine.scheduler import InstanceLock

    clock = FrozenClock(T0)
    lock_a = InstanceLock(db, clock)
    assert lock_a.acquire()
    lock_a.heartbeat()

    lock_b = InstanceLock(db, clock)
    assert not lock_b.acquire()

    clock.advance(121)
    assert lock_b.acquire()
    lock_b.release()
    assert not lock_b.acquired

    assert lock_a.acquire()
    lock_a.release()


def test_interval_task_skips_until_due_and_swallows_task_exception(caplog) -> None:
    from trading_bot.engine.scheduler import IntervalTask

    clock = FrozenClock(T0)
    calls = 0

    def fn() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("scheduled failure")

    task = IntervalTask(60, fn, clock, "sample")
    assert task.maybe_run() is True
    assert task.maybe_run() is False
    clock.advance(61)
    assert task.maybe_run() is True
    assert "scheduled task sample failed" in caplog.text
