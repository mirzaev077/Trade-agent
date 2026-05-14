"""BacktestConfig va bog'liq dataclass'lar uchun unit testlar.

Validatsiya, immutability va default qiymatlarni tekshiradi.

F2-2 port: TMAS tests/unit/test_config.py dan ko'chirilgan. Import yo'llar
``apps.api.src.agents.trader.engine`` dan oladi.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path

import pytest

from apps.api.src.agents.trader.core.broker import BrokerConfig
from apps.api.src.agents.trader.engine.config import (
    AnalystParams,
    BacktestConfig,
    DailyRiskState,
    DynamicRiskConfig,
    RiskConfig,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_config(**overrides) -> BacktestConfig:
    """Minimal valid BacktestConfig builder for tests."""
    defaults = dict(
        start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end=datetime(2024, 12, 31, tzinfo=timezone.utc),
        symbol="EURUSD",
        data_path=Path("/tmp/data"),
        broker=BrokerConfig(
            initial_balance=10_000.0,
            commission_per_lot=7.0,
            slippage_model="fixed",
            swap_rates={},
        ),
        risk_config=RiskConfig(),
    )
    defaults.update(overrides)
    return BacktestConfig(**defaults)


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_backtest_config_requires_tz_aware_start() -> None:
    """Naive datetime for start or end must raise ValueError."""
    with pytest.raises(ValueError, match="tz-aware"):
        _make_config(start=datetime(2024, 1, 1))

    with pytest.raises(ValueError, match="tz-aware"):
        _make_config(end=datetime(2024, 12, 31))


def test_backtest_config_defaults() -> None:
    """Default fields must match the spec."""
    cfg = _make_config()

    assert cfg.min_confluence == 0.6
    assert cfg.seed == 42
    assert cfg.reflector_shadow_mode is True
    assert cfg.primary_timeframe == "M15"
    assert cfg.timeframes == ("M15", "H1", "H4")
    assert isinstance(cfg.analyst_params, AnalystParams)


def test_backtest_config_run_id_unique() -> None:
    """Each BacktestConfig must generate a unique run_id (UUID4)."""
    cfg1 = _make_config()
    cfg2 = _make_config()

    assert cfg1.run_id != cfg2.run_id


def test_backtest_config_invalid_ordering() -> None:
    """end <= start, empty symbol, and primary_timeframe not in timeframes must raise."""
    with pytest.raises(ValueError):
        _make_config(
            start=datetime(2024, 6, 1, tzinfo=timezone.utc),
            end=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )

    with pytest.raises(ValueError):
        _make_config(symbol="")

    with pytest.raises(ValueError):
        _make_config(primary_timeframe="M5")


def test_risk_config_immutable() -> None:
    """RiskConfig is frozen — mutation must raise FrozenInstanceError."""
    cfg = RiskConfig()

    with pytest.raises(FrozenInstanceError):
        cfg.risk_per_trade_pct = 2.0  # type: ignore[misc]


def test_dynamic_risk_state_mutable() -> None:
    """DynamicRiskConfig and DailyRiskState are mutable runtime state."""
    dyn = DynamicRiskConfig()
    dyn.consecutive_losses = 5
    assert dyn.consecutive_losses == 5

    daily = DailyRiskState()
    daily.trades_today = 3
    assert daily.trades_today == 3
