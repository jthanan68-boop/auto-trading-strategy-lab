"""Intraday mode stage 4: minutes-freshness, 1% stop, style-aware costs.

The exposure caps (100M position, 300k total, 10k daily loss, 100% of equity)
and the 20/hour + 100/day order caps must come out of the intraday profile
byte-for-byte equal to the daily ones. Only the stop and the freshness gates
move. All orders here are simulated; no broker is contacted.
"""

from __future__ import annotations

from datetime import timedelta

from stratbot.broker.paper import PaperBroker
from stratbot.config import RiskLimits, intraday_limits
from stratbot.db import Database
from stratbot.models import (
    AccountState,
    Bar,
    HaltReason,
    OrderRequest,
    OrderStatus,
    Position,
    Side,
    utcnow,
)
from stratbot.risk import RiskManager


def aged_bar(symbol: str, minutes: float = 5.0, days: float = 0.0) -> Bar:
    ts = utcnow() - timedelta(days=days, minutes=minutes)
    return Bar(symbol=symbol, ts=ts, open=100.0, high=100.0, low=100.0,
               close=100.0, volume=1.0)


def account(equity: float = 1_000_000, positions=None) -> AccountState:
    return AccountState(cash=equity, positions=positions or {})


class TestIntradayLimitsProfile:
    def test_exposure_and_rate_caps_are_carried_through_unchanged(self):
        base = RiskLimits()
        limits = intraday_limits()
        assert limits.max_position_value == base.max_position_value
        assert limits.max_total_exposure == base.max_total_exposure
        assert limits.max_daily_loss == base.max_daily_loss
        assert limits.max_position_pct_of_equity == base.max_position_pct_of_equity
        assert limits.max_orders_per_hour == base.max_orders_per_hour == 20
        assert limits.max_orders_per_day == base.max_orders_per_day == 100

    def test_only_stop_and_freshness_change(self):
        limits = intraday_limits()
        assert limits.stop_loss_pct == 0.01
        assert limits.max_bar_age_minutes == 120
        assert limits.warn_bar_age_minutes == 30
        assert RiskLimits().stop_loss_pct == 0.08
        assert RiskLimits().max_bar_age_minutes is None


class TestMinutesFreshness:
    def db(self, tmp_path) -> Database:
        return Database(tmp_path / "risk.sqlite3")

    def test_fresh_bar_is_approved(self, tmp_path):
        risk = RiskManager(self.db(tmp_path), intraday_limits())
        assert risk.check_bar_freshness(aged_bar("RELIANCE", 5),
                                        session_live=True).approved

    def test_stale_bar_halts_when_the_session_is_live(self, tmp_path):
        db = self.db(tmp_path)
        risk = RiskManager(db, intraday_limits())
        decision = risk.check_bar_freshness(aged_bar("RELIANCE", 121),
                                            session_live=True)
        assert not decision.approved
        assert risk.halted and risk.halt_reason is HaltReason.STALE_PRICE

    def test_warning_only_between_warn_and_halt(self, tmp_path):
        db = self.db(tmp_path)
        risk = RiskManager(db, intraday_limits())
        decision = risk.check_bar_freshness(aged_bar("RELIANCE", 45),
                                            session_live=True)
        assert decision.approved
        events = db.recent_risk_events()
        assert any(e["kind"] == "stale_bar_warning" for e in events)

    def test_quiet_class_never_halts_on_minutes(self, tmp_path):
        """Holiday case: no equity peer published today, so no minutes halt."""
        db = self.db(tmp_path)
        risk = RiskManager(db, intraday_limits())
        decision = risk.check_bar_freshness(aged_bar("RELIANCE", 200),
                                            session_live=False)
        assert decision.approved
        assert not risk.halted

    def test_quiet_class_falls_back_to_day_gate(self, tmp_path):
        db = self.db(tmp_path)
        risk = RiskManager(db, intraday_limits())
        decision = risk.check_bar_freshness(
            Bar(symbol="RELIANCE", ts=utcnow() - timedelta(days=5),
                open=100, high=100, low=100, close=100, volume=1.0),
            session_live=False,
        )
        assert not decision.approved
        assert risk.halt_reason is HaltReason.STALE_PRICE

    def test_daily_limits_keep_the_day_gate_unaffected(self, tmp_path):
        risk = RiskManager(self.db(tmp_path), RiskLimits())
        assert risk.check_bar_freshness(
            aged_bar("BTC", 180)  # 3 hours old: stale by minute rules, fine for days
        ).approved


class TestIntradayStop:
    def _risk(self, tmp_path) -> RiskManager:
        return RiskManager(Database(tmp_path / "risk.sqlite3"), intraday_limits())

    def _position(self, avg_price: float) -> AccountState:
        held = Position(symbol="TCS", qty=10.0, avg_price=avg_price,
                        opened_at=utcnow())
        return account(positions={"TCS": held})

    def test_breach_at_one_percent_queues_an_exit(self, tmp_path):
        risk = self._risk(tmp_path)
        bar = Bar(symbol="TCS", ts=utcnow(), open=100.0, high=100.0,
                  low=98.0, close=98.4, volume=1.0)  # -1.6% from 100
        exits = risk.check_positions(self._position(100.0), {"TCS": bar})
        assert len(exits) == 1
        assert exits[0].side is Side.SELL
        assert exits[0].strategy == "risk-stop-loss"

    def test_no_exit_within_the_band(self, tmp_path):
        risk = self._risk(tmp_path)
        bar = Bar(symbol="TCS", ts=utcnow(), open=100.0, high=100.0,
                  low=99.4, close=99.6, volume=1.0)  # -0.4% from 100
        assert risk.check_positions(self._position(100.0), {"TCS": bar}) == []


