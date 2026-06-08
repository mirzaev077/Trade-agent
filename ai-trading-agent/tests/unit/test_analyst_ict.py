"""F2-3.1 Variant C: Unit tests for the multi-TF zone-based ICT analyst.

These tests pin down the analyst's *contract* with the engine (returns
``list[Signal]``, dedup semantics, bias resolution) without exercising every
zone-helper rule — those live in ``test_zone_finder.py``. Here we use a fake
``zone_finder.find_all_ict_zones`` so the analyst's wiring (TF loading, ATR
extraction, bias + confluence, signal conversion) is the unit under test.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pandas as pd
import pytest

from apps.api.src.agents.trader.engine import analyst_ict as analyst_module
from apps.api.src.agents.trader.engine.analyst_ict import ICTAnalyst
from apps.api.src.agents.trader.engine.signals import Signal
from apps.api.src.agents.trader.models.signals import ICTSignal


# ── Fixtures / helpers ───────────────────────────────────────────────────────


_FIXED_NOW = datetime(2026, 5, 14, 10, 30, tzinfo=timezone.utc)
_ALL_TFS = ("D1", "H4", "H1", "M30", "M15")


def _df(n_rows: int = 100, close: float = 2350.0) -> pd.DataFrame:
    return pd.DataFrame({
        "open":  [close] * n_rows,
        "high":  [close + 1.0] * n_rows,
        "low":   [close - 1.0] * n_rows,
        "close": [close] * n_rows,
        "tick_volume": [100] * n_rows,
    })


def _make_ict_signal(
    *, trend: str = "sideways", pd_zone: str = "neutral",
    obs: list | None = None,
) -> ICTSignal:
    return ICTSignal(
        signal_type="none",
        structure={"trend": trend},
        order_blocks=obs or [],
        pd_zone=pd_zone,
        confidence=0.0,
    )


def _make_analyst(
    *,
    trends: dict[str, str] | None = None,
    m15_close: float = 2350.0,
    now: datetime = _FIXED_NOW,
    atr: float = 0.5,
) -> ICTAnalyst:
    """Build an analyst wired to mocks. `trends` overrides per-TF trend."""
    clock = MagicMock()
    clock.now.return_value = now

    full = _df(100, close=m15_close)

    def _fake_get_candles_at(symbol, tf, ts, count=500):
        return full

    data = MagicMock()
    data.get_candles_at.side_effect = _fake_get_candles_at

    analyst = ICTAnalyst(clock=clock, data=data, params=None)

    trends = trends or {tf: "sideways" for tf in _ALL_TFS}
    ict_signals = {tf: _make_ict_signal(trend=trends.get(tf, "sideways")) for tf in _ALL_TFS}

    analyst.ict = MagicMock()
    analyst.ict.analyze.side_effect = lambda df, tf: ict_signals[tf]
    analyst.ict._atr.return_value = atr

    return analyst


def _patch_zone_finder(monkeypatch, zones: list[dict]) -> MagicMock:
    """Replace ``zone_finder.find_all_ict_zones`` with a mock returning ``zones``."""
    mock = MagicMock(return_value=zones)
    monkeypatch.setattr(analyst_module.zone_finder, "find_all_ict_zones", mock)
    return mock


def _zone(
    *, direction: str = "buy", entry: float = 2350.0, sl: float = 2345.0,
    tp1: float = 2360.0, tp2: float = 2365.0, tp3: float = 2370.0,
    label: str = "H4_OB", tf: str = "H4", weight: float = 30.0,
) -> dict:
    return {
        "direction": direction, "entry": entry, "sl": sl,
        "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "zone_lo": entry - 2, "zone_hi": entry + 2,
        "label": label, "weight": weight, "tf": tf,
        "htf_conf": 2, "silver_bullet": False,
    }


# ── Tests ────────────────────────────────────────────────────────────────────


class TestInsufficientData:
    def test_returns_empty_when_short_tf(self) -> None:
        analyst = _make_analyst()
        analyst.data.get_candles_at.side_effect = (
            lambda symbol, tf, ts, count=500:
                _df(10) if tf == "M30" else _df(100)
        )
        assert analyst.analyze_market("XAUUSD", _ALL_TFS) == []

    def test_returns_empty_when_data_not_loaded(self) -> None:
        clock = MagicMock()
        clock.now.return_value = _FIXED_NOW
        data = MagicMock()
        data.get_candles_at.side_effect = KeyError("XAUUSD D1 not loaded")
        analyst = ICTAnalyst(clock=clock, data=data)
        assert analyst.analyze_market("XAUUSD", _ALL_TFS) == []


class TestBiasResolution:
    def test_d1_bullish_picks_buy(self, monkeypatch) -> None:
        zone = _zone(direction="buy")
        mock = _patch_zone_finder(monkeypatch, [zone])
        analyst = _make_analyst(trends={"D1": "bullish", "H4": "sideways",
                                        "H1": "sideways", "M30": "sideways", "M15": "sideways"})
        analyst.analyze_market("XAUUSD", _ALL_TFS)
        assert mock.call_args.kwargs["want_dir"] == "buy"

    def test_h4_fallback_when_d1_sideways(self, monkeypatch) -> None:
        zone = _zone(direction="sell")
        mock = _patch_zone_finder(monkeypatch, [zone])
        analyst = _make_analyst(trends={"D1": "sideways", "H4": "bearish",
                                        "H1": "sideways", "M30": "sideways", "M15": "sideways"})
        analyst.analyze_market("XAUUSD", _ALL_TFS)
        assert mock.call_args.kwargs["want_dir"] == "sell"

    def test_returns_empty_when_no_bias(self, monkeypatch) -> None:
        mock = _patch_zone_finder(monkeypatch, [])
        analyst = _make_analyst()  # all sideways
        result = analyst.analyze_market("XAUUSD", _ALL_TFS)
        assert result == []
        mock.assert_not_called()  # bias gate fires before zone_finder


class TestHTFConfluence:
    def test_counts_aligned_higher_tfs(self, monkeypatch) -> None:
        """D1+H4+H1 all bullish → confluence = 3; M30 sideways doesn't add."""
        mock = _patch_zone_finder(monkeypatch, [_zone()])
        analyst = _make_analyst(trends={
            "D1": "bullish", "H4": "bullish", "H1": "bullish",
            "M30": "sideways", "M15": "bullish",
        })
        analyst.analyze_market("XAUUSD", _ALL_TFS)
        assert mock.call_args.kwargs["htf_conf"] == 3


