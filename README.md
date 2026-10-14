# Auto-Trading Strategy Lab

Phase 0 of a plan to build a monitored, risk-capped automated trading system for
Indian equities and crypto. This repository is **only the strategy lab**: it
downloads historical data, backtests candidate strategies against buy-and-hold,
and reports the result. It places no orders and holds no credentials.

## Current result: no edge found

Run against 9 instruments from 2018-01-01 to 2026-10-14, the honest out-of-sample
verdict is:

| | |
|---|---|
| Strategy/instrument pairs beating buy-and-hold | 23 of 45 (51%) |
| Median excess return vs buy-and-hold | **+0.44 points** |
| Average trades per pair, out-of-sample | **3.6** |
| Wins that occurred while buy-and-hold was *losing* | 20 of 23 |

A 51% hit rate with a +0.44 point median edge over 3–4 trades is noise, not
skill. Nearly all the "wins" are the mechanical result of sitting in cash during
a downtrend.

The in-sample/out-of-sample gap is the real finding: the same strategies scored
**9 of 45 in-sample** and **23 of 45 out-of-sample**, and the strategies that
looked spectacular in-sample (Donchian and SMA on BTC/ETH, +480 to +610 points of
excess during the 2018-2023 crypto run) lost badly on unseen data (Donchian on
BTC returned +17% against buy-and-hold's +87%). That is the signature of
overfitting, and it is why the project's own rule — a strategy that fails the
unseen-data test does not advance — currently blocks Phase 1 from going live.

## The original idea, and what happened to it

The brief was: *buy when the graph goes up, sell when it goes down.* That rule is
implemented here as `sma-20/100` so its fate is measurable rather than arguable.
It did not work. In a strong uptrend it sat in cash through the best moves — on
BTC out-of-sample it returned **-12.3%** while buy-and-hold returned **+87.6%**.
A confirmation-based signal fires *after* the move, so it systematically buys
late and sells early. See `stratlab/strategies/library.py`.

## Running it

```bash
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python run_lab.py
.\.venv\Scripts\python -m pytest
```

Useful flags:

```bash
python run_lab.py --symbols BTC,TCS     # a subset
python run_lab.py --dev-end 2023-01-01  # move the dev/test boundary
python run_lab.py --refresh             # discard the cache and refetch
```

Output lands in `out/`: `report.md`, `results.csv`, `equity_curves.png`.
Price data is cached to `data/cache/` on first run and costs nothing.

## How the backtest avoids lying to you

Most backtest results are wrong because of hidden bugs. Three are pinned down
here by tests in `tests/test_backtest.py`:

- **No lookahead.** A signal formed on the close of bar *t* fills at the **open
  of bar t+1**. Filling at the signal bar's own close means trading on prices
  that were not knowable when the decision was made, and it manufactures profit
  out of nothing. `test_future_bars_are_never_visible_to_the_strategy` asserts
  the strategy only ever sees bars up to the present.
- **Costs are real.** Indian delivery round trip is modelled at **0.32%**
  (0.22% statutory + 0.10% slippage). Zero-cost backtests are fiction; at this
  cost, a strategy trading often enough needs to clear 0.32% *every round trip*
  just to break even.
- **Stateful strategies restart per split.** Reusing one instance across the
  dev/test boundary leaks in-sample state into the unseen run.

## Cost model

Indian statutory charges, post-October-2024 STT regime, in `stratlab/costs.py`:
STT 0.1% each side, exchange transaction ~0.003%, SEBI ₹10/crore, IPFT
₹0.50/crore, stamp duty 0.015% buy-side, GST 18% on brokerage and exchange
charges. Binance spot: 0.1% per side.

**Verify every line against your own contract note before trusting any number
here.** The defaults are a starting point, not a substitute for the real thing.

## Layout

```
stratlab/
  costs.py        Indian and crypto transaction cost models
  indicators.py   causal SMA / Wilder RSI / ATR
  data.py         Yahoo Finance loading with a CSV cache
  backtest.py     event loop, next-bar fills, metrics
  report.py       markdown, CSV and chart output
  strategies/     buy-and-hold, SMA crossover, RSI, Donchian, buy-the-dip
run_lab.py        entry point
tests/            no-lookahead and cost tests
```

## Scope

- Symbols: NIFTY 50, RELIANCE, TCS, INFY, HDFCBANK, ITC, SBIN, BTC, ETH
- Long-only, all-in or flat, one instrument at a time
- Daily bars, next-open execution

## Next steps, in the order they should happen

1. **Fix the sample size, not the parameters.** Three trades per pair cannot
   validate anything. Widen the universe and lengthen the test window before
   tuning anything else — parameter tuning on 4 trades is guaranteed
   overfitting.
2. **Add cross-sectional breadth and a risk layer.** A single all-in instrument
   means the backtest mostly measures whether that stock went up.
3. **Re-run and re-read the verdict** before any further phase.

Phase 1 (paper-trading engine), Phase 2 (dashboard), Phase 3 (extended paper
running) and Phase 4 (live trading) are not started. Live trading additionally
requires settling the broker question: Zerodha Kite Connect charges ₹500/month
for market data and requires a static IP for order placement, which is awkward
on residential broadband. Angel One, Upstox, Fyers and Dhan offer free APIs.

## Disclaimer

This is research software for a personal project. It is not investment advice.
Past performance does not indicate future results, and nothing here should be
treated as evidence that any strategy is profitable.