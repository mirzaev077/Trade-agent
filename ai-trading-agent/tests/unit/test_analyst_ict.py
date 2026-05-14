"""F2-2.E: Unit tests for the MVP ICT analyst adapter.

Mocks the clock, HistoricalDataManager, and ICTAnalysis so each test can
pin down a single decision path. The Signal contract is exercised through
``ICTAnalyst.analyze_market`` — no real candles or indicators required.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pandas as pd
import pytest

from apps.api.src.agents.trader.engine.analyst_ict import ICTAnalyst
from apps.api.src.agents.trader.engine.signals import Signal
from apps.api.src.agents.trader.models.signals import ICTSignal


# ── Fixtures / helpers ───────────────────────────────────────────────────────


_FIXED_NOW = datetime(2026, 5, 14, 10, 30, tzinfo=timezone.utc)


def _df(n_rows: int, close: float = 2350.0) -> pd.DataFrame:
    """Build a tiny OHLC DataFrame for mocking ``get_candles_at`` returns."""
    return pd.DataFrame(
        {
            "open": [close] * n_rows,
            "high": [close + 1.0] * n_rows,
            "low": [close - 1.0] * n_rows,
            "close": [close] * n_rows,
            "tick_volume": [100] * n_rows,
        }
    )


def _make_ict_signal(
    *,
    trend: str = "sideways",
    pd_zone: str = "neutral",
    obs: list | None = None,
) -> ICTSignal:
    """Construct an ICTSignal pydantic instance with just the fields we use."""
    return ICTSignal(
        signal_type="none",
        structure={"trend": trend},
        order_blocks=obs or [],
        pd_zone=pd_zone,
        confidence=0.0,
    )


def _make_analyst(
    *,
    data_returns: dict[str, pd.DataFrame] | None = None,
    h4_signal: ICTSignal | None = None,
    h1_signal: ICTSignal | None = None,
    m15_close: float = 2350.0,
    now: datetime = _FIXED_NOW,
) -> ICTAnalyst:
    """Construct an ``ICTAnalyst`` wired to mocks.

    ``data_returns`` keyed by timeframe lets a test override what
    ``get_candles_at`` returns per TF. By default each TF returns a 100-row
    valid frame with ``m15_close`` as last close.
    """
    clock = MagicMock()
    clock.now.return_value = now

    full = _df(100, close=m15_close)
    defaults = {"H4": full, "H1": full, "M15": full}
    if data_returns:
        defaults.update(data_returns)

    def _fake_get_candles_at(symbol, tf, ts, count=500):
        return defaults[tf]

    data = MagicMock()
    data.get_candles_at.side_effect = _fake_get_candles_at

    analyst = ICTAnalyst(clock=clock, data=data, params=None)

    # Replace the real ICTAnalysis with a mock so we control the verdict
    analyst.ict = MagicMock()
    h4 = h4_signal if h4_signal is not None else _make_ict_signal()
    h1 = h1_signal if h1_signal is not None else _make_ict_signal()

    def _fake_analyze(df, tf):
        return h4 if tf == "H4" else h1

    analyst.ict.analyze.side_effect = _fake_analyze
    return analyst


# ── Tests ────────────────────────────────────────────────────────────────────


class TestInsufficientData:
    def test_returns_none_when_insufficient_candles(self) -> None:
        """If any TF returns fewer than min_candles_per_tf rows → None."""
        small = _df(10)
        analyst = _make_analyst(data_returns={"H1": small})

        result = analyst.analyze_market("XAUUSD", ["H4", "H1", "M15"])

        assert result is None

    def test_returns_none_when_data_not_loaded(self) -> None:
        """HistoricalDataManager raises KeyError → graceful None."""
        clock = MagicMock()
        clock.now.return_value = _FIXED_NOW
        data = MagicMock()
        data.get_candles_at.side_effect = KeyError("XAUUSD H4 not loaded")

        analyst = ICTAnalyst(clock=clock, data=data)

        assert analyst.analyze_market("XAUUSD", ["H4", "H1", "M15"]) is None


class TestHTFBiasGate:
    def test_returns_none_when_no_htf_bias(self) -> None:
        """H4 trend == 'sideways' → no signal."""
        analyst = _make_analyst(
            h4_signal=_make_ict_signal(trend="sideways"),
            h1_signal=_make_ict_signal(trend="sideways", pd_zone="discount"),
        )

        assert analyst.analyze_market("XAUUSD", ["H4", "H1", "M15"]) is None


class TestSignalEmission:
    """End-to-end happy paths through analyze_market."""

    def _bullish_ob(self, low: float = 2348.0, high: float = 2352.0, strength: float = 2.5) -> dict:
        return {
            "type": "bullish_ob",
            "high": high,
            "low": low,
            "mid": (high + low) / 2,
            "strength": strength,
            "mitigated": False,
        }

    def _bearish_ob(self, low: float = 2348.0, high: float = 2352.0, strength: float = 2.0) -> dict:
        return {
            "type": "bearish_ob",
            "high": high,
            "low": low,
            "mid": (high + low) / 2,
            "strength": strength,
            "mitigated": False,
        }

    def test_returns_signal_when_all_conditions_met(self) -> None:
        """Bullish H4, unmitigated H1 bullish OB, price in zone, discount PD."""
        ob = self._bullish_ob(low=2348.0, high=2352.0)
        analyst = _make_analyst(
            h4_signal=_make_ict_signal(trend="bullish"),
            h1_signal=_make_ict_signal(
                trend="bullish", pd_zone="discount", obs=[ob]
            ),
            m15_close=2350.0,  # Inside the OB
        )

        sig = analyst.analyze_market("XAUUSD", ["H4", "H1", "M15"])

        assert sig is not None
        assert isinstance(sig, Signal)
        assert sig.symbol == "XAUUSD"
        assert sig.direction == "buy"
        assert sig.entry_price == pytest.approx(2350.0)
        # SL = OB low (2348) - 5 pips * 0.10 = 2347.5
        assert sig.sl == pytest.approx(2347.5)
        # TP = entry + 2.5 * 2 = 2355.0
        assert sig.tp == pytest.approx(2355.0)
        assert sig.setup_type == "H1_OB"
        assert sig.mode == "sniper"

    def test_returns_none_when_price_outside_ob_zone(self) -> None:
        """Price below the OB → not yet at entry → None."""
        ob = self._bullish_ob(low=2348.0, high=2352.0)
        analyst = _make_analyst(
            h4_signal=_make_ict_signal(trend="bullish"),
            h1_signal=_make_ict_signal(
                trend="bullish", pd_zone="discount", obs=[ob]
            ),
            m15_close=2340.0,  # below OB
        )

        assert analyst.analyze_market("XAUUSD", ["H4", "H1", "M15"]) is None

    def test_returns_none_when_pd_zone_misaligned(self) -> None:
        """Buy bias but H1 pd_zone == 'premium' (counter-trend) → None."""
        ob = self._bullish_ob()
        analyst = _make_analyst(
            h4_signal=_make_ict_signal(trend="bullish"),
            h1_signal=_make_ict_signal(
                trend="bullish", pd_zone="premium", obs=[ob]
            ),
            m15_close=2350.0,
        )

        assert analyst.analyze_market("XAUUSD", ["H4", "H1", "M15"]) is None

    def test_returns_none_when_all_obs_mitigated(self) -> None:
        """If every matching OB is already mitigated → None."""
        ob = self._bullish_ob()
        ob["mitigated"] = True
        analyst = _make_analyst(
            h4_signal=_make_ict_signal(trend="bullish"),
            h1_signal=_make_ict_signal(
                trend="bullish", pd_zone="discount", obs=[ob]
            ),
            m15_close=2350.0,
        )

        assert analyst.analyze_market("XAUUSD", ["H4", "H1", "M15"]) is None

    def test_picks_strongest_ob_when_multiple_match(self) -> None:
        """Among unmitigated buy OBs, the highest-strength one wins."""
        weak = self._bullish_ob(low=2340.0, high=2342.0, strength=0.5)
        strong = self._bullish_ob(low=2348.0, high=2352.0, strength=2.8)
        analyst = _make_analyst(
            h4_signal=_make_ict_signal(trend="bullish"),
            h1_signal=_make_ict_signal(
                trend="bullish", pd_zone="discount", obs=[weak, strong]
            ),
            m15_close=2350.0,  # Inside strong, outside weak
        )

        sig = analyst.analyze_market("XAUUSD", ["H4", "H1", "M15"])

        assert sig is not None
        # Strong OB midpoint = 2350
        assert sig.entry_price == pytest.approx(2350.0)

    def test_bearish_path_returns_sell_signal(self) -> None:
        """Bearish H4 + bearish unmitigated OB + premium PD → sell signal."""
        ob = self._bearish_ob(low=2348.0, high=2352.0, strength=2.0)
        analyst = _make_analyst(
            h4_signal=_make_ict_signal(trend="bearish"),
            h1_signal=_make_ict_signal(
                trend="bearish", pd_zone="premium", obs=[ob]
            ),
            m15_close=2350.0,
        )

        sig = analyst.analyze_market("XAUUSD", ["H4", "H1", "M15"])

        assert sig is not None
        assert sig.direction == "sell"
        assert sig.entry_price == pytest.approx(2350.0)
        # SL = OB high (2352) + 5 pips * 0.10 = 2352.5
        assert sig.sl == pytest.approx(2352.5)
        # TP = entry - 2.5 * 2 = 2345.0
        assert sig.tp == pytest.approx(2345.0)


class TestSignalContract:
    """Lock down Signal fields the engine + journal rely on."""

    def _setup(self, now: datetime = _FIXED_NOW) -> Signal:
        ob = {
            "type": "bullish_ob",
            "high": 2352.0,
            "low": 2348.0,
            "mid": 2350.0,
            "strength": 2.5,
            "mitigated": False,
        }
        analyst = _make_analyst(
            h4_signal=_make_ict_signal(trend="bullish"),
            h1_signal=_make_ict_signal(
                trend="bullish", pd_zone="discount", obs=[ob]
            ),
            m15_close=2350.0,
            now=now,
        )
        sig = analyst.analyze_market("XAUUSD", ["H4", "H1", "M15"])
        assert sig is not None
        return sig

    def test_signal_has_correct_rr(self) -> None:
        """``Signal.rr()`` should equal the analyst's configured rr_ratio."""
        sig = self._setup()
        assert sig.rr() == pytest.approx(2.0)

    def test_signal_timestamp_uses_clock(self) -> None:
        """The Signal stamp must be the value returned by ``clock.now()``."""
        sig = self._setup(now=_FIXED_NOW)
        assert sig.timestamp == _FIXED_NOW

    def test_signal_session_and_day_match_clock(self) -> None:
        """10:30 UTC Thursday → 'london' session, 'thu' day."""
        # 2026-05-14 is a Thursday
        sig = self._setup(
            now=datetime(2026, 5, 14, 10, 30, tzinfo=timezone.utc)
        )
        assert sig.session == "london"
        assert sig.day_of_week == "thu"

    def test_naive_clock_coerced_to_utc(self) -> None:
        """If the clock returns a naive datetime, Signal must still validate."""
        naive = datetime(2026, 5, 14, 10, 30)  # no tz
        sig = self._setup(now=naive)
        assert sig.timestamp.tzinfo is not None
        assert sig.timestamp == naive.replace(tzinfo=timezone.utc)
