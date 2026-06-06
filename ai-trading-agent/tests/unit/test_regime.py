"""F4 high-volatility regime gate — unit + analyst-integration tests.

Two layers, mirroring the blocked_setups pattern (test_analyst_ict.py):
  1. ``RegimeGate`` in isolation — ATR%% maths, enable/disable, should_block.
  2. ``ICTAnalyst`` wiring — a high-vol M15 window drops the gated setups while
     keeping the rest; low-vol / disabled gate keeps everything.

The gate computes its OWN ATR%% from the M15 candles (it does NOT reuse the
analyst's mocked ``_atr``), so the integration tests drive volatility through
the candle high/low range, not through the ATR mock.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from apps.api.src.agents.trader.engine import analyst_ict as analyst_module
from apps.api.src.agents.trader.engine.analyst_ict import ICTAnalyst
from apps.api.src.agents.trader.engine.regime import (
    DEFAULT_REGIME_BLOCKED_SETUPS,
    HIGH_VOL,
    NORMAL,
    RegimeGate,
)
from apps.api.src.agents.trader.models.signals import ICTSignal

_FIXED_NOW = datetime(2026, 5, 14, 10, 30, tzinfo=timezone.utc)
_ALL_TFS = ("D1", "H4", "H1", "M30", "M15")

# Real M15 parquet used to calibrate the gate (data/historical/). Present
# locally; absent in a bare CI checkout → the parity test skips itself.
_PARQUET = Path(__file__).resolve().parents[2] / "data" / "historical" / "XAUUSD_M15.parquet"


def _candles(n: int = 100, close: float = 2350.0, half_range: float = 1.0) -> pd.DataFrame:
    """OHLC where every bar spans ``2*half_range`` around a flat ``close``.

    True range per bar == 2*half_range, so ATR == 2*half_range and
    ATR%% == 2*half_range / close * 100 — exactly computable for assertions.
    half_range=1.0 @2350 -> 0.085%% (low vol); half_range=5.0 -> 0.426%% (high).
    """
    return pd.DataFrame({
        "open":  [close] * n,
        "high":  [close + half_range] * n,
        "low":   [close - half_range] * n,
        "close": [close] * n,
        "tick_volume": [100] * n,
    })


def _two_regime_candles(
    close: float = 2350.0,
    calm_n: int = 90, calm_hr: float = 1.0,
    vol_n: int = 10, vol_hr: float = 10.0,
) -> pd.DataFrame:
    """``calm_n`` low-range bars followed by ``vol_n`` high-range bars.

    Lets a test distinguish ATR periods: a 14-bar window straddles fewer calm
    bars than a 20-bar window, so the trailing ATR differs by period — a flat
    series (``_candles``) cannot prove the period is actually used.
    """
    hrs = [calm_hr] * calm_n + [vol_hr] * vol_n
    return pd.DataFrame({
        "open":  [close] * len(hrs),
        "high":  [close + hr for hr in hrs],
        "low":   [close - hr for hr in hrs],
        "close": [close] * len(hrs),
        "tick_volume": [100] * len(hrs),
    })


# ── 1. RegimeGate in isolation ───────────────────────────────────────────────


class TestRegimeGateConfig:
    def test_default_is_disabled(self) -> None:
        """A bare gate (threshold 0) is a no-op."""
        gate = RegimeGate()
        assert gate.atr_threshold == 0.0
        assert gate.atr_period == 14
        assert gate.enabled is False
        assert gate.blocked_setups == DEFAULT_REGIME_BLOCKED_SETUPS

    def test_enabled_requires_positive_threshold_and_blocks(self) -> None:
        assert RegimeGate(atr_threshold=0.18).enabled is True
        assert RegimeGate(atr_threshold=0.0).enabled is False
        assert RegimeGate(atr_threshold=-1.0).enabled is False
        # threshold > 0 but nothing to block → still a no-op
        assert RegimeGate(atr_threshold=0.18, blocked_setups=set()).enabled is False

    def test_blocked_setups_normalised_to_frozenset(self) -> None:
        gate = RegimeGate(atr_threshold=0.18, blocked_setups=["M15_OB", "M15_BB"])
        assert isinstance(gate.blocked_setups, frozenset)
        assert gate.blocked_setups == frozenset({"M15_OB", "M15_BB"})

    def test_invalid_period_raises(self) -> None:
        with pytest.raises(ValueError):
            RegimeGate(atr_threshold=0.18, atr_period=0)
        with pytest.raises(ValueError):
            RegimeGate(atr_threshold=0.18, atr_period=-5)


class TestAtrPct:
    def test_known_value(self) -> None:
        gate = RegimeGate(atr_threshold=0.18)
        # half_range=5 @2350 -> TR=10 -> ATR%=10/2350*100
        assert gate.atr_pct(_candles(close=2350.0, half_range=5.0)) == pytest.approx(
            10.0 / 2350.0 * 100.0
        )

    def test_low_vs_high(self) -> None:
        gate = RegimeGate(atr_threshold=0.18)
        low = gate.atr_pct(_candles(half_range=1.0))
        high = gate.atr_pct(_candles(half_range=5.0))
        assert low < 0.18 < high

    def test_normalised_by_price(self) -> None:
        """Same half_range at 2x price -> half the ATR%% (gold doubled in window)."""
        gate = RegimeGate(atr_threshold=0.18)
        a = gate.atr_pct(_candles(close=2000.0, half_range=4.0))
        b = gate.atr_pct(_candles(close=4000.0, half_range=4.0))
        assert a == pytest.approx(2 * b)

    def test_fails_open_on_empty(self) -> None:
        gate = RegimeGate(atr_threshold=0.18)
        assert gate.atr_pct(pd.DataFrame({"high": [], "low": [], "close": []})) == 0.0
        assert gate.atr_pct(None) == 0.0  # type: ignore[arg-type]

    def test_first_bar_true_range_uses_high_low(self) -> None:
        """First bar's shifted-close terms are NaN → TR must fall back to
        high-low (relies on pandas skipna=True). A single bar must not break."""
        gate = RegimeGate(atr_threshold=0.18)
        one = _candles(n=1, close=2350.0, half_range=5.0)
        assert gate.atr_pct(one) == pytest.approx(10.0 / 2350.0 * 100.0)

    def test_period_is_actually_used(self) -> None:
        """On a two-regime series the trailing ATR depends on the window, so a
        14-bar and 20-bar gate MUST disagree — proves atr_period is honoured
        (a mutant that hardcodes the window would fail this)."""
        c = _two_regime_candles(close=2350.0, calm_n=90, calm_hr=1.0,
                                 vol_n=10, vol_hr=10.0)
        g14 = RegimeGate(atr_threshold=0.18, atr_period=14).atr_pct(c)
        g20 = RegimeGate(atr_threshold=0.18, atr_period=20).atr_pct(c)
        # last 14 bars: 10*TR(20) + 4*TR(2); last 20 bars: 10*TR(20) + 10*TR(2)
        assert g14 == pytest.approx((10 * 20 + 4 * 2) / 14 / 2350.0 * 100.0)
        assert g20 == pytest.approx((10 * 20 + 10 * 2) / 20 / 2350.0 * 100.0)
        assert g14 > g20

    @pytest.mark.skipif(not _PARQUET.exists(), reason="real M15 parquet absent")
    def test_matches_reference_formula_on_real_data(self) -> None:
        """The gate's scalar ATR%% equals the SMA-14 reference (the formula
        atr_diagnosis.py calibrated 0.18 against) on a real 500-bar M15 window.
        Guards the calibration↔runtime contract against any off-by-one drift."""
        df = pd.read_parquet(_PARQUET).tail(500).reset_index(drop=True)
        h = df["high"].astype(float)
        l = df["low"].astype(float)
        c = df["close"].astype(float)
        tr = pd.concat(
            [h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1
        ).max(axis=1)
        ref = (tr.rolling(14, min_periods=1).mean() / c * 100.0).iloc[-1]
        assert RegimeGate(atr_threshold=0.18, atr_period=14).atr_pct(df) == pytest.approx(ref)


class TestRegimeClassify:
    def test_regime_label(self) -> None:
        gate = RegimeGate(atr_threshold=0.18)
        assert gate.regime(0.42) == HIGH_VOL
        assert gate.regime(0.10) == NORMAL
        assert gate.regime(0.18) == NORMAL  # strict >

    def test_disabled_gate_is_always_normal(self) -> None:
        assert RegimeGate(atr_threshold=0.0).regime(99.0) == NORMAL


class TestShouldBlock:
    def test_blocks_gated_setup_in_high_vol(self) -> None:
        gate = RegimeGate(atr_threshold=0.18)
        assert gate.should_block("M15_OB", 0.42) is True
        assert gate.should_block("M15_CISD", 0.42) is True

    def test_keeps_non_gated_setup(self) -> None:
        gate = RegimeGate(atr_threshold=0.18)
        assert gate.should_block("H1_OB", 0.42) is False
        assert gate.should_block("M15_FVG", 0.42) is False

    def test_keeps_everything_in_normal_vol(self) -> None:
        gate = RegimeGate(atr_threshold=0.18)
        assert gate.should_block("M15_OB", 0.10) is False
        assert gate.should_block("M15_OB", 0.18) is False  # strict >

    def test_disabled_gate_blocks_nothing(self) -> None:
        gate = RegimeGate(atr_threshold=0.0)
        assert gate.should_block("M15_OB", 99.0) is False

    def test_custom_blocklist(self) -> None:
        gate = RegimeGate(atr_threshold=0.18, blocked_setups={"H1_OB"})
        assert gate.should_block("H1_OB", 0.42) is True
        assert gate.should_block("M15_OB", 0.42) is False


# ── 2. ICTAnalyst integration ────────────────────────────────────────────────


def _zone(*, direction: str = "buy", entry: float = 2350.0, sl: float = 2345.0,
          tp1: float = 2360.0, tp2: float = 2365.0, tp3: float = 2370.0,
          label: str = "H1_OB", tf: str = "H1", weight: float = 30.0) -> dict:
    return {
        "direction": direction, "entry": entry, "sl": sl,
        "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "label": label, "tf": tf, "weight": weight,
    }


def _patch_zone_finder(monkeypatch, zones: list[dict]) -> MagicMock:
    mock = MagicMock(return_value=zones)
    monkeypatch.setattr(analyst_module.zone_finder, "find_all_ict_zones", mock)
    return mock


def _make_analyst(regime: RegimeGate | None, m15_candles: pd.DataFrame) -> ICTAnalyst:
    """Analyst wired to mocks. ``data.get_candles_at`` returns ``m15_candles``
    for every TF (the gate only reads M15). blocked_setups is empty so any
    filtering observed comes solely from the regime gate."""
    clock = MagicMock()
    clock.now.return_value = _FIXED_NOW
    data = MagicMock()
    data.get_candles_at.side_effect = lambda s, tf, ts, count=500: m15_candles

    analyst = ICTAnalyst(clock=clock, data=data, blocked_setups=set(), regime=regime)

    trends = {"D1": "bullish", "H4": "sideways", "H1": "sideways",
              "M30": "sideways", "M15": "sideways"}
    ict_signals = {
        tf: ICTSignal(signal_type="none", structure={"trend": trends[tf]},
                      order_blocks=[], pd_zone="neutral", confidence=0.0)
        for tf in _ALL_TFS
    }
    analyst.ict = MagicMock()
    analyst.ict.analyze.side_effect = lambda df, tf: ict_signals[tf]
    analyst.ict._atr.return_value = 0.5  # analyst's atr_map; gate ignores it
    return analyst


_MIXED_ZONES = [
    _zone(label="M15_OB", tf="M15", entry=2350.0),    # gated in high vol
    _zone(label="M15_CISD", tf="M15", entry=2351.0),  # gated in high vol
    _zone(label="H1_OB", tf="H1", entry=2352.0),      # never gated
    _zone(label="M15_FVG", tf="M15", entry=2353.0),   # not in default blocklist
]


class TestAnalystRegimeIntegration:
    def test_high_vol_blocks_core_setups(self, monkeypatch) -> None:
        _patch_zone_finder(monkeypatch, _MIXED_ZONES)
        analyst = _make_analyst(
            RegimeGate(atr_threshold=0.18),
            _candles(close=2350.0, half_range=5.0),  # ATR% ~0.43 > 0.18
        )
        labels = {s.setup_type for s in analyst.analyze_market("XAUUSD", _ALL_TFS)}
        assert "M15_OB" not in labels
        assert "M15_CISD" not in labels
        assert {"H1_OB", "M15_FVG"} <= labels  # non-gated survive

    def test_low_vol_keeps_everything(self, monkeypatch) -> None:
        _patch_zone_finder(monkeypatch, _MIXED_ZONES)
        analyst = _make_analyst(
            RegimeGate(atr_threshold=0.18),
            _candles(close=2350.0, half_range=1.0),  # ATR% ~0.085 < 0.18
        )
        labels = {s.setup_type for s in analyst.analyze_market("XAUUSD", _ALL_TFS)}
        assert {"M15_OB", "M15_CISD", "H1_OB", "M15_FVG"} <= labels

    def test_disabled_gate_keeps_everything_even_in_high_vol(self, monkeypatch) -> None:
        _patch_zone_finder(monkeypatch, _MIXED_ZONES)
        analyst = _make_analyst(None, _candles(close=2350.0, half_range=5.0))
        labels = {s.setup_type for s in analyst.analyze_market("XAUUSD", _ALL_TFS)}
        assert "M15_OB" in labels  # no gate → high vol irrelevant

    def test_gate_and_blocked_setups_compose(self, monkeypatch) -> None:
        """blocked_setups and the regime gate both filter; union is dropped."""
        _patch_zone_finder(monkeypatch, _MIXED_ZONES)
        clock = MagicMock(); clock.now.return_value = _FIXED_NOW
        data = MagicMock()
        hv = _candles(close=2350.0, half_range=5.0)
        data.get_candles_at.side_effect = lambda s, tf, ts, count=500: hv
        analyst = ICTAnalyst(
            clock=clock, data=data,
            blocked_setups={"M15_FVG"},               # static block
            regime=RegimeGate(atr_threshold=0.18),    # gates M15_OB/CISD in high vol
        )
        ict_signals = {tf: ICTSignal(signal_type="none",
                                     structure={"trend": "bullish" if tf == "D1" else "sideways"},
                                     order_blocks=[], pd_zone="neutral", confidence=0.0)
                       for tf in _ALL_TFS}
        analyst.ict = MagicMock()
        analyst.ict.analyze.side_effect = lambda df, tf: ict_signals[tf]
        analyst.ict._atr.return_value = 0.5
        labels = {s.setup_type for s in analyst.analyze_market("XAUUSD", _ALL_TFS)}
        assert labels == {"H1_OB"}  # M15_FVG static-blocked; M15_OB/CISD gated
