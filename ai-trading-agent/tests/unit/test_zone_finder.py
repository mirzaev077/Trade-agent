"""F2-3.1 Variant C: Unit tests for ``engine/zone_finder.py``.

Targets:
  * Pure helpers — math correctness (rr_targets, swing_tp_targets, structural_sl,
    volume_boost) and the per-zone shape constructors (ob_zone, fvg_zone,
    liq_zone, liq_void_zone, unswept_liq_zone, dealing_eq_zone, bos_sr_zone,
    ote_fib_zones).
  * find_all_ict_zones — dispatch + dedup + cap behavior.

Tests use minimal hand-built dicts; ICT analysis is NOT invoked. The point is
to lock down the math contract this module promises to the analyst.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from apps.api.src.agents.trader.engine import zone_finder
from apps.api.src.agents.trader.engine.zone_finder import (
    PIP, SL_MAX_PIPS, SL_MIN_PIPS, TP_MAX_PIPS,
    bos_sr_zone, dealing_eq_zone, fvg_zone, find_all_ict_zones,
    liq_void_zone, liq_zone, ob_zone, ote_fib_zones, rr_targets,
    structural_sl, swing_tp_targets, unswept_liq_zone, volume_boost,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _candles(rows: int = 30, base: float = 2350.0, vol: int = 100,
             body: float = 1.0) -> pd.DataFrame:
    """Flat OHLCV frame — most helpers don't care about candle internals."""
    return pd.DataFrame({
        "open":  [base] * rows,
        "high":  [base + body] * rows,
        "low":   [base - body] * rows,
        "close": [base] * rows,
        "tick_volume": [vol] * rows,
    })


def _candles_with_swings(highs: list, lows: list, vol: int = 100) -> pd.DataFrame:
    n = len(highs)
    return pd.DataFrame({
        "open":  [(h + l) / 2 for h, l in zip(highs, lows)],
        "high":  highs,
        "low":   lows,
        "close": [(h + l) / 2 for h, l in zip(highs, lows)],
        "tick_volume": [vol] * n,
    })


# ── rr_targets ───────────────────────────────────────────────────────────────


class TestRRTargets:
    def test_buy_targets_above_entry(self) -> None:
        tp1, tp2, tp3 = rr_targets(entry=2350.0, direction="buy", sl_pips=10.0)
        # 10 pip × 1.5/2.5/4 × 0.10 → +1.5 / +2.5 / +4.0
        assert tp1 == pytest.approx(2351.5)
        assert tp2 == pytest.approx(2352.5)
        assert tp3 == pytest.approx(2354.0)

    def test_sell_targets_below_entry(self) -> None:
        tp1, tp2, tp3 = rr_targets(entry=2350.0, direction="sell", sl_pips=10.0)
        assert tp1 == pytest.approx(2348.5)
        assert tp2 == pytest.approx(2347.5)
        assert tp3 == pytest.approx(2346.0)

    def test_tp_max_cap(self) -> None:
        # sl_pips=100 → tp3 would be 400p but cap=150p
        _, _, tp3 = rr_targets(entry=2350.0, direction="buy", sl_pips=100.0, tp_max=150.0)
        assert tp3 == pytest.approx(2350.0 + 150 * PIP)


# ── volume_boost ─────────────────────────────────────────────────────────────


class TestVolumeBoost:
    def test_returns_one_when_short(self) -> None:
        assert volume_boost(_candles(rows=5)) == 1.0

    def test_returns_one_when_none(self) -> None:
        assert volume_boost(None) == 1.0

    def test_boost_on_spike(self) -> None:
        df = _candles(rows=25, vol=100)
        df.loc[df.index[-1], "tick_volume"] = 200  # 2x avg
        assert volume_boost(df) == pytest.approx(1.4)

    def test_no_boost_below_threshold(self) -> None:
        df = _candles(rows=25, vol=100)
        df.loc[df.index[-1], "tick_volume"] = 120  # only 1.2x
        assert volume_boost(df) == 1.0


# ── structural_sl ────────────────────────────────────────────────────────────


class TestStructuralSL:
    def test_none_when_short(self) -> None:
        assert structural_sl("buy", 2350.0, _candles(rows=5), 2345.0) is None

    def test_none_when_candles_none(self) -> None:
        assert structural_sl("buy", 2350.0, None, 2345.0) is None

    def test_buy_finds_swing_low(self) -> None:
        # 80 bars; index 40 is a clear swing low far enough below entry to satisfy SL_MIN_PIPS
        n = 80
        highs = [2351.0] * n
        lows = [2349.5] * n
        # entry=2350.0, swing low 2348.0 (2 below) → sl ~ 2347.7 → 23 pips → in [8, 50]
        lows[40] = 2348.0
        lows[39] = 2349.0
        lows[41] = 2349.0
        lows[38] = 2349.0
        df = _candles_with_swings(highs, lows)
        sl = structural_sl("buy", 2350.0, df, 2345.0)
        assert sl is not None
        assert sl < 2348.0  # below the swing


