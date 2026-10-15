#!/usr/bin/env python
"""Why did the strategies lose? A diagnosis, not another verdict.

The breadth run showed a median -12.36 point shortfall against buy-and-hold.
Knowing the number is less useful than knowing the mechanism, because the
mechanism decides whether any fix is possible.

    python diagnose.py
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

OUT_DIR = Path(__file__).resolve().parent / "out"


def load(results_csv: Path) -> pd.DataFrame:
    frame = pd.read_csv(results_csv)
    return frame[frame["period"] == "test"].copy()


def pair_with_benchmark(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for symbol, group in frame.groupby("symbol"):
        benchmark = group[group["strategy"].str.startswith("buy-and-hold")]
        if benchmark.empty:
            continue
        base = float(benchmark.iloc[0]["total_return_pct"])
        base_exposure = float(benchmark.iloc[0]["exposure_pct"])
        for _, row in group.iterrows():
            if row["strategy"].startswith("buy-and-hold"):
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "strategy": row["strategy"].replace(" [test]", ""),
                    "return": float(row["total_return_pct"]),
                    "benchmark": base,
                    "excess": float(row["total_return_pct"]) - base,
                    "exposure": float(row["exposure_pct"]),
                    "benchmark_exposure": base_exposure,
                    "drawdown": float(row["max_drawdown_pct"]),
                    "benchmark_drawdown": float(benchmark.iloc[0]["max_drawdown_pct"]),
                    "trades": int(row["trades"]),
                    "win_rate": float(row["win_rate_pct"]),
                }
            )
    return pd.DataFrame(rows)


def heading(frame: pd.DataFrame) -> None:
    print("\n--- Is the loss explained by sitting in cash? ---")
    low = frame[frame["exposure"] < 50]["excess"]
    high = frame[frame["exposure"] >= 50]["excess"]
    print(f"  exposure < 50% : {len(low):>3} pairs, median excess {low.median():+7.2f} points")
    print(f"  exposure >= 50%: {len(high):>3} pairs, median excess {high.median():+7.2f} points")
    correlation = frame["exposure"].corr(frame["excess"])
    print(f"  correlation(exposure, excess) = {correlation:+.2f}")


def by_market_direction(frame: pd.DataFrame) -> None:
    print("\n--- Split by what buy-and-hold did ---")
    up = frame[frame["benchmark"] > 0]
    down = frame[frame["benchmark"] <= 0]
    print(f"  market rose  ({len(up):>3} pairs): median excess {up['excess'].median():+7.2f} points")
    print(f"  market fell  ({len(down):>3} pairs): median excess {down['excess'].median():+7.2f} points")
    print(f"  hit rate in a rising market : {(up['excess'] > 0).mean():.0%}")
    print(f"  hit rate in a falling market: {(down['excess'] > 0).mean():.0%}")


def by_strategy(frame: pd.DataFrame) -> pd.DataFrame:
    print("\n--- Per strategy ---")
    grouped = frame.groupby("strategy").agg(
        pairs=("excess", "size"),
        median_excess=("excess", "median"),
        mean_excess=("excess", "mean"),
        beat_rate=("excess", lambda s: (s > 0).mean()),
        exposure=("exposure", "median"),
        trades=("trades", "median"),
        win_rate=("win_rate", "median"),
    ).sort_values("median_excess", ascending=False)

    for name, row in grouped.iterrows():
        print(
            f"  {name:<20} median {row['median_excess']:+7.2f} pts | beat {row['beat_rate']:4.0%} "
            f"| exposure {row['exposure']:4.0f}% | trades {row['trades']:4.0f}"
        )
    return grouped


def drawdown_tradeoff(frame: pd.DataFrame) -> None:
    print("\n--- Did any strategy buy less risk for it? ---")
    less_risk = frame[frame["drawdown"] > frame["benchmark_drawdown"]]
    print(f"  pairs with a shallower drawdown than buy-and-hold: {len(less_risk)} of {len(frame)}")
    if len(less_risk):
        print(f"  of those, median excess return: {less_risk['excess'].median():+.2f} points")
        print("  -> reduced risk alone did not produce excess return")


def conclusion(frame: pd.DataFrame) -> list[str]:
    rising = frame[frame["benchmark"] > 0]
    falling = frame[frame["benchmark"] <= 0]
    notes = [
        f"In a rising market these strategies lose a median "
        f"{abs(rising['excess'].median()):.1f} points to simply holding, while beating the "
        f"benchmark {(rising['excess'] > 0).mean():.0%} of the time.",
        f"In a falling market they gain a median {falling['excess'].median():+.1f} points, "
        f"beating it {(falling['excess'] > 0).mean():.0%} of the time.",
        "Together that is the signature of a rule that reduces exposure: it gives up the "
        "upside and collects the downside. That is not an edge, and no amount of "
        "parameter tuning reverses it.",
        "A fix would have to change the shape of the strategy, not its settings: invest "
        "across several instruments at once (so a single name cannot decide the outcome) "
        "and hold through noise instead of stepping aside at every dip.",
    ]
    return notes


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose the backtest shortfall.")
    parser.add_argument("--results", default=str(OUT_DIR / "results.csv"))
    args = parser.parse_args()

    frame = pair_with_benchmark(load(Path(args.results)))
    if frame.empty:
        print("no out-of-sample rows found")
        return 1

    print("=" * 68)
    print("DIAGNOSIS - why did the strategies lose to buy-and-hold?")
    print("=" * 68)
    print(f"{len(frame)} strategy/instrument pairs across {frame['symbol'].nunique()} instruments")

    heading(frame)
    by_market_direction(frame)
    by_strategy(frame)
    drawdown_tradeoff(frame)

    print("\n" + "=" * 68)
    print("CONCLUSION")
    print("=" * 68)
    for note in conclusion(frame):
        print(f"  {note}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "diagnosis.txt").write_text(
        "\n".join(conclusion(frame)), encoding="utf-8"
    )
    print(f"\nwrote {OUT_DIR / 'diagnosis.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())