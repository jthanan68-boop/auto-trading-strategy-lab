# Auto-Trading Strategy Lab

Phase 0 (strategy lab), Phase 1 (paper-trading engine) and Phase 2 (dashboard)
of a plan to build a monitored, risk-capped automated trading system for Indian
equities and crypto.

Two trading styles live in the same engine and never mix state: **daily**
(the original multi-week Donchian hold) and **intraday** (buy and exit within
the same session). The default is currently intraday, but that default is
**provisional** — it was pinned in advance of the empirical comparison that
`run_intraday_lab.py` exists to produce, and it only stands if that comparison
backs it.

**This repository places no orders and holds no broker credentials.** Phase 1 is
a simulator. There is no live broker adapter, no credential reader, and a test
(`test_no_live_credentials_are_readable_anywhere`) asserts those strings appear
nowhere in the source.

## Phase 0 result: no edge, and breadth confirmed it

Run against **43 instruments** from 2018-01-01 to 2026-10-14 (Tata Motors was
dropped: Yahoo returns an empty payload for every Tata Motors symbol it tries).
Out-of-sample (2024 onward), across 215 strategy/instrument pairs:

| | |
|---|---|
| Pairs beating buy-and-hold | 65 of 215 (**30%**) |
| **Median excess return** | **−12.36 points** |
| Average trades per pair | 3.7 |
| Wins while buy-and-hold was *falling* | 42 of 65 |

The first lab run on 9 instruments returned a median excess of **+0.44 points**
on 3.6 trades, which I reported as "no edge, indistinguishable from noise".
Widening the universe was the right move: that figure was a small-sample fluke.
On 43 instruments the median is **−12.36 points**, and 42 of the 65 wins are
just the mechanical effect of sitting in cash during a downtrend.

That is now a shortfall, not noise. Every strategy in this set loses to simply
holding the instrument it is trading, out-of-sample, with realistic costs.

The in-sample numbers are worse still: 41 of 210, median −77 points. Nothing
here was ever going to survive contact with unseen data.

Phase 1 was built anyway, on the understanding that **a multi-week live paper
run is better evidence than another backtest**. Infrastructure is ready; the
strategy question is now firmly negative rather than merely unanswered.

## Why they lost

`python diagnose.py` breaks the result down rather than restating it:

| | Median excess | Hit rate |
|---|---|---|
| Market **rose** (150 pairs) | **−25.64 points** | 15% |
| Market **fell** (60 pairs) | +8.38 points | 70% |

Time-in-market is *not* the explanation — `correlation(exposure, excess) = -0.00`.
The rule is not simply holding too much cash. It is systematically missing upside
while collecting downside, which is what a confirmation-based signal does.

160 of 210 pairs achieved a shallower drawdown than buy-and-hold, but still
returned a median −10.02 points. Reduced risk without a return for it.

## Cross-sectional rules also fail

The diagnosis pointed at a specific fix: stop trading one instrument at a time,
rank them against each other, stay invested. `run_xs.py` implements that as a
portfolio backtester with monthly rebalancing.

| Rule (top-10 of 42) | Out-of-sample return | vs equal-weight B&H |
|---|---|---|
| xs-mom 63/21 | +0.96% | −18.03 pts |
| xs-mom 252/21 | +7.84% | −11.15 pts |
| xs-lowvol 126 | −9.77% | −28.76 pts |
| xs-mom 126/21 | −33.80% | −52.79 pts |

**0 of 6 beat buy-and-hold.** Median excess −29.57 points.

### And it is not a cost problem

These rules traded ~430 times, at 0.322% a round trip, so friction was the
obvious suspect. It is not:

| | Return | vs B&H |
|---|---|---|
| equal-weight buy-and-hold | +18.99% | — |
| xs-mom 126/21, **real costs** | −33.92% | −52.91 pts |
| xs-mom 126/21, **zero costs** | −31.18% | −50.17 pts |

Removing every rupee of cost recovers **2.7 of 52 points**. The signal has no
value on its own; friction is a rounding error next to that.