class TestSignalEmission:
    def test_returns_signal_per_zone(self, monkeypatch) -> None:
        zones = [
            _zone(label="H4_OB", tf="H4", entry=2350.0, weight=40.0),
            _zone(label="H1_FVG", tf="H1", entry=2348.0, weight=25.0),
        ]
        _patch_zone_finder(monkeypatch, zones)
        analyst = _make_analyst(trends={"D1": "bullish", "H4": "bullish",
                                        "H1": "sideways", "M30": "sideways", "M15": "sideways"})
        sigs = analyst.analyze_market("XAUUSD", _ALL_TFS)
        assert isinstance(sigs, list)
        assert len(sigs) == 2
        for sig in sigs:
            assert isinstance(sig, Signal)
            assert sig.is_limit is True
            assert sig.symbol == "XAUUSD"
        assert {s.setup_type for s in sigs} == {"H4_OB", "H1_FVG"}

    def test_confluence_score_normalized(self, monkeypatch) -> None:
        # weight=80 / max=100 → 0.80
        _patch_zone_finder(monkeypatch, [_zone(weight=80.0)])
        analyst = _make_analyst(trends={"D1": "bullish", "H4": "sideways",
                                        "H1": "sideways", "M30": "sideways", "M15": "sideways"})
        sigs = analyst.analyze_market("XAUUSD", _ALL_TFS)
        assert sigs[0].confluence_score == pytest.approx(0.80)

    def test_confluence_score_clamped_to_one(self, monkeypatch) -> None:
        # Weight > confluence_max_weight clamps to 1.0
        _patch_zone_finder(monkeypatch, [_zone(weight=250.0)])
        analyst = _make_analyst(trends={"D1": "bullish", "H4": "sideways",
                                        "H1": "sideways", "M30": "sideways", "M15": "sideways"})
        sigs = analyst.analyze_market("XAUUSD", _ALL_TFS)
        assert sigs[0].confluence_score == 1.0

    def test_max_signals_per_cycle_caps_output(self, monkeypatch) -> None:
        zones = [_zone(label=f"Z{i}", entry=2300.0 + i) for i in range(20)]
        _patch_zone_finder(monkeypatch, zones)
        analyst = _make_analyst(trends={"D1": "bullish", "H4": "sideways",
                                        "H1": "sideways", "M30": "sideways", "M15": "sideways"})
        analyst.max_signals_per_cycle = 3
        sigs = analyst.analyze_market("XAUUSD", _ALL_TFS)
        assert len(sigs) == 3


