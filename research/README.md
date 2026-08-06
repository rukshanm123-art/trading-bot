# Research module (offline)

Investigates whether an **EMA meta-label** has an edge: the existing EMA
strategy proposes entries, a model only ACCEPTS or REJECTS them. The model
never sizes positions and never reaches an exchange.

**This module is never imported by the trading engine.** The dependency
direction is one-way — `research/` imports `trading_bot`, never the reverse —
so nothing here can change live behaviour. Training happens offline; the bot
would only ever load a frozen, versioned artifact.

`research_spec.yaml` is the pre-registered contract. Its SHA-256 is recorded
in every ledger entry. Changing it — especially the EMA parameters or the exit
rules, which *define* the label — starts a NEW experiment series.

## ⚠ Holdout status: 2018-01 .. 2026-07 is BURNED

It is **not** an untouched final test, and an earlier version of this README
wrongly said it was. The first version of `step0_count_events.py` reported
expectancy, profit factor and buy-and-hold across **all** events including
2025–2026 before any model was fitted, and `step2_walkforward.py` asserted the
later years were *present* and printed a positive rate spanning them — the
opposite of holding them out.

Measured afterwards the burned slice resembles the rest (positive rate 22.33%
through 2024 vs 22.50% in 2025–26), so it very likely did not change the
NO PROMOTION conclusion. But "probably harmless" is not "untouched", and only
the latter is a valid holdout.

Both scripts now hard-scope to `OBSERVED_THROUGH = 2024` and report how many
later events were excluded. **A genuine holdout accrues prospectively from
2026-08-01**, may be evaluated once, and only after it holds ≥30 events.

## Run

```bash
python3 research/import_binance.py        # verified archive -> research/data/
python3 research/step0_count_events.py    # event supply + baseline economics
python3 research/step0b_regime_split.py   # is trade quality conditional?
python3 research/step1_atr_stop.py        # does a volatility-scaled stop help?
python3 research/step2_walkforward.py     # the classifier  [--stress]
```

Steps 0/0b/1 are stdlib-only. Step 2 needs
`research/requirements-research.txt` (numpy + scikit-learn), kept deliberately
out of `requirements.txt` so they never enter the trading image.

Bulk candles (`research/data/*.csv`) are gitignored as reproducible; the
**manifest and the experiment ledger are committed**, because a conclusion
that cannot be tied to its inputs is an anecdote.

## Results — all in-scope (≤2024), data sha256 `c598269c…`

**Step 0 — event supply: GO.** 75,096 verified candles yield 1,030 tradeable
events through 2024 (~148/yr, stable in every year). Supply is not the
constraint.

**Step 0 — baseline economics: the EMA entry loses money.**

| | |
|---|---|
| profitable after costs | 230 / 1030 (22.3%) |
| expectancy per trade | **−0.096%** |
| profit factor | **0.918** |
| avg winner / loser | +4.32% / −1.47% |

**Step 0b — trade quality IS conditional** (train years only, all 8 subsets
reported): calm-volatility entries score +0.063%/trade against −0.235% for
volatile ones, and all four splits move expectancy in a consistent direction.

**Step 1 — ATR-scaled stops do NOT repair it.** Widening cuts stop-outs
exactly as predicted (57.7% → 8.1%) yet no k beats the flat 2%. So the
stopped trades were genuinely losing, not noise-stopped winners — the
"stop too tight in volatile regimes" mechanism is falsified. Tradeability at
35 USDT also falls 99.6% → 49.3% as stops widen, because a wider stop shrinks
the position under the exchange minimum.

**Step 2 — the classifier does not beat matched-random. NO PROMOTION**, at
baseline and stressed costs. With purging by exit timestamp and chronological
internal CV, the 2024 fold gives −0.145% at the **16th percentile** of
duration-matched random selection.

> Worth recording: before those two corrections, the same fold showed
> **+0.281% at the 68th percentile** — which reads like a large win against a
> +0.009% baseline. Honest CV removed it. The leaky version would still have
> been rejected (68 < the required 95), but only because the matched-random
> null was there to catch it.

## Defensible conclusion

Under the registered EMA 12/26 BTCUSDT 1h experiment, **no promotable edge was
found after costs.** ATR stops did not repair expectancy; the classifier did
not reliably outperform matched-random selection; cost stress removed the
apparent 2024 improvement.

That supports **do not trade this strategy.** It does **not** show that every
EMA or BTC strategy lacks an edge. The three tests are related rather than
independent — they share the same underlying event set — so they are one
negative result, not three.

Caveats that remain: 1h OHLC cannot resolve intrabar order, so the pessimistic
policy applies throughout (stop wins ties, gap-through fills at the open);
per-trade returns are summed at full allocation, not compounded through the
live position sizer; and at 35 USDT even a real edge of this size would be
worth cents per year.
