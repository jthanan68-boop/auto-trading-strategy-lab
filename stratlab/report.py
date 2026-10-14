"""Markdown, CSV and chart output for a strategy lab run."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

COLUMNS = [
    ("symbol", "Symbol"),
    ("strategy", "Strategy"),
    ("total_return_pct", "Return %"),
    ("cagr_pct", "CAGR %"),
    ("ann_vol_pct", "Vol %"),
    ("sharpe", "Sharpe"),
    ("sortino", "Sortino"),
    ("max_drawdown_pct", "Max DD %"),
    ("trades", "Trades"),
    ("win_rate_pct", "Win %"),
    ("profit_factor", "PF"),
    ("exposure_pct", "Exposure %"),
    ("end_equity", "End equity"),
]

DISCLAIMER = (
    "Backtest output is not a forecast. A strategy that looks good here has "
    "survived one historical path, not proven an edge in the market. Treat a "
    "strategy that fails the out-of-sample test as refuted, not as a project "
    "waiting for better parameters."
)


def to_frame(rows: list[dict]) -> pd.DataFrame:
    keep = [key for key, _ in COLUMNS if key in rows[0]] if rows else []
    return pd.DataFrame(rows)[keep] if keep else pd.DataFrame(rows)


def markdown_table(rows: list[dict]) -> str:
    if not rows:
        return "_no results_"
    frame = to_frame(rows)
    header = "| " + " | ".join(label for _, label in COLUMNS if _ in frame.columns) + " |"
    rule = "|" + "|".join("---" for _ in frame.columns) + "|"
    lines = [header, rule]
    for _, row in frame.iterrows():
        cells = []
        for key, _ in COLUMNS:
            if key in frame.columns:
                value = row[key]
                cells.append("inf" if value == float("inf") else f"{value}")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def verdict(metric_row: dict, benchmark_return: float) -> str:
    """Plain-language read of one strategy against buy-and-hold."""
    strategy_return = metric_row.get("total_return_pct", 0.0)
    drawdown = abs(metric_row.get("max_drawdown_pct", 0.0))
    trades = metric_row.get("trades", 0)

    if trades == 0:
        return "no trades - nothing to judge"
    if strategy_return > benchmark_return:
        if drawdown < abs(benchmark_return) * 0.75 or benchmark_return < 0:
            return f"beat buy-and-hold by {strategy_return - benchmark_return:.1f} pts"
        return f"beat buy-and-hold by {strategy_return - benchmark_return:.1f} pts, but with more risk"
    gap = benchmark_return - strategy_return
    if gap < 5:
        return f"roughly matched buy-and-hold, trailing by {gap:.1f} pts"
    return f"lost to buy-and-hold by {gap:.1f} pts - refuted"


def _aggregate(rows: list[dict], period: str) -> dict | None:
    """How often did a strategy actually beat buy-and-hold, on the unseen data?"""
    subset = [r for r in rows if r.get("period") == period]
    pairs: list[dict] = []
    for symbol in {r["symbol"] for r in subset}:
        group = [r for r in subset if r["symbol"] == symbol]
        benchmark = next((r for r in group if r["strategy"].startswith("buy-and-hold")), None)
        if not benchmark:
            continue
        for row in group:
            if row["strategy"].startswith("buy-and-hold"):
                continue
            pairs.append(
                {
                    "symbol": symbol,
                    "strategy": row["strategy"].replace(f" [{period}]", ""),
                    "excess": row["total_return_pct"] - benchmark["total_return_pct"],
                    "trades": row["trades"],
                    "drawdown": abs(row["max_drawdown_pct"]),
                    "benchmark_return": benchmark["total_return_pct"],
                }
            )
    if not pairs:
        return None

    excess = [p["excess"] for p in pairs]
    beaten = [p for p in pairs if p["excess"] > 0]
    # A win while the benchmark was falling is just reduced exposure, not skill.
    by_paperwork = [p for p in beaten if p["benchmark_return"] < 0]
    return {
        "pairs": len(pairs),
        "beaten": len(beaten),
        "median_excess": float(sorted(excess)[len(excess) // 2]),
        "mean_trades": sum(p["trades"] for p in pairs) / len(pairs),
        "while_benchmark_fell": len(by_paperwork),
        "beaten_in_uptrend": len(beaten) - len(by_paperwork),
        "best": max(pairs, key=lambda p: p["excess"]),
        "worst": min(pairs, key=lambda p: p["excess"]),
    }


def write_report(
    path: Path,
    universe_rows: list[dict],
    symbols: list[str],
    dev_end: str,
    round_trip_notes: list[str],
) -> None:
    lines = [
        "# Strategy lab report",
        "",
        f"Development period ends: **{dev_end}**. Rows marked `[test]` are out-of-sample.",
        "",
        "## Verdict",
        "",
    ]

    test_summary = _aggregate(universe_rows, "test")
    dev_summary = _aggregate(universe_rows, "dev")

    if test_summary:
        share = test_summary["beaten"] / test_summary["pairs"]
        lines += [
            f"On unseen data, **{test_summary['beaten']} of {test_summary['pairs']}** "
            f"strategy/instrument pairs beat buy-and-hold ({share:.0%}).",
            "",
            f"- Median excess return vs buy-and-hold: **{test_summary['median_excess']:+.2f} points**",
            f"- Average trades per pair out-of-sample: **{test_summary['mean_trades']:.1f}**",
            f"- Of the wins, {test_summary['while_benchmark_fell']} happened while "
            f"buy-and-hold was losing money, and only {test_summary['beaten_in_uptrend']} "
            f"in a rising market",
            "",
        ]
        if test_summary["median_excess"] < 5 or test_summary["mean_trades"] < 10:
            lines += [
                "> **No strategy has demonstrated an edge.** "
                f"Median excess return is {test_summary['median_excess']:+.2f} points on "
                f"{test_summary['mean_trades']:.0f} trades per pair. "
                + (
                    "That is a shortfall, not an edge: the strategies lose to simply "
                    "holding the instrument."
                    if test_summary["median_excess"] < 0
                    else "That is indistinguishable from noise."
                )
                + " Most apparent wins are the mechanical effect of holding cash during a "
                "downtrend, which is not the same as finding an edge. Per the project's own "
                "rule, this does not advance to live trading.",
                "",
            ]
        if dev_summary:
            best_dev = max(dev_summary["beaten"], 0)
            lines += [
                f"For contrast, the same strategies scored {dev_summary['beaten']} of "
                f"{dev_summary['pairs']} in-sample. A large in-sample/out-of-sample gap is the "
                "signature of overfitting, not of a good strategy.",
                "",
            ]

    lines += [
        "## Assumed round-trip cost per trade",
        "",
        *[f"- {note}" for note in round_trip_notes],
        "",
        "## Results",
        "",
    ]

    for symbol in symbols:
        rows = [r for r in universe_rows if r.get("symbol") == symbol]
        if not rows:
            continue
        benchmark = next(
            (r for r in rows if r.get("strategy") == "buy-and-hold"), None
        )
        lines.append(f"### {symbol}")
        lines.append("")
        lines.append(markdown_table(rows))
        lines.append("")
        if benchmark:
            for row in rows:
                if row.get("strategy") == "buy-and-hold":
                    continue
                lines.append(f"- **{row['strategy']}**: {verdict(row, benchmark['total_return_pct'])}")
            lines.append("")

    lines += ["---", "", f"_{DISCLAIMER}_", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def plot_equity_curves(path: Path, curves: dict[str, pd.Series], title: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axis = plt.subplots(figsize=(11, 5.5))
    for label, series in curves.items():
        if series is not None and len(series) > 1:
            normalised = series / series.iloc[0] * 100.0
            axis.plot(normalised.index, normalised, label=label, linewidth=1.4)

    axis.set_title(title)
    axis.set_ylabel("Growth of 100")
    axis.grid(alpha=0.25, linewidth=0.6)
    axis.legend(frameon=False, fontsize=8)
    figure.autofmt_xdate()
    figure.tight_layout()
    figure.savefig(path, dpi=130)
    plt.close(figure)