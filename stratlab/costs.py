"""Indian and crypto trading cost models.

Defaults reflect published statutory charges for India and Binance spot. They are
starting points, not gospel: verify every line against your own contract note
before trusting any backtest result.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    """Per-side transaction costs as fractions of turnover (0.001 == 0.1%)."""

    name: str
    stt_buy: float = 0.0
    stt_sell: float = 0.0
    exchange_txn: float = 0.0
    sebi: float = 0.0
    ipft: float = 0.0
    stamp_duty: float = 0.0
    gst: float = 0.0
    brokerage_pct: float = 0.0
    brokerage_flat: float = 0.0
    min_brokerage: float = 0.0
    slippage_bps: float = 5.0

    def brokerage(self, turnover: float) -> float:
        fee = max(self.brokerage_flat, turnover * self.brokerage_pct)
        return max(fee, self.min_brokerage) if turnover > 0 else 0.0

    def charges(self, side: str, turnover: float) -> float:
        """Total explicit cost of one fill, slippage excluded."""
        is_buy = side.upper() == "BUY"
        stt = self.stt_buy if is_buy else self.stt_sell
        stamp = self.stamp_duty if is_buy else 0.0
        brokerage = self.brokerage(turnover)
        taxable = turnover * (self.exchange_txn + self.sebi + self.ipft) + brokerage
        return turnover * (stt + stamp + self.exchange_txn + self.sebi + self.ipft) + taxable * self.gst

    def fill_price(self, reference: float, side: str) -> float:
        """Apply slippage against the trade direction."""
        rate = self.slippage_bps / 10_000
        return reference * (1 + rate) if side.upper() == "BUY" else reference * (1 - rate)

    def breakdown(self) -> dict:
        """Round-trip cost split on a 100-unit notional, in percent."""
        notional = 100.0
        buy, sell = self.charges("BUY", notional), self.charges("SELL", notional)
        stt = notional * (self.stt_buy + self.stt_sell)
        stamp = notional * self.stamp_duty
        slippage = notional * (self.slippage_bps / 10_000) * 2
        other = buy + sell - stt - stamp
        return {
            "stt": round(stt, 4),
            "stamp_duty": round(stamp, 4),
            "other_charges": round(other, 4),
            "statutory": round(stt + stamp + other, 4),
            "slippage": round(slippage, 4),
            "total": round(buy + sell + slippage, 4),
        }

    def round_trip_pct(self) -> float:
        """Cost of a buy immediately followed by a sell, as a percentage of notional.

        Priced on a 100-rupee notional, so the currency amount charged is also
        the percentage figure.
        """
        cost = self.charges("BUY", 100.0) + self.charges("SELL", 100.0)
        slippage = 100.0 * (self.slippage_bps / 10_000) * 2
        return cost + slippage


# Equity delivery, post-Oct-2024 STT regime: 0.1% each side.
# Zerodha-style brokers charge no brokerage on delivery, so flat/pct stay zero.
INDIA_EQUITY_DELIVERY = CostModel(
    name="India equity - delivery",
    stt_buy=0.001,
    stt_sell=0.001,
    exchange_txn=0.0000297,
    sebi=0.000001,
    ipft=0.00000005,
    stamp_duty=0.00015,
    gst=0.18,
    slippage_bps=5.0,
)

# Intraday STT is charged on the sell side only, and stamp duty is lower.
INDIA_EQUITY_INTRADAY = CostModel(
    name="India equity - intraday",
    stt_buy=0.0,
    stt_sell=0.00025,
    exchange_txn=0.0000297,
    sebi=0.000001,
    ipft=0.00000005,
    stamp_duty=0.00003,
    gst=0.18,
    slippage_bps=5.0,
)

# Binance spot without BNB discount. GST/TDS are personal liabilities, not
# execution costs, so they are deliberately excluded from the backtest.
BINANCE_SPOT = CostModel(
    name="Binance spot",
    exchange_txn=0.001,
    slippage_bps=5.0,
)

CRYPTO_SPOT = BINANCE_SPOT

MARKET = "market"
INTRADAY = "intraday"


def cost_model_for(asset_class: str, market: str = MARKET) -> CostModel:
    asset_class = asset_class.lower()
    if asset_class in {"crypto", "cryptocurrency"}:
        return CRYPTO_SPOT
    if market == INTRADAY:
        return INDIA_EQUITY_INTRADAY
    return INDIA_EQUITY_DELIVERY