Two strategy families, one ruled by "up when the graph rises", one from academic
cross-sectional momentum literature, both lose by a wide margin. That points away
from tuning and toward the question the project was really about.

## The original idea, and what happened to it

The brief was: *buy when the graph goes up, sell when it goes down.* That rule is
implemented here as `sma-20/100` so its fate is measurable rather than arguable.
It did not work. In a strong uptrend it sat in cash through the best moves — on
BTC out-of-sample it returned **-12.3%** while buy-and-hold returned **+87.6%**.
A confirmation-based signal fires *after* the move, so it systematically buys
late and sells early. See `stratlab/strategies/library.py`.

## Running the strategy lab

```bash
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python run_lab.py
.\.venv\Scripts\python -m pytest
```

Useful flags:

```bash
python run_lab.py --symbols BTC,TCS     # a subset
python run_lab.py --full                # all 44 instruments; far more trades
python run_lab.py --dev-end 2023-01-01  # move the dev/test boundary
python run_lab.py --refresh             # discard the cache and refetch
```

The default 9-instrument set is for a quick look. `--full` runs 44 NIFTY
constituents and indices — the Phase 0 verdict was that 9 instruments yielded
only 3.6 out-of-sample trades per pair, too few to judge anything, so breadth is
the fix rather than more strategy variants. Downloads are cached incrementally in
`data/cache/`, so an interrupted `--full` run resumes where it stopped.

Output lands in `out/`: `report.md`, `results.csv`, `equity_curves.png`.
Price data is cached to `data/cache/` on first run and costs nothing.

## Intraday mode (the current default, provisional)

The engine can also trade *inside* one session: buy after it is open, square
off before it closes, hold nothing overnight. Naive-UTC is the working timezone
and `stratlab/calendar.py` owns the session rules:

| Asset class | Session (UTC) | Bars |
|---|---|---|
| NSE equity | 03:45–10:00 | Yahoo 5m, publisher freshness varies; near-live on 26 Oct 2026 |
| Crypto | full day, every day | Yahoo/Binance 5m |

### The three presets

| Preset | Rule | Instance name |
|---|---|---|
| ORB | buy the break above the first 15 minutes' range, exit under its low | `orb-15min` |
| VWAP dip | buy ≤0.1% below the session VWAP, exit back at the line | `vwap-dip-0.1%` |
| Fast SMA | 24/72-bar cross, the "graph up, buy" rule at intraday speed | `sma-intraday-24/72` |

`buy-and-hold` runs alongside as the baseline, exempt from the evidence gate.

### What changes versus daily mode — and what does not

Only the stop and the freshness gate move. Every exposure and order cap is
carried through at the same configured value:

| Control | Daily | Intraday |
|---|---|---|
| Stop loss | 8% | **1%** |
| Stale bar | day-based calendar gate | warn 30 min, halt 120 min — but only while the asset class printed a bar today, so a holiday never false-halts |
| Position value, total exposure, daily loss, % of equity | unchanged | unchanged |
| Order caps 20/hour, 100/day | unchanged | unchanged |
| Round-trip cost (equity) | 0.322% (delivery STT both sides) | **0.1353%** (intraday STT sell-side only) |

### Session-end guarantees

- **Square-off** opens a fixed window (3 bars, ~15 min at 5m) before the close:
  open positions queue a `square-off` exit that fills at the next bar's open,
  inside the session. A window order bypasses order caps and a halt the way a
  stop loss does.
- **No new entries** in that window (reducing is always allowed).
- **Restart recovery**: a crash between queue and fill — or any cycle that lands
  outside the session with the book open — zeroes the position immediately at the
  newest known price, through the same accounting path as any other fill, so the
  ledger books the round trip. An empty cache, a stalled feed or a killed process
  can trap data, never the book.
- The daily-loss baseline still resets at UTC midnight.

### The honest caveats

NSE intraday bars arrive ~15 minutes late, so equity prices are a simulation
approximation, accepted deliberately. Signal on a completed bar fills at the
next bar's open, unchanged. Fresh intraday history is only ~59 days deep at
Yahoo, so any intraday comparison is a short-window result by construction.

