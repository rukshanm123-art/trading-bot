"""Shared EMA meta-label event construction.

Single source of truth for "what is an event and what is its label", imported
by every research step so the definition cannot drift between them. The EMA
itself comes from trading_bot, so it cannot drift from the deployed strategy
either.

Label = whichever RUNTIME exit fires first (protective stop or EMA exit).
No take-profit, no timeout — the running bot has neither. Ambiguity policy
from research_spec.yaml: stop wins ties, gap-through fills at the open,
events spanning a data gap are discarded.
"""

from __future__ import annotations

import csv
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_bot.strategies.interface import ema_series  # noqa: E402

DATA = ROOT / "research" / "data" / "BTCUSDT-1h.csv"
SPEC = ROOT / "research" / "research_spec.yaml"
BPS = Decimal(10000)

SPEC_KEYS = (
    "fast:",
    "slow:",
    "stop_loss_pct:",
    "taker_fee_bps:",
    "spread_bps:",
    "slippage_bps:",
)


def load_spec() -> dict:
    """Minimal reader for the scalars we need (avoids a yaml dependency)."""
    out: dict = {}
    for line in SPEC.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        for key in SPEC_KEYS:
            if s.startswith(key):
                value = s.split(":", 1)[1]
                if " #" in value:  # strip inline comment before parsing
                    value = value.split(" #", 1)[0]
                out[key.rstrip(":")] = value.strip().strip('"')
    return out


def load_candles() -> list[dict]:
    if not DATA.exists():
        raise SystemExit(f"missing {DATA}\nRun: python3 research/import_binance.py")
    rows = []
    with DATA.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            rows.append(
                {
                    "t": datetime.fromisoformat(r["open_time"]),
                    "o": Decimal(r["open"]),
                    "h": Decimal(r["high"]),
                    "low": Decimal(r["low"]),
                    "c": Decimal(r["close"]),
                    "v": Decimal(r["volume"]),
                }
            )
    return rows


def build_events(candles: list[dict], spec: dict) -> tuple[list[dict], int]:
    """Return (events, discarded_for_gap).

    Each event carries signal_idx (the last CLOSED candle at decision time —
    the only bar features may use) and entry_idx (the fill bar).
    """
    fast_n, slow_n = int(spec["fast"]), int(spec["slow"])
    stop_pct = Decimal(spec["stop_loss_pct"])
    fee = Decimal(spec["taker_fee_bps"])
    half_spread = Decimal(spec["spread_bps"]) / 2
    slip = Decimal(spec["slippage_bps"])

    closes = [c["c"] for c in candles]
    n = len(candles)
    fast = ema_series(closes, fast_n)
    slow = ema_series(closes, slow_n)

    def diff_at(i: int) -> Decimal | None:
        fi, si = i - (fast_n - 1), i - (slow_n - 1)
        if fi < 0 or si < 0:
            return None
        return fast[fi] - slow[si]

    contiguous_from = [0] * n
    for i in range(1, n):
        gap = (candles[i]["t"] - candles[i - 1]["t"]).total_seconds() != 3600
        contiguous_from[i] = i if gap else contiguous_from[i - 1]

    entry_cost = (half_spread + slip) / BPS
    exit_cost = (half_spread + slip) / BPS
    round_trip_fees = (fee * 2) / BPS

    events: list[dict] = []
    discarded_gap = 0
    i = slow_n
    while i < n - 1:
        d_now, d_prev = diff_at(i), diff_at(i - 1)
        if d_now is None or d_prev is None or not (d_prev <= 0 < d_now):
            i += 1
            continue

        entry_idx = i + 1
        eff_entry = candles[entry_idx]["o"] * (Decimal(1) + entry_cost)
        stop_price = eff_entry * (Decimal(1) - stop_pct / Decimal(100))

        exit_idx, exit_px, reason = None, None, None
        j = entry_idx
        while j < n - 1:
            if contiguous_from[j] > entry_idx:
                break
            bar = candles[j]
            if j > entry_idx and bar["low"] <= stop_price:
                exit_px = bar["o"] if bar["o"] < stop_price else stop_price
                exit_idx, reason = j, "stop"
                break
            dj, dj_prev = diff_at(j), diff_at(j - 1)
            if dj is not None and dj_prev is not None and j > entry_idx:
                if (dj_prev >= 0 > dj) or dj < 0:
                    exit_idx, exit_px, reason = j + 1, candles[j + 1]["o"], "ema_exit"
                    break
            j += 1

        if exit_idx is None or exit_px is None:
            discarded_gap += 1
            i += 1
            continue

        eff_exit = exit_px * (Decimal(1) - exit_cost)
        net = (eff_exit / eff_entry) - Decimal(1) - round_trip_fees
        events.append(
            {
                "signal_idx": i,
                "entry_idx": entry_idx,
                "entry_time": candles[entry_idx]["t"],
                "hold_hours": exit_idx - entry_idx,
                "exit_reason": reason,
                "net_return": net,
                "profitable": net > 0,
            }
        )
        i = max(exit_idx, i + 1)

    return events, discarded_gap
