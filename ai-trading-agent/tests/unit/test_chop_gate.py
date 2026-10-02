"""Structural CHOP gate — rule, live/backtest parity, analyst wiring.

Context (2026-08-16 filter diagnosis): the live trader abandons a whole tick
when ``SelfLearner.get_regime`` returns ``"chop"`` (``agent.py:900``), but the
backtest analyst had no equivalent. Every v5rr number therefore described a
strategy the live bot was not running. The rule now lives once in
``engine.regime`` and both paths call it; these tests are what keeps that true.

Three layers, mirroring ``test_regime.py``:
  1. ``structure_regime`` in isolation — the trend / chop / range branches.
  2. Parity — ``SelfLearner.get_regime`` must agree with the shared function on
     every combination, so a live-side edit cannot silently fork the backtest.
  3. ``ICTAnalyst`` wiring — gate off is a no-op (v5rr reproduction), gate on
     drops chop cycles and counts them.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pandas as pd
import pytest

from apps.api.src.agents.trader.brain.self_learner import SelfLearner
from apps.api.src.agents.trader.engine import analyst_ict as analyst_module
from apps.api.src.agents.trader.engine.__main__ import _parse_reduced_days
from apps.api.src.agents.trader.engine.analyst_ict import ICTAnalyst
from apps.api.src.agents.trader.engine.regime import (
    CHOP,
    RANGE,
    TREND,
    structure_regime,
    structure_regime_from_ict,
)
from apps.api.src.agents.trader.models.signals import ICTSignal
from apps.api.src.agents.trader.specialist.htf_bias import is_sniper_aligned

_FIXED_NOW = datetime(2026, 5, 14, 10, 30, tzinfo=timezone.utc)
_ALL_TFS = ("D1", "H4", "H1", "M30", "M15")


def _ict(trend: str = "sideways", bos: int = 0, choch: bool = False) -> ICTSignal:
    return ICTSignal(
        signal_type="none",
        structure={"trend": trend, "bos": bos, "choch": choch},
        order_blocks=[], pd_zone="neutral", confidence=0.0,
    )


# ── 1. The rule in isolation ─────────────────────────────────────────────────


class TestStructureRegime:
    def test_trend_needs_direction_and_two_bos(self) -> None:
        assert structure_regime("bullish", 2, False, "sideways") == TREND
        assert structure_regime("bearish", 3, False, "sideways") == TREND
        # One BOS is not enough — falls through to the range branch.
        assert structure_regime("bullish", 1, False, "sideways") == RANGE
        # Sideways H4 can never be "trend", however many BOS.
        assert structure_regime("sideways", 5, False, "sideways") == CHOP

    def test_chop_needs_both_sideways_and_no_choch(self) -> None:
        assert structure_regime("sideways", 0, False, "sideways") == CHOP
        # H1 showing direction breaks chop.
        assert structure_regime("sideways", 0, False, "bullish") == RANGE
        # An H4 CHoCH breaks chop even with both sideways — structure is moving.
        assert structure_regime("sideways", 0, True, "sideways") == RANGE

    def test_range_is_the_fallback(self) -> None:
        assert structure_regime("bullish", 0, False, "bullish") == RANGE
        assert structure_regime("bearish", 1, True, "sideways") == RANGE

    def test_from_ict_reads_structure_dict(self) -> None:
        assert structure_regime_from_ict(_ict("sideways"), _ict("sideways")) == CHOP
        assert structure_regime_from_ict(_ict("bullish", bos=2), _ict("sideways")) == TREND

    def test_from_ict_missing_timeframe_degrades_to_sideways(self) -> None:
        """A None TF must not raise — live uses ``.get(..., "sideways")``.

        Both-None lands on chop, which is the conservative read: no structure
        evidence at all is not a reason to trade.
        """
        assert structure_regime_from_ict(None, None) == CHOP
        assert structure_regime_from_ict(_ict("bullish", bos=2), None) == TREND
        assert structure_regime_from_ict(_ict("sideways"), None) == CHOP

    def test_from_ict_tolerates_none_bos(self) -> None:
        """``bos: None`` in a structure dict must read as 0, not crash."""
        sig = ICTSignal(
            signal_type="none",
            structure={"trend": "bullish", "bos": None, "choch": False},
            order_blocks=[], pd_zone="neutral", confidence=0.0,
        )
        assert structure_regime_from_ict(sig, _ict("sideways")) == RANGE


# ── 2. Live / backtest parity ────────────────────────────────────────────────


class TestLiveParity:
    def test_self_learner_delegates_for_every_combination(self) -> None:
        """The live call and the shared rule must never disagree.

        ``get_regime`` ignores ``self``, so it is invoked unbound — this test
        must not depend on SelfLearner's json/state setup.
        """
        trends = ("bullish", "bearish", "sideways")
        for h4_trend, h4_bos, h4_choch, h1_trend in itertools.product(
            trends, (0, 1, 2, 3), (False, True), trends
        ):
            h4 = _ict(h4_trend, bos=h4_bos, choch=h4_choch)
            h1 = _ict(h1_trend)
            live = SelfLearner.get_regime(None, h4, h1)  # type: ignore[arg-type]
            shared = structure_regime(h4_trend, h4_bos, h4_choch, h1_trend)
            assert live == shared, (
                f"live/backtest regime drift at H4={h4_trend} bos={h4_bos} "
                f"choch={h4_choch} H1={h1_trend}: {live} != {shared}"
            )

    def test_known_live_log_state_is_chop(self) -> None:
        """The state seen on 2026-08-07 (H4/H1 both sideways) → chop.

        675 of 951 live cycles that day were skipped on this branch; the
        backtest must now be able to reproduce that.
        """
        assert structure_regime_from_ict(_ict("sideways"), _ict("sideways")) == CHOP


# ── 3. ICTAnalyst wiring ─────────────────────────────────────────────────────


def _candles(n: int = 100, close: float = 2350.0) -> pd.DataFrame:
    return pd.DataFrame({
        "open": [close] * n, "high": [close + 1.0] * n,
        "low": [close - 1.0] * n, "close": [close] * n,
        "tick_volume": [100] * n,
    })


def _zone(label: str = "H1_OB") -> dict:
    return {
        "direction": "buy", "entry": 2350.0, "sl": 2345.0,
        "tp1": 2360.0, "tp2": 2365.0, "tp3": 2370.0,
        "label": label, "tf": "H1", "weight": 30.0,
    }


def _make_analyst(
    *,
    chop_gate: bool = False,
    h4_trend: str = "sideways",
    h1_trend: str = "sideways",
    d1_trend: str = "bullish",
    reduced_days: tuple[int, ...] | None = None,
    now: datetime = _FIXED_NOW,
) -> ICTAnalyst:
    """Analyst on mocks. D1 is bullish by default so a bias always exists — any
    empty result therefore comes from a gate, not from a missing bias."""
    clock = MagicMock()
    clock.now.return_value = now
    data = MagicMock()
    data.get_candles_at.side_effect = lambda s, tf, ts, count=500: _candles()

    analyst = ICTAnalyst(
        clock=clock, data=data, blocked_setups=set(), chop_gate=chop_gate,
        reduced_days=reduced_days,
    )
    trends = {"D1": d1_trend, "H4": h4_trend, "H1": h1_trend,
              "M30": "sideways", "M15": "sideways"}
    signals = {tf: _ict(trends[tf]) for tf in _ALL_TFS}
    analyst.ict = MagicMock()
    analyst.ict.analyze.side_effect = lambda df, tf: signals[tf]
    analyst.ict._atr.return_value = 0.5
    return analyst


class TestAnalystChopGate:
    def test_default_is_off(self) -> None:
        analyst = ICTAnalyst(clock=MagicMock(), data=MagicMock())
        assert analyst.chop_gate is False

    def test_off_emits_signals_in_chop_state(self, monkeypatch) -> None:
        """v5rr reproduction: with the gate off a chop cycle still trades."""
        monkeypatch.setattr(
            analyst_module.zone_finder, "find_all_ict_zones",
            MagicMock(return_value=[_zone()]),
        )
        analyst = _make_analyst(
            chop_gate=False, h4_trend="sideways", h1_trend="sideways",
        )
        assert len(analyst.analyze_market("XAUUSD", _ALL_TFS)) == 1
        assert analyst.cycles_chop_skipped == 0

    def test_on_blocks_chop_cycle(self, monkeypatch) -> None:
        finder = MagicMock(return_value=[_zone()])
        monkeypatch.setattr(analyst_module.zone_finder, "find_all_ict_zones", finder)
        analyst = _make_analyst(
            chop_gate=True, h4_trend="sideways", h1_trend="sideways",
        )
        assert analyst.analyze_market("XAUUSD", _ALL_TFS) == []
        # Short-circuits before zone discovery — the gate must be cheap.
        finder.assert_not_called()
        assert analyst.cycles_seen == 1
        assert analyst.cycles_chop_skipped == 1

    def test_on_allows_non_chop_cycle(self, monkeypatch) -> None:
        monkeypatch.setattr(
            analyst_module.zone_finder, "find_all_ict_zones",
            MagicMock(return_value=[_zone()]),
        )
        # H1 trending → range, not chop.
        analyst = _make_analyst(
            chop_gate=True, h4_trend="sideways", h1_trend="bullish",
        )
        assert len(analyst.analyze_market("XAUUSD", _ALL_TFS)) == 1
        assert analyst.cycles_seen == 1
        assert analyst.cycles_chop_skipped == 0

    def test_counters_accumulate_across_cycles(self, monkeypatch) -> None:
        monkeypatch.setattr(
            analyst_module.zone_finder, "find_all_ict_zones",
            MagicMock(return_value=[_zone()]),
        )
        analyst = _make_analyst(
            chop_gate=True, h4_trend="sideways", h1_trend="sideways",
        )
        for _ in range(3):
            analyst.analyze_market("XAUUSD", _ALL_TFS)
        assert analyst.cycles_seen == 3
        assert analyst.cycles_chop_skipped == 3


# ── 4. Reduced-day (Mon/Fri) rule ────────────────────────────────────────────

_MONDAY = datetime(2026, 5, 11, 10, 30, tzinfo=timezone.utc)
_WEDNESDAY = datetime(2026, 5, 13, 10, 30, tzinfo=timezone.utc)
_FRIDAY = datetime(2026, 5, 15, 10, 30, tzinfo=timezone.utc)


class TestSniperAligned:
    def test_requires_same_non_sideways_direction(self) -> None:
        assert is_sniper_aligned("bullish", "bullish") is True
        assert is_sniper_aligned("bearish", "bearish") is True
        assert is_sniper_aligned("bullish", "bearish") is False
        assert is_sniper_aligned("bullish", "sideways") is False
        assert is_sniper_aligned("sideways", "sideways") is False
        assert is_sniper_aligned("neutral", "neutral") is False


class TestAnalystReducedDays:
    def test_default_is_empty(self) -> None:
        assert ICTAnalyst(clock=MagicMock(), data=MagicMock()).reduced_days == ()

    def test_flow_cycle_dropped_on_reduced_day(self, monkeypatch) -> None:
        finder = MagicMock(return_value=[_zone()])
        monkeypatch.setattr(analyst_module.zone_finder, "find_all_ict_zones", finder)
        # D1 bullish + H4 sideways -> FLOW, and Monday is a reduced day.
        analyst = _make_analyst(
            reduced_days=(0, 4), d1_trend="bullish", h4_trend="sideways",
            now=_MONDAY,
        )
        assert analyst.analyze_market("XAUUSD", _ALL_TFS) == []
        finder.assert_not_called()
        assert analyst.cycles_reduced_day_skipped == 1

    def test_friday_is_also_reduced(self, monkeypatch) -> None:
        monkeypatch.setattr(
            analyst_module.zone_finder, "find_all_ict_zones",
            MagicMock(return_value=[_zone()]),
        )
        analyst = _make_analyst(
            reduced_days=(0, 4), d1_trend="bullish", h4_trend="sideways",
            now=_FRIDAY,
        )
        assert analyst.analyze_market("XAUUSD", _ALL_TFS) == []
        assert analyst.cycles_reduced_day_skipped == 1

    def test_non_reduced_day_unaffected(self, monkeypatch) -> None:
        monkeypatch.setattr(
            analyst_module.zone_finder, "find_all_ict_zones",
            MagicMock(return_value=[_zone()]),
        )
        analyst = _make_analyst(
            reduced_days=(0, 4), d1_trend="bullish", h4_trend="sideways",
            now=_WEDNESDAY,
        )
        assert len(analyst.analyze_market("XAUUSD", _ALL_TFS)) == 1
        assert analyst.cycles_reduced_day_skipped == 0

    def test_sniper_survives_the_reduced_day(self, monkeypatch) -> None:
        """D1+H4 aligned is exactly the case the live rule still allows."""
        monkeypatch.setattr(
            analyst_module.zone_finder, "find_all_ict_zones",
            MagicMock(return_value=[_zone()]),
        )
        analyst = _make_analyst(
            reduced_days=(0, 4), d1_trend="bullish", h4_trend="bullish",
            now=_MONDAY,
        )
        assert len(analyst.analyze_market("XAUUSD", _ALL_TFS)) == 1
        assert analyst.cycles_reduced_day_skipped == 0

    def test_off_by_default_keeps_monday_trading(self, monkeypatch) -> None:
        """v5rr reproduction: without the flag Monday trades as before."""
        monkeypatch.setattr(
            analyst_module.zone_finder, "find_all_ict_zones",
            MagicMock(return_value=[_zone()]),
        )
        analyst = _make_analyst(d1_trend="bullish", h4_trend="sideways", now=_MONDAY)
        assert len(analyst.analyze_market("XAUUSD", _ALL_TFS)) == 1
        assert analyst.cycles_reduced_day_skipped == 0


class TestParseReducedDays:
    def test_empty_is_off(self) -> None:
        assert _parse_reduced_days("") == ()
        assert _parse_reduced_days("   ") == ()

    def test_parses_and_normalises(self) -> None:
        assert _parse_reduced_days("0,4") == (0, 4)
        assert _parse_reduced_days(" 4 , 0 ") == (0, 4)   # sorted
        assert _parse_reduced_days("4,4,0") == (0, 4)     # deduped

    def test_rejects_garbage_instead_of_silently_disabling(self) -> None:
        """A typo must fail the run, not quietly turn the gate off mid-sweep."""
        for bad in ("mon", "0;4", "7", "-1"):
            with pytest.raises(ValueError):
                _parse_reduced_days(bad)
