"""Trace one synthetic Donchian breakout: bars -> entry fill -> exit."""

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent / "tests"))

import numpy as np
import pandas as pd

import stratbot.engine as engine_module
from stratbot.broker.paper import PaperBroker
from stratbot.config import BotConfig, RiskLimits
from stratbot.db import Database
from stratbot.models import Bar, utcnow
from stratlab.costs import cost_model_for
from stratlab.strategies.library import DonchianBreakout

COST = cost_model_for("equity")


def frame(closes):
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="B")
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1.0},
        index=idx,
    )


base = list(np.linspace(200.0, 100.0, 80))
entry_level = max(base[-56:-1]) * 1.02
entry_hist = frame(base + [entry_level])
# Gain, then a decline that breaks the 20-day low but stays inside the 8% stop,
# so the Donchian exit is what fires.
_gain = list(np.linspace(entry_level * 1.01, entry_level * 1.30, 21))
_peak_series = base + [entry_level] + _gain
_exit_px = min(_peak_series[-20:]) * 0.995
exit_hist = frame(_peak_series + [_exit_px])


def bar_of(hist):
    c = float(hist["close"].iloc[-1])
    return Bar(symbol="TEST", ts=utcnow(), open=c, high=c, low=c, close=c)


def prior_high(h, n=55):
    return max(h["close"].iloc[-(n + 1):-1])


def prior_low(h, n=20):
    return min(h["close"].iloc[-(n + 1):-1])


print("=" * 74)
print("SYNTHETIC DONCHIAN 55/20 LIFECYCLE TRACE")
print("=" * 74)
print(f"\nPhase 1 - ENTRY ({len(entry_hist)} bars, last {entry_hist.index[-1].date()})")
print(f"  last close          {entry_hist['close'].iloc[-1]:10.2f}")
print(f"  prior 55-bar high   {prior_high(entry_hist):10.2f}")
print(f"  entry condition     {entry_hist['close'].iloc[-1]:.2f} >= {prior_high(entry_hist):.2f}"
      f"  -> {entry_hist['close'].iloc[-1] >= prior_high(entry_hist)}")
print(f"  position()          {DonchianBreakout(55, 20).position(entry_hist)}")

tmp = pathlib.Path(tempfile.mkdtemp())
db = Database(tmp / "trace.sqlite3")
config = BotConfig(symbols=("TEST",), strategy_name="donchian-55/20",
                   initial_cash=500_000.0,
                   limits=RiskLimits(max_position_value=250_000.0, max_total_exposure=300_000.0))
eng = engine_module.Engine(config, db, broker=PaperBroker(config.initial_cash))
current = {"h": entry_hist}
eng_mod = engine_module
original = eng_mod.current_bars
eng_mod.current_bars = lambda symbols: {"TEST": bar_of(current["h"])}
eng._history = lambda symbol: current["h"]

print("\nPhase 2 - ENGINE CYCLE 1 (entry)")
res = eng.cycle()
for d in res.decisions:
    print(f"  {d}")
f = db.recent_fills(1)[0]
ref = entry_hist["close"].iloc[-1]
print(f"\n  reference price      {ref:10.2f}")
print(f"  fill price           {float(f['price']):10.2f}   "
      f"(= ref x 1.0005, one 5bps step)")
print(f"  allowed/filled qty   {float(f['qty']):10.6f}")
print(f"  notional             {float(f['qty']) * float(f['price']):10.2f}")
print(f"  fees                 {float(f['fees']):10.2f}")
print(f"  cash after           {eng.broker.cash:10.2f}")
pos = eng.broker.positions["TEST"]
print(f"  position             qty {pos.qty:.6f} @ avg {pos.avg_price:.2f}")
row = db.equity_history()[-1]
print(f"  equity snapshot      {float(row['equity']):10.2f}  "
      f"unrealised {float(row['unrealised']):.2f}  exposure {float(row['exposure']):.2f}")

print(f"\nPhase 3 - ENGINE CYCLE 2 (exit), price falls to {exit_hist['close'].iloc[-1]:.2f}")
current["h"] = exit_hist
print(f"  prior 20-bar low     {prior_low(exit_hist):10.2f}")
print(f"  exit condition       {exit_hist['close'].iloc[-1]:.2f} <= {prior_low(exit_hist):.2f}"
      f"  -> {exit_hist['close'].iloc[-1] <= prior_low(exit_hist)}")
res = eng.cycle()
for d in res.decisions:
    print(f"  {d}")
fills = db.recent_fills(10)
s = [x for x in fills if x["side"] == "SELL"][0]
print(f"\n  sell fill            {float(s['qty']):.6f} @ {float(s['price']):.2f}  "
      f"fees {float(s['fees']):.2f}")
print(f"  position after       qty {eng.broker.positions['TEST'].qty:.6f}  (flat)")
print(f"  cash after           {eng.broker.cash:10.2f}")
acct = eng.broker.get_account()
print(f"  exposure             {acct.exposure:10.2f}")
print(f"  equity               {acct.equity:10.2f}   vs 500,000 opening")
print(f"  net change           {acct.equity - 500_000:+10.2f}")
total_fees = sum(float(x["fees"]) for x in fills)
print(f"  fees total           {total_fees:10.2f}")
print(f"  gross ex fees        {(acct.equity - 500_000) + total_fees:+10.2f}")

eng_mod.current_bars = original
print("\n" + "=" * 74)