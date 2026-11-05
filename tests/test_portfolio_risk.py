"""Portfolio risk: sector caps must cap without stranding capital.

Clamping an over-weight sector quietly leaves the book short of its gross
target. The freed weight is redistributed into sectors that still have room,
and a sector can never be granted past its own cap.
"""

from __future__ import annotations

import pytest

from stratbot.portfolio import PortfolioConfig, PortfolioRiskManager


class NullDB:
    def log_risk_event(self, *args, **kwargs):
        return None


def manager(cap: float = 0.25) -> PortfolioRiskManager:
    return PortfolioRiskManager(
        PortfolioConfig(enabled=True, max_sector_pct=cap), NullDB())


def sector_totals(pm: PortfolioRiskManager, weights: dict[str, float]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for symbol, weight in weights.items():
        sector = pm.sector_map.get(symbol, "other")
        totals[sector] = totals.get(sector, 0.0) + weight
    return totals


class TestSectorCaps:
    def test_freed_weight_is_redistributed(self):
        pm = manager()
        weights = {"BTC": 0.5, "ETH": 0.1, "RELIANCE": 0.1, "TCS": 0.1,
                   "HDFCBANK": 0.1, "ITC": 0.1}
        capped = pm._apply_sector_caps(weights)
        assert sum(capped.values()) == pytest.approx(1.0)
        assert max(sector_totals(pm, capped).values()) == pytest.approx(0.25)

    def test_no_sector_is_ever_pushed_back_over_the_cap(self):
        pm = manager()
        weights = {"BTC": 0.6, "ETH": 0.2, "SOL": 0.1, "RELIANCE": 0.05,
                   "TCS": 0.05}
        capped = pm._apply_sector_caps(weights)
        assert all(total <= 0.25 + 1e-9
                   for total in sector_totals(pm, capped).values())

    def test_a_single_sector_book_stays_at_the_cap(self):
        pm = manager()
        weights = {symbol: 0.1 for symbol in
                   ("BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "AVAX", "LINK", "DOT", "LTC")}
        capped = pm._apply_sector_caps(weights)
        assert sum(capped.values()) == pytest.approx(0.25)
        assert sector_totals(pm, capped)["crypto"] == pytest.approx(0.25)

    def test_a_book_already_inside_its_caps_is_untouched(self):
        pm = manager()
        weights = {"BTC": 0.2, "RELIANCE": 0.2, "TCS": 0.2, "GOLD": 0.2}
        capped = pm._apply_sector_caps(weights)
        assert capped == pytest.approx(weights)

    def test_unmapped_symbols_share_the_other_bucket(self):
        pm = manager(cap=0.25)
        weights = {"UNKNOWN1": 0.4, "UNKNOWN2": 0.4, "BTC": 0.2}
        capped = pm._apply_sector_caps(weights)
        assert sector_totals(pm, capped)["other"] <= 0.25 + 1e-9
        # "other" is capped at 0.25 and crypto has 0.05 of room, so the book ends
        # short of its target: the cap binds rather than being worked around.
        assert sector_totals(pm, capped)["crypto"] == pytest.approx(0.25)
        assert sum(capped.values()) == pytest.approx(0.5)

    def test_empty_book_stays_empty(self):
        assert manager()._apply_sector_caps({}) == {}
