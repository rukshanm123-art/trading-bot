"""Strategy construction from validated config. No dynamic code loading."""

from __future__ import annotations

from trading_bot.config.models import StrategyConfig
from trading_bot.strategies.benchmarks import BuyAndHoldStrategy, NoTradeStrategy
from trading_bot.strategies.ema_trend import EmaTrendStrategy
from trading_bot.strategies.interface import Strategy


def build_strategy(cfg: StrategyConfig) -> Strategy:
    if cfg.name == "ema_trend":
        return EmaTrendStrategy(cfg.params, version=cfg.version)
    if cfg.name == "buy_and_hold":
        return BuyAndHoldStrategy()
    if cfg.name == "no_trade":
        return NoTradeStrategy()
    raise ValueError(f"unknown strategy {cfg.name}")  # unreachable: config validates
