"""Backtesting = the real engine replayed over fixture data.

There is no separate simulation code path: a backtest constructs the SAME
TradingEngine (paper adapter, risk engine, gateway, accounting) with a
fixture data source, an in-memory database and the fixture-driven clock, so
signal logic, sizing, fees, filters and every risk limit behave exactly as in
paper/live operation. Delayed execution is inherent: signals fire on a closed
candle and fill against the NEXT candle's quote.

Walk-forward: parameters are chosen on train data, selected on validation
data, and reported on untouched out-of-sample test data — the test segment is
never used for selection.
"""

from __future__ import annotations

import json
import logging
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_bot.config.models import AppConfig
from trading_bot.core.enums import Mode
from trading_bot.core.types import ZERO, dec
from trading_bot.reporting.performance import (
    max_drawdown_pct,
    ratio_metrics,
    trade_stats,
)

log = logging.getLogger(__name__)

PARAM_GRID: list[tuple[int, int]] = [(8, 21), (10, 30), (12, 26), (15, 40), (20, 50)]


def _ensure_tmp_dir() -> Path:
    p = Path("var/tmp")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _backtest_config(
    base: AppConfig, data_path: str, fast: int | None, slow: int | None
) -> AppConfig:
    raw = base.model_dump()
    raw["mode"] = "paper"
    raw["data"] = {"source": "fixture", "fixture_path": str(Path(data_path).resolve())}
    raw["db"] = {"url": "sqlite:///:memory:"}
    raw["monitoring"] = {**raw.get("monitoring", {}), "enabled": False}
    raw["notifications"] = {
        "console": False,
        "email": {"enabled": False},
        "telegram": {"enabled": False},
    }
    raw["continuation"] = {
        "mode": "auto_continue",
        "approve_default_hours": 24,
        "acknowledge_auto_continue_risk": False,
    }
    if fast is not None and slow is not None:
        raw["strategy"]["params"]["fast"] = fast
        raw["strategy"]["params"]["slow"] = slow
    return AppConfig.model_validate(raw)


def run_backtest(
    base_cfg: AppConfig,
    data_path: str | Path,
    fast: int | None = None,
    slow: int | None = None,
    label: str = "full",
) -> dict[str, Any]:
    """Replay the engine over the fixture; return the metrics dict."""
    from trading_bot.engine.trader import TradingEngine  # local import: avoid cycle

    cfg = _backtest_config(base_cfg, str(data_path), fast, slow)
    # Isolated root: a kill switch latched INSIDE a replay (e.g. by the
    # circuit breaker) must never write STOP_TRADING into the real project.
    bt_root = Path(tempfile.mkdtemp(prefix="bt-", dir=_ensure_tmp_dir()))
    engine = TradingEngine(
        cfg,
        config_path=None,
        migrations_dir=Path("migrations").resolve(),
        project_root=bt_root,
        close_db_on_shutdown=False,
    )
    fixture = engine.fixture
    if fixture is None:
        raise RuntimeError("backtest engine did not construct a fixture data source")
    n_candles = len(fixture.candles)

    engine.run(max_cycles=n_candles + 10)

    repos = engine.repos
    snaps = repos.db.query(
        "SELECT equity FROM balance_snapshots WHERE mode = ? ORDER BY ts",
        (Mode.PAPER.value,),
    )
    curve = [dec(str(r["equity"])) for r in snaps]
    closed = repos.positions.closed_positions(Mode.PAPER, limit=100000)
    pnls = [dec(str(r["realized_pnl"])) for r in closed if r["realized_pnl"] is not None]
    exit_fees = [dec(str(r["exit_fee"])) for r in closed if r["exit_fee"] is not None]
    entry_fees = [dec(str(r["entry_fee"])) for r in closed if r["entry_fee"] is not None]
    decisions = repos.decisions.count(Mode.PAPER)

    start_equity = curve[0] if curve else cfg.paper.starting_quote
    end_equity = curve[-1] if curve else cfg.paper.starting_quote
    total_return_pct = (
        (end_equity - start_equity) / start_equity * Decimal(100) if start_equity > ZERO else ZERO
    )

    first_close = fixture.candles[0].close
    last_close = fixture.candles[-1].close
    fee_frac = (Decimal(10000) - cfg.paper.taker_fee_bps) / Decimal(10000)
    bah_return_pct = (last_close / first_close * fee_frac - 1) * Decimal(100)

    # exposure: fraction of the tested window with an open position
    total_span = (
        fixture.candles[-1].close_time - fixture.candles[0].open_time
    ).total_seconds() or 1.0
    held = 0.0
    for r in closed:
        opened = datetime.fromisoformat(r["opened_at"])
        closed_at = datetime.fromisoformat(r["closed_at"]) if r["closed_at"] else None
        if closed_at:
            held += (closed_at - opened).total_seconds()
    open_pos = repos.positions.open_position(Mode.PAPER)
    if open_pos is not None:
        held += (
            fixture.candles[-1].close_time.replace(tzinfo=UTC) - open_pos.opened_at
        ).total_seconds()

    turnover = ZERO
    for r in closed:
        turnover += dec(str(r["qty"])) * dec(str(r["avg_entry_price"])) * 2

    periods_per_year = int(365 * 86400 / cfg.interval_seconds)
    ratios = ratio_metrics(curve, periods_per_year) if len(curve) > 3 else {}

    engine.db.close()

    stats = trade_stats(pnls, exit_fees)
    return {
        "label": label,
        "candles": n_candles,
        "interval": cfg.interval,
        "strategy": {
            "name": cfg.strategy.name,
            "fast": cfg.strategy.params.fast,
            "slow": cfg.strategy.params.slow,
            "stop_loss_pct": str(cfg.strategy.params.stop_loss_pct),
        },
        "decisions": decisions,
        "start_equity": str(start_equity),
        "end_equity": str(end_equity),
        "total_return_pct": str(total_return_pct.quantize(Decimal("0.0001"))),
        "buy_and_hold_return_pct": str(bah_return_pct.quantize(Decimal("0.0001"))),
        "no_trade_return_pct": "0",
        "max_drawdown_pct": str(max_drawdown_pct(curve).quantize(Decimal("0.01")))
        if curve
        else "0",
        **ratios,
        **stats,
        "total_entry_fees": str(sum(entry_fees, ZERO)),
        "exposure_time_pct": round(held / total_span * 100, 2),
        "turnover_quote": str(turnover),
        "disclaimer": (
            "Backtested results do not guarantee future performance. Fills are "
            "simulated (spread + bounded slippage + fees); live results will differ."
        ),
    }


