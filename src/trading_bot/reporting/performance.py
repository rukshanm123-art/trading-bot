"""Performance metrics and benchmark comparisons.

Money stays Decimal end-to-end; ratio statistics (Sharpe/Sortino/Calmar) are
computed in float because they are analytics, not accounting — documented in
docs/BACKTESTING.md.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from trading_bot.core.types import HUNDRED, ZERO, dec

PERIODS_PER_YEAR_DAILY = 365  # crypto trades every day


def trade_stats(realized_pnls: Sequence[Decimal], fees: Sequence[Decimal]) -> dict[str, Any]:
    wins = [p for p in realized_pnls if p > ZERO]
    losses = [p for p in realized_pnls if p < ZERO]
    n = len(realized_pnls)
    gross_profit = sum(wins, ZERO)
    gross_loss = -sum(losses, ZERO)
    win_rate = Decimal(len(wins)) / Decimal(n) * HUNDRED if n else ZERO
    profit_factor: Decimal | None = None
    if gross_loss > ZERO:
        profit_factor = gross_profit / gross_loss
    expectancy = (sum(realized_pnls, ZERO) / Decimal(n)) if n else ZERO
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": str(win_rate.quantize(Decimal("0.01")) if n else ZERO),
        "gross_profit": str(gross_profit),
        "gross_loss": str(gross_loss),
        "profit_factor": str(profit_factor.quantize(Decimal("0.001"))) if profit_factor else None,
        "expectancy": str(expectancy),
        "avg_win": str(gross_profit / Decimal(len(wins))) if wins else None,
        "avg_loss": str(-(gross_loss / Decimal(len(losses)))) if losses else None,
        "total_fees": str(sum(fees, ZERO)),
        "net_pnl": str(sum(realized_pnls, ZERO)),
    }


def max_drawdown_pct(equity_curve: Sequence[Decimal]) -> Decimal:
    peak = None
    worst = ZERO
    for eq in equity_curve:
        if peak is None or eq > peak:
            peak = eq
        if peak and peak > ZERO:
            dd = (peak - eq) / peak * HUNDRED
            if dd > worst:
                worst = dd
    return worst


def _returns(equity_curve: Sequence[Decimal]) -> list[float]:
    out: list[float] = []
    for prev, cur in itertools.pairwise(equity_curve):
        if prev > ZERO:
            out.append(float((cur - prev) / prev))
    return out


def ratio_metrics(
    equity_curve: Sequence[Decimal], periods_per_year: int = PERIODS_PER_YEAR_DAILY
) -> dict[str, float | None]:
    rets = _returns(equity_curve)
    if len(rets) < 2:
        return {"sharpe": None, "sortino": None, "calmar": None, "annualized_return_pct": None}
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    std = math.sqrt(var)
    downside = [r for r in rets if r < 0]
    dvar = sum(r**2 for r in downside) / len(rets) if downside else 0.0
    dstd = math.sqrt(dvar)

    ann_factor = math.sqrt(periods_per_year)
    sharpe = (mean / std) * ann_factor if std > 0 else None
    sortino = (mean / dstd) * ann_factor if dstd > 0 else None

    total_return = (
        float(equity_curve[-1] / equity_curve[0]) - 1.0 if equity_curve[0] > ZERO else 0.0
    )
    years = len(rets) / periods_per_year
    annualized = ((1 + total_return) ** (1 / years) - 1) * 100 if years > 0.02 else None
    mdd = float(max_drawdown_pct(equity_curve))
    calmar = (annualized / mdd) if (annualized is not None and mdd > 0) else None
    return {
        "sharpe": round(sharpe, 3) if sharpe is not None else None,
        "sortino": round(sortino, 3) if sortino is not None else None,
        "calmar": round(calmar, 3) if calmar is not None else None,
        "annualized_return_pct": round(annualized, 2) if annualized is not None else None,
    }


def benchmark_comparison(
    start_equity: Decimal,
    current_equity: Decimal,
    start_price: Decimal | None,
    current_price: Decimal | None,
    fee_bps: Decimal = Decimal("10"),
) -> dict[str, Any]:
    """Strategy vs buy-and-hold vs stay-in-cash, from the same starting equity."""
    strategy_return = (current_equity / start_equity - 1) * HUNDRED if start_equity > ZERO else ZERO
    bah_return: Decimal | None = None
    if start_price and current_price and start_price > ZERO:
        # one taker buy at the start; fee reduces the invested amount
        invested = (Decimal(10000) - fee_bps) / Decimal(10000)
        bah_return = (current_price / start_price * invested - 1) * HUNDRED
    return {
        "strategy_return_pct": str(strategy_return.quantize(Decimal("0.0001"))),
        "buy_and_hold_return_pct": str(bah_return.quantize(Decimal("0.0001")))
        if bah_return is not None
        else None,
        "no_trade_return_pct": "0",
        "beats_buy_and_hold": bool(bah_return is not None and strategy_return > bah_return),
        "beats_cash": bool(strategy_return > ZERO),
    }


def equity_curve_from_rows(rows: Sequence[dict[str, Any]], key: str = "equity") -> list[Decimal]:
    return [dec(str(r[key])) for r in rows if r.get(key) is not None]