# ── ob_zone ──────────────────────────────────────────────────────────────────


class TestOBZone:
    def test_buy_zone_in_range(self) -> None:
        ob = {"type": "bullish_ob", "high": 2352.0, "low": 2348.0}
        z = ob_zone(ob, "buy", price=2350.0, buf=0.5, wt=6.0, tf="H1", label_suffix="OB")
        assert z is not None
        assert z["direction"] == "buy"
        assert z["entry"] == pytest.approx(2349.2)  # 2348 + (2352-2348)*0.30
        assert z["label"] == "H1_OB"
        assert z["tf"] == "H1"

    def test_sell_zone_mirrors_buy(self) -> None:
        ob = {"type": "bearish_ob", "high": 2352.0, "low": 2348.0}
        z = ob_zone(ob, "sell", price=2350.0, buf=0.5, wt=6.0, tf="H1", label_suffix="OB")
        assert z is not None
        assert z["direction"] == "sell"
        assert z["entry"] == pytest.approx(2350.8)  # 2352 - (2352-2348)*0.30

    def test_returns_none_when_direction_mismatch(self) -> None:
        ob = {"type": "bearish_ob", "high": 2352.0, "low": 2348.0}
        assert ob_zone(ob, "buy", 2350.0, 0.5, 6.0, "H1", "OB") is None

    def test_returns_none_when_malformed(self) -> None:
        assert ob_zone({"type": "bullish_ob", "high": 0, "low": 0}, "buy", 2350.0, 0.5, 6.0, "H1", "OB") is None
        assert ob_zone({"type": "bullish_ob", "high": 100, "low": 200}, "buy", 2350.0, 0.5, 6.0, "H1", "OB") is None

    def test_returns_none_when_price_too_far(self) -> None:
        ob = {"type": "bullish_ob", "high": 2352.0, "low": 2348.0}
        # MAX_DIST = 200 * 0.10 = 20 → price > 2372 or < 2328 = reject
        assert ob_zone(ob, "buy", 2500.0, 0.5, 6.0, "H1", "OB") is None


# ── fvg_zone ─────────────────────────────────────────────────────────────────


class TestFVGZone:
    def test_buy_zone_in_range(self) -> None:
        fvg = {"type": "bullish_fvg", "high": 2352.0, "low": 2348.0}
        z = fvg_zone(fvg, "buy", price=2350.0, buf=0.5, wt=5.0, tf="H1")
        assert z is not None
        assert z["entry"] == pytest.approx(2348.4)  # 2348 + (4)*0.10
        assert z["label"].startswith("H1_BULL")

    def test_bisi_treated_as_bear(self) -> None:
        fvg = {"type": "bisi", "high": 2352.0, "low": 2348.0}
        z = fvg_zone(fvg, "sell", 2350.0, 0.5, 5.0, "H1")
        assert z is not None
        assert z["direction"] == "sell"


# ── liq_zone ─────────────────────────────────────────────────────────────────


class TestLiqZone:
    def test_unswept_returns_none(self) -> None:
        lz = {"type": "SSL", "price": 2348.0, "swept": False}
        assert liq_zone(lz, "buy", 2350.0, 0.5, 6.0, "H1") is None

    def test_buy_after_ssl_sweep(self) -> None:
        # ATR=5 → entry=2348.50, sl=2346.50, sl_p=20 pip → in [8,50] ✓
        lz = {"type": "SSL", "price": 2348.0, "swept": True}
        z = liq_zone(lz, "buy", price=2350.0, atr=5.0, wt=6.0, tf="H1")
        assert z is not None
        assert z["direction"] == "buy"
        assert z["label"] == "H1_SSL_sweep"


# ── ote_fib_zones ────────────────────────────────────────────────────────────