def walk_forward(
    base_cfg: AppConfig, data_path: str | Path, work_dir: str | Path = "var/tmp"
) -> dict[str, Any]:
    """60/20/20 train/validation/test split with untouched out-of-sample test."""
    import csv as _csv

    src = Path(data_path)
    with open(src, newline="", encoding="utf-8") as fh:
        rows = list(_csv.DictReader(fh))
    n = len(rows)
    i_train, i_val = int(n * 0.6), int(n * 0.8)
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    slices = {
        "train": rows[:i_train],
        "validation": rows[i_train:i_val],
        "test": rows[i_val:],
    }
    paths: dict[str, Path] = {}
    for name, subset in slices.items():
        p = work / f"wf_{name}.csv"
        with open(p, "w", newline="", encoding="utf-8") as fh:
            writer = _csv.DictWriter(
                fh, fieldnames=["open_time", "open", "high", "low", "close", "volume"]
            )
            writer.writeheader()
            writer.writerows(subset)
        paths[name] = p

    train_results = []
    for fast, slow in PARAM_GRID:
        res = run_backtest(base_cfg, paths["train"], fast, slow, label=f"train {fast}/{slow}")
        train_results.append(res)
        log.info(
            "walk-forward train %s/%s: return %s%%, trades %s",
            fast,
            slow,
            res["total_return_pct"],
            res["trades"],
        )

    # top-2 by train return -> select on validation
    top2 = sorted(train_results, key=lambda r: Decimal(r["total_return_pct"]), reverse=True)[:2]
    val_results = [
        run_backtest(
            base_cfg,
            paths["validation"],
            r["strategy"]["fast"],
            r["strategy"]["slow"],
            label=f"validation {r['strategy']['fast']}/{r['strategy']['slow']}",
        )
        for r in top2
    ]
    best_val = max(val_results, key=lambda r: Decimal(r["total_return_pct"]))
    chosen_fast = best_val["strategy"]["fast"]
    chosen_slow = best_val["strategy"]["slow"]

    test_result = run_backtest(
        base_cfg,
        paths["test"],
        chosen_fast,
        chosen_slow,
        label=f"out-of-sample test {chosen_fast}/{chosen_slow}",
    )

    return {
        "method": "walk-forward 60/20/20 (test segment never used for selection)",
        "grid": PARAM_GRID,
        "train": train_results,
        "validation": val_results,
        "selected_params": {"fast": chosen_fast, "slow": chosen_slow},
        "out_of_sample_test": test_result,
        "honesty_note": (
            "Only the out_of_sample_test section is an unbiased estimate; train/"
            "validation numbers are fitted and WILL overstate performance."
        ),
    }


# ----------------------------------------------------------------------
def cmd_backtest(args) -> int:
    """CLI entry (trading-bot backtest run)."""
    from trading_bot.config.loader import load_config

    cfg = load_config(args.config)
    if args.walk_forward:
        result = walk_forward(cfg, args.data)
        kind = "walkforward"
    else:
        result = run_backtest(cfg, args.data)
        kind = "backtest"

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out = Path(args.out or f"var/reports/backtest-{stamp}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")

    # register in DB so the live gate can see a backtest exists
    from trading_bot.storage.db import Database
    from trading_bot.storage.repositories import Repositories

    db = Database(cfg.db.url)
    db.migrate(Path("migrations"))
    Repositories(db).reports.insert(
        stamp[:8],
        "backtest",
        Mode.PAPER,
        f"see {out}",
        result
        if kind == "backtest"
        else {"walkforward": True, "out_of_sample_test": result.get("out_of_sample_test")},
    )
    db.close()

    print(json.dumps(result, indent=2, default=str))
    print(f"\nsaved: {out}")
    print("\nNOTE: backtested results do not guarantee future performance.")
    return 0
