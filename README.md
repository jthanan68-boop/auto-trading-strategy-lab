# Auto-Trading Strategy Lab

Phase 0 (strategy lab), Phase 1 (paper-trading engine) and Phase 2 (dashboard)
of a plan to build a monitored, risk-capped automated trading system for Indian
equities and crypto.

**This repository places no orders and holds no broker credentials.** Phase 1 is
a simulator. There is no live broker adapter, no credential reader, and a test
(`test_no_live_credentials_are_readable_anywhere`) asserts those strings appear
nowhere in the source.

## Phase 0 result: no edge found

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
overfitting, and it is why no strategy has been promoted to live trading.

Phase 1 was built anyway, on the understanding that **a multi-week live paper
run is better evidence than another backtest**. Infrastructure is ready; the
strategy question is still open.

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
# two cycles against the live free feed
.\.venv\Scripts\python run_bot.py --cycles 2 --sleep 5

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
| `GET /api/equity` | equity curve |
| `GET /api/signals` | every decision, with the reason for it |
| `GET /api/risk-events` | halts, stop losses, rejections |
| `GET /api/reconcile` | ledger vs broker, in sync or not |
| `POST /api/emergency-stop` | halt, cancel, flatten. **Requires the token** |
| `POST /api/resume` | clear a halt. **Requires the token** |

On a quiet day the engine may legitimately place no orders — Donchian only buys a
55-day high, and on some days nothing qualifies. `tests/test_integration.py`
drives synthetic bars to prove the order path works regardless.

## Risk controls

All of these are enforced in `stratbot/risk.py`, which can only *refuse* an
order, never amend one. Strategy code cannot reach a broker without passing
through it, and cannot raise a limit.

| Control | Behaviour |
|---|---|
| Max position value | Order shrunk to fit, or refused and halted |
| Max total exposure | Refused and halted |
| Max daily loss | New trading halts for the day |
| Stop loss | Forced exit at 8%, bypassing position caps on purpose |
| Duplicate orders | Deterministic `client_order_id`; a repeat returns the original outcome, never a second fill |
| Reconciliation | Ledger vs broker compared every cycle; a mismatch halts rather than self-heals |
| Stale prices | Bar older than 2h halts the bot |
| Order rate limits | 20/hour, 100/day |
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
slippage), 0.30% for Binance spot.

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
  data.py            Yahoo Finance loading with a CSV cache
  backtest.py        event loop, next-bar fills, metrics
  report.py          markdown, CSV and chart output
  strategies/        buy-and-hold, SMA crossover, RSI, Donchian, buy-the-dip

stratbot/            Phase 1: paper trading engine
  config.py          risk limits and universe
  models.py          Bar, OrderRequest, Fill, Position, AccountState
  db.py              SQLite ledger: signals, orders, fills, equity, events
  risk.py            every hard cap, kill switch
  reconciler.py      ledger vs broker truth
  engine.py          the loop
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

run_lab.py           strategy lab entry point
run_bot.py           paper engine and dashboard server
analyse_run.py       Phase 3 analysis
scripts/
  install_task.ps1   Windows scheduled task for continuous running
tests/               115 tests
```

## Scope

- Symbols: NIFTY 50, RELIANCE, TCS, INFY, HDFCBANK, ITC, SBIN, BTC, ETH
- Long-only, all-in or flat, one instrument at a time
- Daily bars, next-open execution
- Phase 1 currently trades BTC, ETH, RELIANCE, TCS, INFY with Donchian 55/20

## Next steps

1. **Run the paper engine for weeks** (Phase 3) and check `analyse_run.py`
   weekly. This is the real test; the backtest was inconclusive. The tooling
   and the dashboard are ready to watch it happen.
2. **Widen the universe and lengthen the window** before tuning any parameter.
   Parameter tuning on 4 trades is guaranteed overfitting.
3. **Phase 4 stays gated** until the run beats buy-and-hold on live paper.

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