### Comparing presets offline

```bash
.\.venv\Scripts\python run_intraday_lab.py            # 5 symbols, 5m, ~40 days
.\.venv\Scripts\python run_intraday_lab.py --interval 15m
```

Splits chronologically (~60/40 dev/test), runs each preset per symbol with the
per-asset-class intraday cost model, and ranks by test-split return. Anything
under 20 round trips in a split is marked **insufficient evidence** rather than
being treated as a result. The report ranks; it does not pick a winner.

### Comparing live paper runs

Run the engine once per strategy into separate databases, then:

```bash
.\.venv\Scripts\python run_bot.py --db data/run_orb.sqlite3
.\.venv\Scripts\python run_bot.py --db data/run_vwap.sqlite3 ... --strategy vwap-dip
.\.venv\Scripts\python run_compare.py data/run_orb.sqlite3 data/run_vwap.sqlite3
```

`run_compare.py` reads each ledger read-only and reports net return, drawdown
(calendar-day and worst intraday tick), trade count, win rate, fees and
resampled Sharpe/Sortino side by side. It also ranks rather than verdicts.

## The dashboard (Phase 2)

A React + TypeScript PWA with candlestick charts, equity curve, positions,
signals, fills and a risk log. Served by the same FastAPI process, so the UI
and API are same-origin and no host configuration is needed.

```bash
cd dashboard
npm install
npm run build          # outputs to dashboard/dist, which FastAPI serves
cd ..
.\.venv\Scripts\python run_bot.py --serve
```

Open **http://127.0.0.1:8000** — not `/docs`. FastAPI serves the dashboard at
the root and keeps the API at `/api/*`.

For live UI development with hot reload, run both:

```bash
# terminal 1
.\.venv\Scripts\python run_bot.py --serve

# terminal 2
cd dashboard && npm run dev     # http://localhost:5173, proxies /api to :8000
```

### Using it on your phone

```powershell
.\.venv\Scripts\python run_bot.py --serve --host 0.0.0.0
ipconfig | Select-String "IPv4"
```

Then on the phone, same WiFi: `http://<your-ip>:8000` → Chrome menu →
**Add to Home screen**. You get an icon and a fullscreen window.

Set `STRATBOT_CONTROL_TOKEN` before binding to `0.0.0.0`, otherwise the
emergency stop stays disabled. **Do not port-forward this to the internet** —
it has no TLS and your LAN is the only thing protecting the kill switch. For
access from outside home, use a VPN.

### Converting to an Android APK

The dashboard is built conversion-ready:

- API calls are relative `/api/*`, so no server host is baked into the bundle.
  `test_built_bundle_does_not_hardcode_a_server_host` enforces this.
- `dashboard/capacitor.config.ts` is committed with `CapacitorHttp` pre-enabled.

```bash
cd dashboard
npm install @capacitor/core @capacitor/cli @capacitor/android
npx cap add android
npx cap sync
npx cap open android      # then Build > Build APK, or: gradlew assembleDebug
```

Capacitor serves the webview from `https://localhost`, so plain `http://` calls
would normally be blocked as mixed content with no visible error.
`CapacitorHttp` routes requests natively and avoids that. TWA/Bubblewrap is not
suitable here because it requires HTTPS, and this server is HTTP on the LAN.

Building an APK needs the JDK and Android SDK installed (~8–10 GB). An installed
PWA gives most of the benefit for none of that.

## Running the paper engine (Phase 1)

```bash
# two cycles against the live free feed (style default is intraday)
.\.venv\Scripts\python run_bot.py --cycles 2 --sleep 5

# the historical daily profile: Donchian 55/20, 5-min poll, 60-min refresh
.\.venv\Scripts\python run_bot.py --cycles 2 --style daily

# start the API
.\.venv\Scripts\python -m pip install fastapi "uvicorn[standard]"
$env:STRATBOT_CONTROL_TOKEN = "pick-a-long-random-string"
.\.venv\Scripts\python run_bot.py --serve
```

Then `http://127.0.0.1:8000/docs` for the API, or:

