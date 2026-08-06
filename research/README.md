# Research module (offline)

Investigates whether an **EMA meta-label** has an edge: the existing EMA
strategy proposes entries, a model only ACCEPTS or REJECTS them. The model
never sizes positions and never reaches an exchange.

**This module is never imported by the trading engine.** The dependency
direction is one-way — `research/` imports `trading_bot`, never the reverse —
so nothing here can change live behaviour. Training happens offline; the bot
would only ever load a frozen, versioned artifact.

`research_spec.yaml` is the pre-registered contract. Its SHA-256 goes in every
experiment ledger entry. Changing it (especially the EMA parameters or the
exit rules, which *define* the label) starts a NEW experiment series — it is
not a tweak.

## Run

```bash
python3 research/import_binance.py        # verified archive -> research/data/
python3 research/step0_count_events.py    # the go/no-go gate
```

Both are stdlib-only. Modelling steps will need numpy/scikit-learn; the
go/no-go deliberately does not.

`research/data/` is gitignored — it is reproducible from the manifest, which
records every file's checksum.

## Step 0 result (2026-08-06, data sha256 `c598269c…`)

| | |
|---|---|
| candles | 75,096 (2018-01-01 → 2026-07-31, 28 gaps, not filled) |
| tradeable EMA events | **1,270** (~148/yr, stable every year) |
| profitable after costs | **284 (22.4%)** |
| EMA expectancy/trade | **−0.172%** |
| profit factor | **0.849** |
| avg winner / loser | +4.32% / −1.47% |
| verdict | **GO** on event supply |

Event supply is sufficient. The finding that matters is the second half: the
EMA baseline **loses money** over 8.6 years after costs, and buy-and-hold
returned +365% over the same window.

That is not a reason to stop — it is what makes meta-labelling well posed.
The break-even win rate at this payoff ratio is **25.4%** against an observed
**22.4%**, so the model has a precise, falsifiable job: lift precision by ~3
points out-of-sample, with margin. If it cannot, there is no edge and the
answer is NO PROMOTION.

Caveats on these numbers: 1h OHLC cannot resolve intrabar order, so the
pessimistic policy applies throughout (stop wins ties, gap-through fills at
the open) — the true expectancy is likely marginally better than shown. Per-
trade returns are summed at full allocation, not compounded through the live
position sizer.