class TestDedup:
    def test_same_zone_emits_once(self, monkeypatch) -> None:
        _patch_zone_finder(monkeypatch, [_zone()])
        analyst = _make_analyst(trends={"D1": "bullish", "H4": "sideways",
                                        "H1": "sideways", "M30": "sideways", "M15": "sideways"})
        first = analyst.analyze_market("XAUUSD", _ALL_TFS)
        second = analyst.analyze_market("XAUUSD", _ALL_TFS)
        assert len(first) == 1
        assert second == []

    def test_different_label_or_entry_emits(self, monkeypatch) -> None:
        analyst = _make_analyst(trends={"D1": "bullish", "H4": "sideways",
                                        "H1": "sideways", "M30": "sideways", "M15": "sideways"})

        _patch_zone_finder(monkeypatch, [_zone(label="H4_OB", entry=2350.0)])
        first = analyst.analyze_market("XAUUSD", _ALL_TFS)

        _patch_zone_finder(monkeypatch, [_zone(label="H1_FVG", entry=2350.0)])
        second = analyst.analyze_market("XAUUSD", _ALL_TFS)

        _patch_zone_finder(monkeypatch, [_zone(label="H4_OB", entry=2348.0)])
        third = analyst.analyze_market("XAUUSD", _ALL_TFS)

        assert len(first) == 1
        assert len(second) == 1  # different label
        assert len(third) == 1   # different entry