| Endpoint | Purpose |
|---|---|
| `GET /api/status` | equity, exposure, halt state, risk limits |
| `GET /api/positions` | open positions and unrealised P/L |
| `GET /api/quotes` | live prices, with the source and the age of each |
| `GET /api/equity` | equity curve |
| `GET /api/signals` | every decision, with the reason for it |
| `GET /api/risk-events` | halts, stop losses, rejections |
| `GET /api/reconcile` | ledger vs broker, in sync or not |
| `POST /api/emergency-stop` | halt, cancel, flatten. **Requires the token** |
| `POST /api/resume` | clear a halt. **Requires the token** |

On a quiet day the engine may legitimately place no orders — Donchian only buys a
55-day high, and on some days nothing qualifies. `tests/test_integration.py`
drives synthetic bars to prove the order path works regardless.

## Realtime quotes

The completed-bar rule holds in both styles: a signal decided on a completed
bar fills at the next bar's open, whether that bar is a day or 5 minutes. What
is live is the *mark*: the price an open position is valued at, and the price
the dashboard shows.

| Asset class | Source | Latency |
|---|---|---|
| BTC, ETH | Binance spot ticker | live, no account and no key |
| NSE equities | Yahoo one-minute chart | measured 6–16 s behind on 26 Oct 2026; aged prints are refused and stops fall back to the bars |

There is no key-free live NSE source, so a delayed equity price is the honest
best available. Every quote carries the venue that produced it and its age, so
a stale price is visible rather than mistaken for a current one.

A quote too old to trust is refused and the daily bar takes over again. A
provider that fails or is slow leaves the bot trading on the evidence it always
had — the quote layer is an addition, never a dependency. `--no-realtime` turns
it off entirely and marks positions from the daily close as before.

## Risk controls

All of these are enforced in `stratbot/risk.py`, which can only *refuse* an
order, never amend one. Strategy code cannot reach a broker without passing
through it, and cannot raise a limit.

| Control | Behaviour |
|---|---|
| Max position value | Order shrunk to fit, or refused and halted |
| Max total exposure | Refused and halted |
| Max daily loss | New trading halts for the day |
| Stop loss | Forced exit at 8% daily / 1% intraday, bypassing position caps on purpose |
| Duplicate orders | Deterministic `client_order_id`; a repeat returns the original outcome, never a second fill |
| Reconciliation | Ledger vs broker compared every cycle; a mismatch halts rather than self-heals |
| Stale prices | Intraday: barring a holiday, a bar 2h old halts (30 min warns). Daily: day-based calendar gate |
| Square-off window | Intraday: entries refused, open book queued for exit ~15 min before close. Bypasses order caps |
| Order rate limits | 20/hour, 100/day (exits in the square-off window and stop losses bypass these on purpose) |
| Unknown order status | Halts the bot — an unresolved order is never retried blindly |
| Emergency stop | Halts, cancels, flattens, works with the API closed |

Defaults live in `stratbot/config.py`. To use an emergency stop, set
`STRATBOT_CONTROL_TOKEN`; without it the mutating routes return 503 and fail
closed.

## Cost model

Indian statutory charges, post-October-2024 STT regime, in `stratlab/costs.py`:
STT 0.1% each side, exchange transaction ~0.003%, SEBI ₹10/crore, IPFT
₹0.50/crore, stamp duty 0.015% buy-side, GST 18% on brokerage and exchange
charges. Binance spot: 0.1% per side.

Modelled round trip: **0.322%** for India delivery (0.222% statutory + 0.10%
slippage), **0.1353%** for India intraday (0.0353% statutory, STT sell-side
only, + 0.10% slippage), 0.30% for Binance spot (same in both styles).

**Verify every line against your own contract note before trusting any number
here.** The defaults are a starting point, not a substitute for the real thing.

## How the backtest avoids lying to you

Most backtest results are wrong because of hidden bugs. Three are pinned down
here by tests:

- **No lookahead.** A signal formed on the close of bar *t* fills at the **open
  of bar t+1**. Filling at the signal bar's own close means trading on prices
  that were not knowable when the decision was made, and it manufactures profit
  out of nothing. `test_future_bars_are_never_visible_to_the_strategy` asserts
  the strategy only ever sees bars up to the present.
- **Costs are real.** At 0.32% a round trip, a strategy trading often enough
  needs to clear that *every time* just to break even.
- **Stateful strategies restart per split.** Reusing one instance across the
  dev/test boundary leaks in-sample state into the unseen run.

## Layout

```
stratlab/            Phase 0: research
  costs.py           Indian and crypto transaction cost models
  indicators.py      causal SMA / Wilder RSI / ATR
  calendar.py        intraday sessions, bar grids, staleness primitives
  data.py            Yahoo Finance loading with a per-interval CSV cache
  backtest.py        event loop, next-bar fills, metrics
  portfolio.py       multi-instrument backtester with rebalancing
  report.py          markdown, CSV and chart output
  strategies/
    library.py       buy-and-hold, SMA crossover, RSI, Donchian, buy-the-dip
    intraday.py      ORB, VWAP dip, fast SMA - the intraday presets
    cross_sectional.py  momentum and low-volatility ranking rules

stratbot/            Phase 1: paper trading engine
  config.py          risk limits, trading styles, universe
  models.py          Bar, OrderRequest, Fill, Position, AccountState
  db.py              SQLite ledger: signals, orders, fills, equity, events
  risk.py            every hard cap, kill switch, bar-freshness gate
  reconciler.py      ledger vs broker truth
  engine.py          the loop: flatten -> square-off -> stops -> signals
  analysis.py        Phase 3: regimes, run summary, not-yet-conclusive gate
  live_gate.py       Phase 4: three-factor unlock that fails closed
  api.py             FastAPI surface, also serves the dashboard
  broker/
    contract.py      behavioural tests every adapter must pass
    paper.py         simulated fills (the only adapter that exists)
    feed.py          free delayed bar feed

dashboard/           Phase 2: PWA
  src/App.tsx        shell, stats, controls, tabs
  src/api.ts         origin-relative API client
  src/components/    equity chart, candlestick chart, tables, risk log
  capacitor.config.ts  conversion-ready APK config

run_lab.py           daily backtest lab
run_xs.py            cross-sectional portfolio backtest
diagnose.py          why the strategies lost, decomposed
run_intraday_lab.py  intraday preset comparison (evidence-gated ranking)
run_compare.py       side-by-side ledger comparison of paper runs
run_bot.py           paper engine and dashboard server
analyse_run.py       Phase 3 analysis
scripts/
  install_task.ps1   Windows scheduled task for continuous running
tests/               full regression suite (run `.\.venv\Scripts\python -m pytest -q`)
```

## Scope

