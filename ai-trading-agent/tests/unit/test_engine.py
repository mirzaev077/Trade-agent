"""BacktestEngine helper metodlari uchun unit testlar (data-free).

F2-2 port: TMAS tests/unit/test_engine.py dan ko'chirilgan. Import yo'llar
``apps.api.src.agents.trader.engine`` dan oladi. ``flush_closed_trades`` testi
StubRisk uchun moslashtirilgan (TMAS RiskAgent.dynamic.consecutive_losses
maydoni stub'da yo'q — record_trade_outcome no-op).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apps.api.src.agents.trader.core.broker import BrokerConfig
from apps.api.src.agents.trader.engine.config import BacktestConfig, RiskConfig
from apps.api.src.agents.trader.engine.engine import BacktestEngine
from apps.api.src.agents.trader.engine.stubs import StubReflector, StubRisk


def _broker_config() -> BrokerConfig:
    return BrokerConfig(
        initial_balance=10_000.0,
        commission_per_lot=7.0,
        slippage_model="fixed",
        swap_rates={},
    )


def _engine(tmp_path: Path, **overrides) -> BacktestEngine:
    cfg = BacktestConfig(
        start=datetime(2026, 5, 13, 0, 0, tzinfo=timezone.utc),
        end=datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc),
        symbol="EURUSD",
        data_path=tmp_path,
        broker=_broker_config(),
        risk_config=RiskConfig(),
        primary_timeframe=overrides.get("primary_timeframe", "M15"),
    )
    return BacktestEngine(cfg)


class TestEngineInit:
    def test_components_wired(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)

        assert engine.clock is not None
        assert engine.data is not None
        assert engine.broker is not None
        assert engine.bus is not None
        assert engine.reflector is not None
        assert engine.risk is not None
        # F2-2.D: analyst is now caller-provided (None by default). F2-2.E
        # da ICTAdapter ulanadi va engine.run() ishlatish mumkin bo'ladi.
        assert engine.analyst is None
        assert engine.journal is not None

    def test_default_reflector_is_stub(self, tmp_path: Path) -> None:
        """F2-2 port: default reflector StubReflector bo'lishi shart."""
        engine = _engine(tmp_path)
        assert isinstance(engine.reflector, StubReflector)

    def test_default_risk_is_stub(self, tmp_path: Path) -> None:
        """F2-2 port: default risk StubRisk bo'lishi shart."""
        engine = _engine(tmp_path)
        assert isinstance(engine.risk, StubRisk)

    def test_journal_uses_config_run_id(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        assert engine.journal.run_id == engine.config.run_id

    def test_reflector_shadow_mode_default_true(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        assert engine.reflector.shadow_mode is True


class TestPrimaryStep:
    @pytest.mark.parametrize(
        "tf, expected_secs",
        [("M1", 60), ("M5", 300), ("M15", 900), ("H1", 3600), ("H4", 14400), ("D1", 86400)],
    )
    def test_primary_step_known_tf(self, tmp_path: Path, tf: str, expected_secs: int) -> None:
        cfg = BacktestConfig(
            start=datetime(2026, 5, 13, 0, 0, tzinfo=timezone.utc),
            end=datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc),
            symbol="EURUSD",
            data_path=tmp_path,
            broker=_broker_config(),
            risk_config=RiskConfig(),
            primary_timeframe=tf,
            timeframes=(tf,),
        )
        engine = BacktestEngine(cfg)
        assert engine._primary_step().total_seconds() == expected_secs


class TestTradingHoursFilter:
    def _eng(self, tmp_path: Path) -> BacktestEngine:
        return _engine(tmp_path)

    def test_weekday_allowed(self, tmp_path: Path) -> None:
        eng = self._eng(tmp_path)
        # Wed 2026-05-13 14:00 UTC
        assert eng._is_trading_allowed(datetime(2026, 5, 13, 14, tzinfo=timezone.utc))

    def test_friday_evening_blocked(self, tmp_path: Path) -> None:
        eng = self._eng(tmp_path)
        # Fri 2026-05-15 22:30 UTC
        assert not eng._is_trading_allowed(datetime(2026, 5, 15, 22, 30, tzinfo=timezone.utc))

    def test_friday_morning_allowed(self, tmp_path: Path) -> None:
        eng = self._eng(tmp_path)
        # Fri 2026-05-15 10:00 UTC
        assert eng._is_trading_allowed(datetime(2026, 5, 15, 10, tzinfo=timezone.utc))

    def test_saturday_blocked(self, tmp_path: Path) -> None:
        eng = self._eng(tmp_path)
        assert not eng._is_trading_allowed(datetime(2026, 5, 16, 10, tzinfo=timezone.utc))

    def test_sunday_blocked(self, tmp_path: Path) -> None:
        eng = self._eng(tmp_path)
        assert not eng._is_trading_allowed(datetime(2026, 5, 17, 10, tzinfo=timezone.utc))

    def test_monday_allowed(self, tmp_path: Path) -> None:
        eng = self._eng(tmp_path)
        assert eng._is_trading_allowed(datetime(2026, 5, 18, 1, tzinfo=timezone.utc))


class TestNewSession:
    def test_first_tick_not_new_session(self, tmp_path: Path) -> None:
        eng = _engine(tmp_path)
        now = datetime(2026, 5, 13, 0, tzinfo=timezone.utc)
        assert eng._is_new_session(None, now) is False

    def test_same_day_not_new_session(self, tmp_path: Path) -> None:
        eng = _engine(tmp_path)
        prev = datetime(2026, 5, 13, 10, tzinfo=timezone.utc)
        now = datetime(2026, 5, 13, 14, tzinfo=timezone.utc)
        assert eng._is_new_session(prev, now) is False

    def test_day_rollover_is_new_session(self, tmp_path: Path) -> None:
        eng = _engine(tmp_path)
        prev = datetime(2026, 5, 13, 23, 59, tzinfo=timezone.utc)
        now = datetime(2026, 5, 14, 0, 1, tzinfo=timezone.utc)
        assert eng._is_new_session(prev, now) is True


class TestBuildResult:
    def test_skeleton_result(self, tmp_path: Path) -> None:
        eng = _engine(tmp_path)
        started = datetime(2026, 5, 13, 0, tzinfo=timezone.utc)
        ended = datetime(2026, 5, 13, 1, tzinfo=timezone.utc)

        result = eng._build_result(started, ended)

        assert result.run_id == eng.config.run_id
        assert result.started_at == started
        assert result.ended_at == ended
        assert result.verdict == "PENDING"
        assert result.total_trades == 0  # no trades yet
        assert result.sharpe is None  # PerformanceAnalyzer to'ldiradi


class TestConfigToDict:
    def test_dict_contains_required_keys(self, tmp_path: Path) -> None:
        eng = _engine(tmp_path)
        d = eng._config_to_dict()

        for key in (
            "run_id", "start", "end", "symbol", "primary_timeframe",
            "timeframes", "data_path", "risk_config", "broker",
        ):
            assert key in d

        assert d["start"].endswith("+00:00")  # ISO UTC
        assert isinstance(d["timeframes"], list)


class TestFlushClosedTrades:
    def test_flush_logs_new_trades_and_resets_counter(self, tmp_path: Path) -> None:
        """StubRisk version: TMAS RiskAgent.dynamic streak tracking yo'q —
        bu yerda faqat _last_history_len yangilanishini va journal'ga
        log_trade_closed chaqirilishini tekshiramiz."""
        eng = _engine(tmp_path)
        closed = MagicMock()
        closed.ticket = 1
        closed.pnl = 50.0
        closed.exit_time = datetime(2026, 5, 13, 12, tzinfo=timezone.utc)
        closed.exit_price = 1.1041
        closed.close_reason = "tp"
        closed.swap = 0.0

        eng.broker.history.append(closed)
        eng._flush_closed_trades()

        assert eng._last_history_len == 1

        # Yana flush qilsa — bo'sh ish (counter o'zgarmaydi)
        eng._flush_closed_trades()
        assert eng._last_history_len == 1


class TestRunRequiresAnalyst:
    """F2-2 port: analyst kwarg endi majburiy. None bo'lsa run() RuntimeError beradi."""

    def test_run_without_analyst_raises(self, tmp_path: Path) -> None:
        eng = _engine(tmp_path)
        assert eng.analyst is None
        with pytest.raises(RuntimeError, match="analyst"):
            eng.run()