class TestSetupBlocking:
    """F2-3.1 Variant C: setups in `blocked_setups` are dropped before emit."""

    def test_h1_ob_in_default_blocklist(self) -> None:
        # 24-month per-trade diagnosis: H1_OB is a chronic, non-vol-driven loser.
        assert "H1_OB" in ICTAnalyst.DEFAULT_BLOCKED_SETUPS

    def test_default_blocks_ote_dr_eq_and_h1_ob(self, monkeypatch) -> None:
        zones = [
            _zone(label="M15_OTE", entry=2350.0),     # blocked by default
            _zone(label="M15_DR_Eq", entry=2351.0),   # blocked by default
            _zone(label="H1_OB", entry=2352.0),       # blocked by default (chronic loser)
            _zone(label="H4_OB", entry=2353.0),       # allowed
        ]
        _patch_zone_finder(monkeypatch, zones)
        analyst = _make_analyst(trends={"D1": "bullish", "H4": "sideways",
                                        "H1": "sideways", "M30": "sideways", "M15": "sideways"})
        sigs = analyst.analyze_market("XAUUSD", _ALL_TFS)
        assert len(sigs) == 1
        assert sigs[0].setup_type == "H4_OB"

    def test_custom_blocked_setups_override(self, monkeypatch) -> None:
        zones = [
            _zone(label="M15_OTE", entry=2350.0),
            _zone(label="H4_OB", entry=2351.0),
        ]
        _patch_zone_finder(monkeypatch, zones)
        analyst = ICTAnalyst(
            clock=MagicMock(now=MagicMock(return_value=_FIXED_NOW)),
            data=MagicMock(),
            blocked_setups={"H4_OB"},  # override defaults
        )
        # Mock the data + ict
        full = _df(100)
        analyst.data.get_candles_at.side_effect = lambda s, tf, ts, count=500: full
        ict_signals = {tf: _make_ict_signal(trend="bullish") for tf in _ALL_TFS}
        analyst.ict = MagicMock()
        analyst.ict.analyze.side_effect = lambda df, tf: ict_signals[tf]
        analyst.ict._atr.return_value = 0.5

        sigs = analyst.analyze_market("XAUUSD", _ALL_TFS)
        # H4_OB blocked, M15_OTE allowed (defaults overridden)
        assert len(sigs) == 1
        assert sigs[0].setup_type == "M15_OTE"

    def test_empty_blocklist_allows_all(self, monkeypatch) -> None:
        zones = [_zone(label="M15_OTE", entry=2350.0)]
        _patch_zone_finder(monkeypatch, zones)
        analyst = ICTAnalyst(
            clock=MagicMock(now=MagicMock(return_value=_FIXED_NOW)),
            data=MagicMock(),
            blocked_setups=set(),
        )
        full = _df(100)
        analyst.data.get_candles_at.side_effect = lambda s, tf, ts, count=500: full
        ict_signals = {tf: _make_ict_signal(trend="bullish") for tf in _ALL_TFS}
        analyst.ict = MagicMock()
        analyst.ict.analyze.side_effect = lambda df, tf: ict_signals[tf]
        analyst.ict._atr.return_value = 0.5

        sigs = analyst.analyze_market("XAUUSD", _ALL_TFS)
        assert len(sigs) == 1
        assert sigs[0].setup_type == "M15_OTE"


class TestSignalContract:
    def _emit_one(self, monkeypatch, *, now=_FIXED_NOW) -> Signal:
        _patch_zone_finder(monkeypatch, [_zone(entry=2350.0, sl=2347.5,
                                                tp1=2355.0, weight=30.0)])
        analyst = _make_analyst(now=now, trends={"D1": "bullish", "H4": "sideways",
                                                  "H1": "sideways", "M30": "sideways", "M15": "sideways"})
        sigs = analyst.analyze_market("XAUUSD", _ALL_TFS)
        assert len(sigs) == 1
        return sigs[0]

    def test_signal_fields_match_zone(self, monkeypatch) -> None:
        sig = self._emit_one(monkeypatch)
        assert sig.entry_price == pytest.approx(2350.0)
        assert sig.sl == pytest.approx(2347.5)
        assert sig.tp == pytest.approx(2355.0)
        assert sig.is_limit is True
        assert sig.mode == "sniper"

    def test_signal_metadata_carries_zone_details(self, monkeypatch) -> None:
        sig = self._emit_one(monkeypatch)
        assert sig.metadata["tf"] == "H4"
        assert sig.metadata["weight"] == 30.0
        assert "tp1" in sig.metadata and "tp2" in sig.metadata and "tp3" in sig.metadata

    def test_signal_session_and_day_match_clock(self, monkeypatch) -> None:
        # 2026-05-14 10:30 Thursday → london session
        sig = self._emit_one(monkeypatch, now=_FIXED_NOW)
        assert sig.session == "london"
        assert sig.day_of_week == "thu"

    def test_naive_clock_coerced_to_utc(self, monkeypatch) -> None:
        naive = datetime(2026, 5, 14, 10, 30)
        sig = self._emit_one(monkeypatch, now=naive)
        assert sig.timestamp.tzinfo is not None