- Symbols: NIFTY 50, RELIANCE, TCS, INFY, HDFCBANK, ITC, SBIN, BTC, ETH
- Long-only, all-in or flat, one instrument at a time
- Bars: daily or intraday 5m/… depending on style; next-open execution in both
- Paper only: `run_bot.py` boots the intraday profile (ORB 15min, 5m bars,
  BTC/ETH/RELIANCE/TCS/INFY) by default; `--style daily` restores Donchian
  55/20 on daily bars. See [Intraday mode](#intraday-mode-the-current-default-provisional).

## Next steps

1. **Let the comparison decide the default.** `run_intraday_lab.py` ranks the
   three presets with an evidence gate; the intraday default is provisional
   until that ranking exists and says where the edge is.
2. **Run the paper engine for weeks** (Phase 3) and check `analyse_run.py`
   weekly. This is the real test; the backtest was inconclusive. The tooling
   and the dashboard are ready to watch it happen.
3. **Widen the universe and lengthen the window** before tuning any parameter.
   Parameter tuning on 4 trades is guaranteed overfitting.
4. **Phase 4 stays gated** until the run beats buy-and-hold on live paper.

## Phase 4 readiness: built, live trading: not built

A live broker adapter is deliberately absent, and adding one would contradict the
project's own rule: a strategy must pass unseen data first. It has not.

What is built is everything that must exist and be proven *before* a live
adapter can be trusted.

### The broker contract

`stratbot/broker/contract.py` is a suite of behavioural tests every adapter must
pass. It currently runs against the paper simulator — and already caught a real
bug: an affordability clamp buffered cash by 0.1% while fees run 0.22% buy-side,
so an oversized buy left cash a few paise negative. That class of bug is exactly
what would cause an unfunded order at a broker.

Any future adapter inherits the same tests, so a live adapter that has not
demonstrated paper equivalence cannot reach the market.

### The live gate

`stratbot/live_gate.py` requires three independent factors before live trading
is possible:

- `STRATBOT_ALLOW_LIVE=yes-do-i-really-want-this` in the environment
- a marker file at `data/LIVE_UNLOCK`
- `--live` on the command line

No subset unlocks it. A test asserts that two of the three leave it locked, and
that a plain `true` or `1` does not count — only the exact phrase. The gate
records each state change.

### The gate to actually open it

- `analyse_run.py` reports `BEATING BUY-AND-HOLD` on live paper
- the broker question is settled (see below)
- limits are cut to a size whose total loss you would accept without thinking
- paper and live credentials stay in separate stores, never in this repo

### Broker considerations, when the time comes

Zerodha Kite Connect charges ₹500/month for market data and requires a static IP
for order placement, awkward on residential broadband. Angel One, Upstox, Fyers
and Dhan offer free APIs. For crypto, Binance is FIU-IND registered but requires
IP-restricted API keys.

## Phase 3: running the evidence

The strategy lab found no edge, so the only thing left that can settle the
question is a live paper run over weeks. That waiting cannot be shortened, but
the measurement tooling for it is built.

### Start it running

```powershell
.\scripts\install_task.ps1          # registers a Windows scheduled task
Start-ScheduledTask -TaskName StratBotPaper
```

The task starts at logon, restarts on failure, and keeps going when you close the
terminal. Output goes to `logs/engine.log` (rotating, 5 MB × 10).

Or just run it in the foreground:

```powershell
.\.venv\Scripts\python run_bot.py --forever --sleep 900
```

`--serve` can run alongside it on the same database, so the dashboard shows a
live bot.

### Check whether the run means anything

```powershell
.\.venv\Scripts\python analyse_run.py
```

This compares the live paper run against buy-and-hold over the *same* window,
reports which market regimes the run actually covered, and flags divergence
from the Phase 0 backtest. Example on a fresh run:

```
VERDICT: NOT YET CONCLUSIVE
  - only 0 days of history, need 14
  - only 0 closed trades, need 20
  - seen 1 market regime(s), need 3
```

The tool is built to refuse to call a short or quiet run a success. It reports
`BEATING BUY-AND-HOLD` only after 14+ days, 20+ closed trades, and 3+ distinct
market regimes.

### Market regime coverage

A run that only ever saw one trending market proves nothing. `analysis.classify`
labels each window as up/down/range crossed with high/low volatility, using
trendiness (net move ÷ total path length) rather than raw direction — a market
that travels far and ends where it started is a range, however violent the
daily bars look.

## Phase 4 is deliberately not built

There is no live broker adapter in this repository, and adding one would
contradict the project's own rule: a strategy must pass unseen data first. It
has not. The gate to open it:

- `analyse_run.py` reports `BEATING BUY-AND-HOLD` on live paper
- the broker question is settled (see below)
- limits are cut to a size whose total loss you would accept without thinking
- paper and live credentials stay in separate stores, never in this repo

Live trading additionally needs a broker decision: Zerodha Kite Connect charges
₹500/month for market data and requires a static IP for order placement, awkward
on residential broadband. Angel One, Upstox, Fyers and Dhan offer free APIs.
For crypto, Binance is FIU-IND registered but requires IP-restricted API keys.

An Android APK is a small optional follow-up; see
[Converting to an Android APK](#converting-to-an-android-apk).

## Disclaimer

This is research and simulation software for a personal project. It is not
investment advice and cannot place a real order. Past performance does not
indicate future results, and nothing here is evidence that any strategy is
profitable.