class TestOTEFibZones:
    def test_returns_empty_when_swing_too_small(self) -> None:
        # Truly flat — swing = 0 → below 8*PIP threshold
        df = _candles(rows=60, base=2350.0, body=0.0)
        zones = ote_fib_zones("buy", df, "H1", atr=0.5, wt=6.0, price=2350.0)
        assert zones == []

    def test_emits_zone_for_buy(self) -> None:
        # 60 bars with hi 2360, lo 2340 → swing 20 → fib 70.5% = 2360-20*0.705 = 2345.9
        highs = [2351.0] * 60
        lows = [2349.0] * 60
        highs[30] = 2360.0
        lows[30] = 2340.0
        df = _candles_with_swings(highs, lows)
        zones = ote_fib_zones("buy", df, "H1", atr=0.5, wt=6.0, price=2350.0)
        assert len(zones) == 1
        assert zones[0]["label"] == "H1_OTE"
        assert 2344.0 < zones[0]["entry"] < 2348.0


# ── find_all_ict_zones dispatch ──────────────────────────────────────────────


def _ict_stub(
    *,
    order_blocks: list | None = None,
    breaker_blocks: list | None = None,
    fvgs: list | None = None,
    liquidity_zones: list | None = None,
    structure: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        order_blocks=order_blocks or [],
        breaker_blocks=breaker_blocks or [],
        fvgs=fvgs or [],
        liquidity_zones=liquidity_zones or [],
        structure=structure or {},
    )


class TestFindAllICTZones:
    def test_empty_tf_data_returns_empty(self) -> None:
        assert find_all_ict_zones("buy", price=2350.0, tf_ict_data=[]) == []

    def test_collects_obs_across_timeframes(self) -> None:
        h4_ict = _ict_stub(order_blocks=[
            {"type": "bullish_ob", "high": 2352.0, "low": 2348.0, "mitigated": False},
        ])
        h1_ict = _ict_stub(order_blocks=[
            {"type": "bullish_ob", "high": 2354.0, "low": 2350.5, "mitigated": False},
        ])
        # body=0 disables OTE which would dominate dedup
        df = _candles(rows=60, body=0.0)
        tf_data = [("H4", h4_ict, 0.5, df), ("H1", h1_ict, 0.5, df)]
        zones = find_all_ict_zones("buy", price=2350.0, tf_ict_data=tf_data)
        labels = [z["label"] for z in zones]
        tfs = [z["tf"] for z in zones]
        assert "H4" in tfs
        assert "H1" in tfs
        assert all("OB" in l for l in labels)

    def test_skips_mitigated_obs(self) -> None:
        ict = _ict_stub(order_blocks=[
            {"type": "bullish_ob", "high": 2352.0, "low": 2348.0, "mitigated": True},
        ])
        tf_data = [("H1", ict, 0.5, _candles(60))]
        zones = find_all_ict_zones("buy", 2350.0, tf_data)
        assert all("OB" not in z["label"] for z in zones)

    def test_pdh_pdl_emitted(self) -> None:
        # ATR=5 → buf=0.5 → sl=pdl-1.0=2348.0 → sl_p=10 → in [8,50] ✓
        # body=0 disables OTE which would interfere
        ict = _ict_stub(structure={"pdh": 0, "pdl": 2349.0, "asian_high": 0, "asian_low": 0})
        tf_data = [("H1", ict, 5.0, _candles(60, body=0.0))]
        zones = find_all_ict_zones("buy", price=2350.0, tf_ict_data=tf_data)
        assert any(z["label"] == "H1_PDL" for z in zones)

    def test_dedup_collapses_near_zones(self) -> None:
        # Two near-identical OBs → dedup keeps only one
        ob = {"type": "bullish_ob", "high": 2352.0, "low": 2348.0, "mitigated": False}
        ob2 = {"type": "bullish_ob", "high": 2352.2, "low": 2348.1, "mitigated": False}
        ict_a = _ict_stub(order_blocks=[ob])
        ict_b = _ict_stub(order_blocks=[ob2])
        tf_data = [("H4", ict_a, 0.5, _candles(60)),
                   ("H1", ict_b, 0.5, _candles(60))]
        zones = find_all_ict_zones("buy", 2350.0, tf_data, dedup_buffer_price=0.5)
        entries = sorted(z["entry"] for z in zones)
        assert len(entries) == 1 or (entries[-1] - entries[0]) >= 0.5

    def test_caps_at_max_zones(self) -> None:
        # Build many distinct OBs at different price points
        obs = [
            {"type": "bullish_ob", "high": 2350.0 + i * 2, "low": 2348.0 + i * 2,
             "mitigated": False}
            for i in range(15)
        ]
        ict = _ict_stub(order_blocks=obs)
        tf_data = [("H1", ict, 0.5, _candles(60))]
        # MAX_DIST = 20 around price=2350 — keep within 20
        zones = find_all_ict_zones("buy", 2350.0, tf_data, max_zones=5)
        assert len(zones) <= 5
