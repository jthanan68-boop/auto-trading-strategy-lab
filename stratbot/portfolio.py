"""Portfolio-level risk management for stock management.

Replaces per-symbol directional strategies with a cross-sectional, risk-managed
portfolio:

- Rank symbols by momentum, hold top N
- Size each by vol target / ATR (volatility-adjusted, equal risk per position)
- Cap per-sector exposure so one macro move cannot draw down the book
- Trailing stops + take-profit on open positions
- Max drawdown circuit breaker halts the book
- Fixed rebalance cadence (session close / weekly / monthly) instead of polling
  every cycle

This module knows nothing about the engine loop: it computes target weights and
exit requests. The engine applies them through its existing order pipeline, so
all risk caps (position value, total exposure, daily loss, order rates) still
bind.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from stratbot.models import OrderRequest, Side, utcnow


# Symbol -> sector mapping for the current NSE + crypto universe. Symbols not
# listed here fall back to the bucket "other" and are still capped individually
# by max_position_pct.
SECTOR_MAP: dict[str, str] = {
    # Banks
    "HDFCBANK": "banks", "ICICIBANK": "banks", "KOTAKBANK": "banks",
    "AXISBANK": "banks", "SBIN": "banks", "INDUSINDBK": "banks",
    # IT
    "TCS": "it", "INFY": "it", "HCLTECH": "it", "TECHM": "it",
    # Energy
    "RELIANCE": "energy", "NTPC": "energy", "POWERGRID": "energy",
    # Auto
    "MARUTI": "auto", "HEROMOTOCO": "auto", "EICHERMOT": "auto",
    # Pharma
    "SUNPHARMA": "pharma", "DRREDDY": "pharma", "CIPLA": "pharma",
    "DIVISLAB": "pharma", "APOLLOHOSP": "pharma",
    # FMCG
    "HINDUNILVR": "fmcg", "ITC": "fmcg", "BRITANNIA": "fmcg",
    "TATACONSUM": "fmcg", "ASIANPAINT": "fmcg", "GRASIM": "fmcg",
    # Metals
    "HINDALCO": "metals", "JSWSTEEL": "metals", "TATASTEEL": "metals",
    # Construction
    "LT": "construction", "ULTRACEMCO": "construction",
    # Finance
    "BAJFINANCE": "finance", "BAJAJFINSV": "finance", "SBILIFE": "finance",
    # Conglomerate / infrastructure
    "ADANIENT": "conglomerate", "ADANIPORTS": "infrastructure",
    # Telecom
    "BHARTIARTL": "telecom",
    # Consumer discretionary
    "TITAN": "consumer",
    # Crypto
    "BTC": "crypto", "ETH": "crypto", "SOL": "crypto", "XRP": "crypto",
    "ADA": "crypto", "DOGE": "crypto", "AVAX": "crypto", "LINK": "crypto",
    "DOT": "crypto", "LTC": "crypto", "BCH": "crypto", "TRX": "crypto",
    # Commodity futures
    "CL": "oil", "BRENT": "oil", "NATGAS": "oil",
    "GOLD": "precious_metals", "SILVER": "precious_metals",
    "PLATINUM": "precious_metals", "PALLADIUM": "precious_metals",
    "COPPER": "industrial_metals",
    # FX
    "EURUSD": "fx", "GBPUSD": "fx", "USDJPY": "fx", "AUDUSD": "fx",
    "USDCAD": "fx", "USDCHF": "fx", "NZDUSD": "fx", "EURGBP": "fx",
    "EURJPY": "fx", "GBPJPY": "fx", "EURTRY": "fx", "USDTRY": "fx",
    "USDMXN": "fx", "USDZAR": "fx", "USDSGD": "fx", "USDHKD": "fx",
}


@dataclass
class PortfolioConfig:
    enabled: bool = False
    # Momentum: rank over `lookback` bars, skip the most recent `skip` bars to
    # avoid the one-month reversal that flips short-horizon momentum sign.
    lookback: int = 126
    skip: int = 21
    top_n: int = 10
    # Volatility sizing: annualised vol target per position, ATR period for the
    # volatility estimate. Weight is capped at max_position_pct.
    vol_target: float = 0.15
    atr_period: int = 14
    max_position_pct: float = 0.05
    max_sector_pct: float = 0.25
    # Rebalance cadence: "session_close" (once per day), "weekly", "monthly".
    rebalance_cadence: str = "session_close"
    # Trade management on open positions.
    trailing_stop_pct: float = 0.08
    take_profit_pct: float = 0.15
    # Circuit breaker: halt when equity falls this far from peak.
    mdd_circuit_breaker_pct: float = 0.10
    min_holding_days: int = 5


class PortfolioRiskManager:
    """Cross-sectional portfolio manager: momentum rank, vol size, sector cap.

    Holds mutable state (peak equity, high-water marks, entry prices, last
    rebalance time) across cycles. Instantiated once by the engine.
    """

    def __init__(
        self,
        config: PortfolioConfig,
        db,
        sector_map: dict[str, str] | None = None,
    ):
        self.config = config
        self.db = db
        self.sector_map = sector_map or SECTOR_MAP
        self._peak_equity: float | None = None
        self._high_water: dict[str, float] = {}
        self._entry_price: dict[str, float] = {}
        self._last_rebalance: datetime | None = None
        self._last_targets: dict[str, float] = {}
        self._last_scores: list[tuple[str, float]] = []

    # ---- equity peak / MDD ----------------------------------------------
    def _peak(self, equity: float) -> float:
        if self._peak_equity is None or equity > self._peak_equity:
            self._peak_equity = equity
        return self._peak_equity

    def check_mdd(self, equity: float) -> bool:
        """True if equity has fallen mdd_pct from peak: halt the book."""
        peak = self._peak(equity)
        if peak <= 0:
            return False
        return (peak - equity) / peak >= self.config.mdd_circuit_breaker_pct

    # ---- rebalance cadence ---------------------------------------------
    def should_rebalance(self, now: datetime) -> bool:
        if self._last_rebalance is None:
            return True
        cadence = self.config.rebalance_cadence
        if cadence == "session_close":
            return now.date() > self._last_rebalance.date()
        if cadence == "weekly":
            return (now - self._last_rebalance) >= timedelta(weeks=1)
        if cadence == "monthly":
            return (now - self._last_rebalance) >= timedelta(days=30)
        return False

    # ---- volatility sizing ----------------------------------------------
    @staticmethod
    def _atr(history: pd.DataFrame, period: int = 14) -> float | None:
        if len(history) < period + 1:
            return None
        high = history["high"]
        low = history["low"]
        close = history["close"]
        prev_close = close.shift(1)
        tr = np.maximum(
            high - low,
            np.maximum((high - prev_close).abs(), (low - prev_close).abs()),
        )
        atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()
        val = atr.iloc[-1]
        return float(val) if pd.notna(val) else None

    def _vol_weight(self, symbol: str, history: pd.DataFrame, price: float) -> float:
        """Weight sized so each position carries equal risk (vol_target annualised).

        weight = vol_target / (atr_pct * sqrt(252)), capped at max_position_pct.
        """
        atr = self._atr(history, self.config.atr_period)
        if atr is None or atr <= 0 or price <= 0:
            return 0.0
        atr_pct = atr / price
        annual_vol = atr_pct * np.sqrt(252)
        if annual_vol <= 0:
            return 0.0
        weight = self.config.vol_target / annual_vol
        return min(max(weight, 0.0), self.config.max_position_pct)

    # ---- momentum ranking ----------------------------------------------
    @staticmethod
    def _momentum(close: pd.Series, lookback: int, skip: int) -> float | None:
        if len(close) < lookback + skip + 1:
            return None
        start = close.iloc[-(lookback + skip + 1)]
        end = close.iloc[-(skip + 1)]
        if start <= 0:
            return None
        return float(end / start - 1.0)

    # ---- main target computation ---------------------------------------
    def compute_targets(
        self,
        histories: dict[str, pd.DataFrame],
        account,
        prices: dict[str, float],
        now: datetime,
    ) -> dict[str, float]:
        """Target weight per symbol: momentum-ranked, vol-sized, sector-capped.

        Returns {} when portfolio mode is disabled or no rebalance is due.
        When no rebalance is due, returns current position weights (hold).
        """
        if not self.config.enabled:
            return {}

        if not self.should_rebalance(now):
            return self._current_weights(account, prices)

        # 1. Momentum rank all symbols with sufficient history.
        scored: list[tuple[str, float]] = []
        for symbol, history in histories.items():
            if len(history) == 0:
                continue
            score = self._momentum(history["close"], self.config.lookback, self.config.skip)
            if score is not None:
                scored.append((symbol, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        chosen = scored[: self.config.top_n]

        if not chosen:
            return {}

        # 2. Vol-size each chosen symbol.
        raw_weights: dict[str, float] = {}
        for symbol, _ in chosen:
            history = histories[symbol]
            price = prices.get(symbol, 0.0)
            w = self._vol_weight(symbol, history, price)
            if w > 0:
                raw_weights[symbol] = w

        if not raw_weights:
            return {}

        # 3. Normalise to gross = 1.0, then apply sector caps.
        total = sum(raw_weights.values())
        if total <= 0:
            return {}
        weights = {s: w / total for s, w in raw_weights.items()}
        weights = self._apply_sector_caps(weights)

        self._last_rebalance = now
        self._last_targets = dict(weights)
        self._last_scores = scored
        return weights

    def _current_weights(self, account, prices: dict[str, float]) -> dict[str, float]:
        """Current position market-value weights (hold, no rebalance due)."""
        equity = account.equity if account.equity > 0 else 1.0
        weights: dict[str, float] = {}
        for symbol, position in account.positions.items():
            if position.is_open() and symbol in prices:
                weights[symbol] = position.market_value / equity
        return weights

    def _apply_sector_caps(self, weights: dict[str, float]) -> dict[str, float]:
        """Cap each sector, then push the freed weight into sectors with room.

        Clamping alone leaves the book short of its gross target: a sector cut
        from 0.60 to 0.25 strands 0.35 of capital even when other sectors have
        headroom. Each pass therefore grants the shortfall pro rata to the room
        still left under each cap, and the grant is bounded by that room, so a
        sector can never be pushed back over. A book that is entirely one sector
        cannot reach the target and stays where the cap puts it.
        """
        cap = self.config.max_sector_pct
        target = sum(weights.values())
        if target <= 0:
            return {}

        def groups() -> dict[str, list[str]]:
            out: dict[str, list[str]] = {}
            for symbol in scaled:
                out.setdefault(self.sector_map.get(symbol, "other"), []).append(symbol)
            return out

        scaled = dict(weights)
        for members in groups().values():
            sector_total = sum(scaled[m] for m in members)
            if sector_total > cap:
                factor = cap / sector_total
                for symbol in members:
                    scaled[symbol] *= factor

        for _ in range(8):
            shortfall = target - sum(scaled.values())
            if shortfall <= 1e-12:
                break
            room = {
                sector: cap - sum(scaled[m] for m in members)
                for sector, members in groups().items()
            }
            open_room = {s: r for s, r in room.items() if r > 1e-12}
            if not open_room:
                break
            total_room = sum(open_room.values())
            pool = min(shortfall, total_room)
            for sector, members in groups().items():
                grant = pool * room[sector] / total_room if room[sector] > 1e-12 else 0.0
                if grant <= 1e-12:
                    continue
                held = sum(scaled[m] for m in members)
                for symbol in members:
                    scaled[symbol] += grant * (scaled[symbol] / held if held > 0
                                               else 1.0 / len(members))
        return scaled

    # ---- high-water / trailing stops -----------------------------------
    def update_high_water(
        self,
        account,
        bars: dict,
        quotes: dict,
    ) -> None:
        """Track high-water price per open position from latest marks."""
        prices: dict[str, float] = {s: b.close for s, b in bars.items()}
        for symbol, quote in quotes.items():
            if getattr(quote, "usable", False):
                prices[symbol] = quote.price
        for symbol, position in account.positions.items():
            if not position.is_open():
                continue
            price = prices.get(symbol, position.last_price)
            if price > 0:
                prev = self._high_water.get(symbol)
                if prev is None or price > prev:
                    self._high_water[symbol] = price
            if symbol not in self._entry_price:
                self._entry_price[symbol] = position.avg_price

    def check_trailing_stops(
        self,
        account,
        bars: dict,
        quotes: dict,
    ) -> list[OrderRequest]:
        """Exit requests for positions breaching trailing stop or take-profit.

        Checked against stored high-water (updated separately via
        update_high_water so a new intra-bar high doesn't suppress the stop).
        """
        exits: list[OrderRequest] = []
        prices: dict[str, float] = {s: b.close for s, b in bars.items()}
        for symbol, quote in quotes.items():
            if getattr(quote, "usable", False):
                prices[symbol] = quote.price

        for symbol, position in account.positions.items():
            if not position.is_open():
                continue
            price = prices.get(symbol, position.last_price)
            if price <= 0:
                continue
            high = self._high_water.get(symbol, price)
            entry = self._entry_price.get(symbol, position.avg_price)

            if high > 0 and price <= high * (1.0 - self.config.trailing_stop_pct):
                exits.append(
                    OrderRequest(
                        symbol=symbol,
                        side=Side.SELL,
                        qty=position.qty,
                        strategy="trailing-stop",
                        signal_bar_ts=utcnow(),
                        reason=(
                            f"trailing stop at {price:.2f} vs high {high:.2f}"
                        ),
                        reference_price=price,
                    )
                )
                continue

            if entry > 0 and price >= entry * (1.0 + self.config.take_profit_pct):
                exits.append(
                    OrderRequest(
                        symbol=symbol,
                        side=Side.SELL,
                        qty=position.qty,
                        strategy="take-profit",
                        signal_bar_ts=utcnow(),
                        reason=(
                            f"take profit at {price:.2f} vs entry {entry:.2f}"
                        ),
                        reference_price=price,
                    )
                )

        return exits

    # ---- dashboard state -------------------------------------------------
    def get_state(self) -> dict:
        """Everything the dashboard needs to show about the portfolio."""
        peak = self._peak_equity
        return {
            "enabled": self.config.enabled,
            "top_n": self.config.top_n,
            "lookback": self.config.lookback,
            "skip": self.config.skip,
            "vol_target": self.config.vol_target,
            "max_position_pct": self.config.max_position_pct,
            "max_sector_pct": self.config.max_sector_pct,
            "rebalance_cadence": self.config.rebalance_cadence,
            "trailing_stop_pct": self.config.trailing_stop_pct,
            "take_profit_pct": self.config.take_profit_pct,
            "mdd_circuit_breaker_pct": self.config.mdd_circuit_breaker_pct,
            "last_rebalance": self._last_rebalance.isoformat() if self._last_rebalance else None,
            "targets": {k: round(v, 4) for k, v in self._last_targets.items()},
            "scores": [(s, round(score, 4)) for s, score in self._last_scores[: self.config.top_n]],
            "peak_equity": round(peak, 2) if peak is not None else None,
            "high_water": {k: round(v, 2) for k, v in self._high_water.items()},
            "entry_price": {k: round(v, 2) for k, v in self._entry_price.items()},
        }