def eq_qty(price: float, qty: float) -> OrderRequest:
    return OrderRequest(symbol="TCS", side=Side.BUY, qty=qty, strategy="test",
                        signal_bar_ts=utcnow(), reference_price=price)


class TestStyleAwareCosts:
    def test_delivery_buy_fees_exceed_intraday_buy_fees(self):
        daily = PaperBroker(1_000_000.0, trading_style="daily")
        intraday = PaperBroker(1_000_000.0, trading_style="intraday")
        request = eq_qty(100.0, 100.0)
        _, fill_daily = daily.submit(request, 100.0)
        _, fill_intraday = intraday.submit(eq_qty(100.0, 100.0), 100.0)
        # Identical execution price means identical notional: the whole fee gap
        # is STT + stamp duty, which only the style switches.
        assert fill_intraday.fees < fill_daily.fees

    def test_crypto_costs_are_style_free(self):
        daily = PaperBroker(1_000_000.0, trading_style="daily")
        intraday = PaperBroker(1_000_000.0, trading_style="intraday")
        request = OrderRequest(symbol="BTC", side=Side.BUY, qty=1.0,
                               strategy="test", signal_bar_ts=utcnow())
        _, fill_daily = daily.submit(request, 100.0)
        _, fill_intraday = intraday.submit(OrderRequest(
            symbol="BTC", side=Side.BUY, qty=1.0, strategy="test",
            signal_bar_ts=utcnow()), 100.0)
        assert fill_daily.fees == fill_intraday.fees


class TestEngineGates:
    def _engine(self, tmp_path, style: str, symbols: tuple[str, ...]):
        from stratbot.config import BotConfig
        from stratbot.engine import Engine

        engine = Engine(
            BotConfig(symbols=symbols, strategy_name="buy-and-hold",
                      trading_style=style, bar_interval="5m"),
            Database(tmp_path / "engine.sqlite3"),
        )
        return engine

    def test_class_wide_strictness(self, tmp_path):
        engine = self._engine(tmp_path, "intraday", ("BTC", "RELIANCE", "TCS"))
        bars = {
            "BTC": aged_bar("BTC", 5),                      # crypto is always publishing
            "RELIANCE": aged_bar("RELIANCE", 30, days=1),   # yesterday
            "TCS": aged_bar("TCS", 30, days=1),             # equity bought nothing today
        }
        gates = engine._freshness_gates(bars)
        # Strictness is granted per class, not per symbol: equity got no bar
        # today, so both equity symbols are lenient no matter how old they are.
        assert gates["BTC"] is True
        assert gates["RELIANCE"] is False
        assert gates["TCS"] is False

        fresh_equity = dict(bars, TCS=aged_bar("TCS", 5))
        gates = engine._freshness_gates(fresh_equity)
        assert gates["RELIANCE"] is True
        assert gates["TCS"] is True

    def test_daily_style_reads_day_gates(self, tmp_path):
        engine = self._engine(tmp_path, "daily", ("RELIANCE",))
        bars = {"RELIANCE": aged_bar("RELIANCE", 200)}
        gates = engine._freshness_gates(bars)
        assert gates["RELIANCE"] is True  # bar stamped today -> strictly judged

    def test_intraday_equity_book_gets_the_intraday_cost_model(self, tmp_path):
        equity_only = self._engine(tmp_path, "intraday", ("RELIANCE", "TCS"))
        assert equity_only.cost_model.name == "India equity - intraday"

        with_crypto = self._engine(tmp_path, "intraday", ("BTC", "RELIANCE"))
        assert with_crypto.cost_model.name == "Binance spot"

        daily_book = self._engine(tmp_path, "daily", ("RELIANCE", "TCS"))
        assert daily_book.cost_model.name == "India equity - delivery"


class TestBuildConfig:
    def _args(self, **kw):
        from types import SimpleNamespace

        base = dict(style=None, interval=None, strategy=None, days_history=None,
                    capital=None, symbols=None, refresh_interval=None, poll=None)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_defaults_resolve_to_the_configured_intraday_profile(self):
        from run_bot import build_config

        config = build_config(self._args())
        assert config.trading_style == "intraday"
        assert config.strategy_name == "orb-15min"
        assert config.refresh_interval_minutes == 5
        assert config.poll_seconds == 60
        assert config.limits == intraday_limits()

    def test_explicit_daily_restores_the_historical_cadence(self):
        from run_bot import build_config

        config = build_config(self._args(style="daily"))
        assert config.trading_style == "daily"
        assert config.strategy_name == "donchian-55/20"
        assert config.refresh_interval_minutes == 60
        assert config.poll_seconds == 300
        assert config.limits == RiskLimits()

    def test_intraday_style_applies_the_session_profile(self):
        from run_bot import build_config

        config = build_config(self._args(style="intraday", interval="5m"))
        assert config.trading_style == "intraday"
        assert config.strategy_name == "orb-15min"
        assert config.limits == intraday_limits()
        assert config.refresh_interval_minutes == 5
        assert config.poll_seconds == 60

    def test_explicit_strategy_beats_the_style_default(self):
        from run_bot import build_config

        config = build_config(self._args(style="intraday", strategy="vwap-dip",
                                         interval="15m"))
        assert config.strategy_name == "vwap-dip"
        assert config.bar_interval == "15m"

    def test_unknown_interval_fails_before_anything_opens(self):
        from run_bot import build_config

        import pytest

        with pytest.raises(ValueError):
            build_config(self._args(style="intraday", interval="7m"))

