"""F2-2.F: End-to-end smoke test — backtest runs with synthetic data.

Also exercises the ``PaperBroker._get_spread`` bug fix by using a real
``HistoricalDataManager`` (no mocks) so the two-arg signature is enforced.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from apps.api.src.agents.trader.core.broker import BrokerConfig, PaperBroker
from apps.api.src.agents.trader.core.clock import VirtualClock, set_clock
from apps.api.src.agents.trader.core.data import HistoricalDataManager
from apps.api.src.agents.trader.engine.analyst_ict import ICTAnalyst
from apps.api.src.agents.trader.engine.config import BacktestConfig, RiskConfig
from apps.api.src.agents.trader.engine.engine import BacktestEngine


# ── Helpers ──────────────────────────────────────────────────────────────────


_FREQ_MAP: dict[str, str] = {"M15": "15min", "H1": "1h", "H4": "4h", "M1": "1min"}


def _make_synthetic_candles(
    start: datetime,
    end: datetime,
    freq: str,
    base: float = 2000.0,
    seed: int = 42,
) -> pd.DataFrame:
    """Random-walk XAUUSD-ish candles."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start=start, end=end, freq=freq, inclusive="left")
    n = len(idx)
    returns = rng.normal(0, 0.0015, n)
    close = base * np.exp(np.cumsum(returns))
    open_ = np.roll(close, 1)
    open_[0] = base
    body = np.abs(rng.normal(0, 0.0005, n))
    high = np.maximum(open_, close) * (1 + body)
    low = np.minimum(open_, close) * (1 - body)
    vol = rng.integers(100, 1000, n)

    return pd.DataFrame(
        {
            "timestamp": idx,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "tick_volume": vol,
        }
    )


@pytest.fixture
def synthetic_data_dir(tmp_path: Path) -> Path:
    """Generate 7 days of XAUUSD synthetic data + warmup, write as CSVs."""
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=7)
    warmup_start = start - timedelta(days=35)
    for tf, freq in [("M15", "15min"), ("H1", "1h"), ("H4", "4h"), ("M1", "1min")]:
        df = _make_synthetic_candles(warmup_start, end, freq=freq, seed=42 + len(tf))
        df.to_csv(tmp_path / f"XAUUSD_{tf}.csv", index=False)
    return tmp_path


# ── Tests ────────────────────────────────────────────────────────────────────


def test_get_spread_no_typeerror_with_real_data_manager(synthetic_data_dir: Path) -> None:
    """F2-2.F bug fix: PaperBroker._get_spread must call
    HistoricalDataManager._estimate_spread with TWO args (symbol, timestamp).

    Pre-fix this raised TypeError because the broker passed only `symbol`.
    We construct a REAL HistoricalDataManager (no mocks) and trigger
    _get_spread via place_order(market). The test passes if no TypeError
    bubbles up.
    """
    start = datetime(2024, 1, 2, 12, 0, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    clock = VirtualClock(start=start, end=end)
    data = HistoricalDataManager(data_path=synthetic_data_dir)
    data.preload("XAUUSD", timeframes=["M1", "M15", "H1", "H4"])

    broker_cfg = BrokerConfig(
        initial_balance=10000.0,
        commission_per_lot=3.0,
        slippage_model="fixed",
        swap_rates={},
        fixed_slippage_pips=1.0,
        leverage=100,
        contract_size=100,
    )
    broker = PaperBroker(config=broker_cfg, clock=clock, data=data)

    # Force a direct call — must not raise TypeError.
    spread = broker._get_spread("XAUUSD", clock.now())
    assert isinstance(spread, float)
    assert spread > 0

    # Now exercise the place_order(market) path, which calls _get_spread
    # via _fill_market_order.
    result = broker.place_order(
        symbol="XAUUSD",
        direction="buy",
        order_type="market",
        lot=0.01,
    )
    # The order may succeed or fail (e.g. NO_DATA depending on bar lookup),
    # but it MUST NOT raise TypeError from the spread call.
    assert result is not None


def test_backtest_e2e_runs_to_completion(synthetic_data_dir: Path) -> None:
    """Pipeline executes without exception; result has reasonable shape."""
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=7)

    clock = VirtualClock(start=start, end=end)
    set_clock(clock)

    try:
        data = HistoricalDataManager(data_path=synthetic_data_dir)
        data.preload("XAUUSD", timeframes=["H4", "H1", "M15"])

        config = BacktestConfig(
            start=start,
            end=end,
            symbol="XAUUSD",
            data_path=synthetic_data_dir,
            broker=BrokerConfig(
                initial_balance=10000.0,
                commission_per_lot=3.0,
                slippage_model="fixed",
                swap_rates={},
                fixed_slippage_pips=1.0,
                leverage=100,
                contract_size=100,
            ),
            risk_config=RiskConfig(),
            primary_timeframe="M15",
            timeframes=("M15", "H1", "H4"),
        )

        analyst = ICTAnalyst(clock=clock, data=data)
        engine = BacktestEngine(config, analyst=analyst)
        # Share clock + data so analyst sees same simulated state as engine
        analyst.clock = engine.clock
        analyst.data = engine.data

        result = engine.run()

        # Sanity checks — synthetic random-walk may not produce trades, but the
        # pipeline must complete and the result must be well-formed.
        assert result is not None
        assert result.total_trades >= 0
        assert result.started_at <= result.ended_at
        # Initial balance preserved (or close to it for 0 trades).
        assert engine.broker.balance > 0
    finally:
        set_clock(None)


def test_backtest_e2e_with_zero_trades(synthetic_data_dir: Path) -> None:
    """Tight time window → no trades, no crash."""
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(hours=2)  # only 8 M15 bars — too short for any HTF bias

    clock = VirtualClock(start=start, end=end)
    set_clock(clock)

    try:
        data = HistoricalDataManager(data_path=synthetic_data_dir)
        data.preload("XAUUSD", timeframes=["H4", "H1", "M15"])

        config = BacktestConfig(
            start=start,
            end=end,
            symbol="XAUUSD",
            data_path=synthetic_data_dir,
            broker=BrokerConfig(
                initial_balance=10000.0,
                commission_per_lot=3.0,
                slippage_model="fixed",
                swap_rates={},
                fixed_slippage_pips=1.0,
                leverage=100,
                contract_size=100,
            ),
            risk_config=RiskConfig(),
            primary_timeframe="M15",
            timeframes=("M15", "H1", "H4"),
        )

        analyst = ICTAnalyst(clock=clock, data=data)
        engine = BacktestEngine(config, analyst=analyst)
        analyst.clock = engine.clock
        analyst.data = engine.data

        result = engine.run()
        assert result.total_trades == 0
        assert engine.broker.balance == pytest.approx(10000.0)
    finally:
        set_clock(None)
