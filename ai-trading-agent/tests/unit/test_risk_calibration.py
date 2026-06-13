"""F4-4: risk_calibration — worst-case DD → risk_per_trade lever.

Tests:
  • R-multiple math + sizing-independence (lot / contract_size cancel)
  • r_multiples() drops trades without a usable SL/risk
  • calibrate() edge cases: insufficient data, all-wins (no DD)
  • linear scaling: projected DD ∝ current risk; recommended ∝ budget
  • verdict transitions: OK / REDUCE_RISK / ROOM_TO_INCREASE / INSUFFICIENT_DATA
  • bounds clamp + conservative round-down; JSON serializable
  • offline_validation wiring (run_risk_calibration + CLI flags + report)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.api.src.agents.trader.analysis.risk_calibration import (
    RiskCalibrationConfig,
    RiskCalibrationResult,
    calibrate,
    r_multiples,
)

_UTC = timezone.utc


# ── helpers ────────────────────────────────────────────────────────────────────


def _trade(pnl: float, entry: float = 2000.0, sl: float = 1990.0, lot: float = 0.01) -> dict:
    """A closed-trade dict shaped like BacktestJournal.get_closed_trades() rows."""
    return {"pnl": pnl, "entry_price": entry, "sl": sl, "lot": lot}


def _trades_with_r(rs: list[float], *, entry: float = 2000.0, sl: float = 1990.0,
                   lot: float = 0.01, contract_size: float = 100.0) -> list[dict]:
    """Build trades whose R-multiples are exactly `rs`.

    risk_dollars = |entry-sl| * lot * contract_size; pnl = R * risk_dollars.
    """
    risk_dollars = abs(entry - sl) * lot * contract_size
    return [_trade(r * risk_dollars, entry=entry, sl=sl, lot=lot) for r in rs]


# ── R-multiple math ────────────────────────────────────────────────────────────


def test_r_multiple_basic():
    # |2000-1990|*0.01*100 = 10$ risk; pnl 20$ → R = 2.0
    rs, dropped = r_multiples([_trade(20.0)], contract_size=100.0)
    assert dropped == 0
    assert rs == [pytest.approx(2.0)]


def test_r_multiple_negative_for_loss():
    rs, _ = r_multiples([_trade(-10.0)], contract_size=100.0)
    assert rs == [pytest.approx(-1.0)]   # full SL loss = -1R


def test_r_multiple_independent_of_lot_and_contract():
    # Same R regardless of lot or contract_size, because pnl carries both.
    # risk = 10*0.05*100 = 50; pnl = 2R → 100
    big = _trade(100.0, lot=0.05)
    small = _trade(20.0, lot=0.01)   # risk 10 → 2R
    rs_big, _ = r_multiples([big], contract_size=100.0)
    rs_small, _ = r_multiples([small], contract_size=100.0)
    assert rs_big[0] == pytest.approx(rs_small[0]) == pytest.approx(2.0)


def test_r_multiples_drops_missing_or_zero_sl():
    trades = [
        _trade(10.0),                       # valid
        {"pnl": 5.0, "entry_price": 2000.0, "sl": 2000.0, "lot": 0.01},  # sl==entry → risk 0
        {"pnl": 5.0, "entry_price": 2000.0, "lot": 0.01},                # no sl
        {"pnl": 5.0, "entry_price": 2000.0, "sl": 1990.0},               # no lot
        {"entry_price": 2000.0, "sl": 1990.0, "lot": 0.01},              # no pnl
    ]
    rs, dropped = r_multiples(trades, contract_size=100.0)
    assert len(rs) == 1
    assert dropped == 4


def test_r_multiples_handles_non_numeric():
    rs, dropped = r_multiples([{"pnl": "x", "entry_price": 2000, "sl": 1990, "lot": 0.01}])
    assert rs == [] and dropped == 1


# ── config validation ──────────────────────────────────────────────────────────


def test_config_rejects_bad_values():
    with pytest.raises(ValueError):
        RiskCalibrationConfig(dd_budget_pct=0, current_risk_pct=1.0)
    with pytest.raises(ValueError):
        RiskCalibrationConfig(dd_budget_pct=10.0, current_risk_pct=0)
    with pytest.raises(ValueError):
        RiskCalibrationConfig(dd_budget_pct=10.0, current_risk_pct=1.0, contract_size=0)
    with pytest.raises(ValueError):
        RiskCalibrationConfig(dd_budget_pct=10.0, current_risk_pct=1.0,
                              risk_floor_pct=6.0, risk_ceiling_pct=5.0)


# ── calibrate(): insufficient data ───────────────────────────────────────────


def test_calibrate_insufficient_data():
    cfg = RiskCalibrationConfig(dd_budget_pct=10.0, current_risk_pct=1.0, min_trades=20)
    res = calibrate(_trades_with_r([1.0, -1.0, 2.0]), cfg)
    assert res.verdict == "INSUFFICIENT_DATA"
    assert res.recommended_risk_pct == 1.0   # unchanged
    assert res.n_trades_valid == 3
    assert res.within_budget is True


def test_calibrate_counts_dropped_in_insufficient():
    cfg = RiskCalibrationConfig(dd_budget_pct=10.0, current_risk_pct=1.0, min_trades=20)
    trades = _trades_with_r([1.0]) + [{"pnl": 5.0}]   # 1 valid, 1 dropped
    res = calibrate(trades, cfg)
    assert res.n_trades_total == 2
    assert res.n_trades_valid == 1
    assert res.n_trades_dropped == 1


# ── calibrate(): all-wins → no drawdown ──────────────────────────────────────


def test_calibrate_all_wins_no_dd_allows_ceiling():
    cfg = RiskCalibrationConfig(dd_budget_pct=10.0, current_risk_pct=1.0,
                                min_trades=10, n_simulations=200, risk_ceiling_pct=5.0)
    res = calibrate(_trades_with_r([1.0] * 30), cfg)
    assert res.worst_dd_per_1pct == 0.0
    assert res.verdict == "OK"
    assert res.recommended_risk_pct == 5.0
    assert res.within_budget is True


# ── calibrate(): REDUCE_RISK when projected DD over budget ───────────────────


def test_calibrate_reduce_risk_when_over_budget():
    # Heavy losing tail → big worst-case DD per 1%. Tight budget + high current risk.
    rs = [-1.0] * 40 + [1.5] * 40     # deep clustered-loss potential
    cfg = RiskCalibrationConfig(dd_budget_pct=5.0, current_risk_pct=2.0,
                                min_trades=10, n_simulations=500)
    res = calibrate(_trades_with_r(rs), cfg)
    assert res.worst_dd_per_1pct > 0
    assert res.projected_worst_dd_pct == pytest.approx(res.worst_dd_per_1pct * 2.0)
    if res.projected_worst_dd_pct > cfg.dd_budget_pct:
        assert res.verdict == "REDUCE_RISK"
        assert res.recommended_risk_pct < cfg.current_risk_pct
        assert res.within_budget is False


# ── linear scaling + recommended back-out ────────────────────────────────────


def test_projected_dd_scales_linearly_with_current_risk():
    rs = [-1.0] * 30 + [2.0] * 30
    base = _trades_with_r(rs)
    a = calibrate(base, RiskCalibrationConfig(dd_budget_pct=10.0, current_risk_pct=1.0,
                                              min_trades=10, n_simulations=300))
    b = calibrate(base, RiskCalibrationConfig(dd_budget_pct=10.0, current_risk_pct=2.0,
                                              min_trades=10, n_simulations=300))
    # Same R-set + same seed → same worst_dd_per_1pct; projection doubles with risk.
    assert a.worst_dd_per_1pct == pytest.approx(b.worst_dd_per_1pct)
    assert b.projected_worst_dd_pct == pytest.approx(2.0 * a.projected_worst_dd_pct)


def test_recommended_risk_matches_budget_over_worst_dd():
    rs = [-1.0] * 30 + [2.0] * 30
    cfg = RiskCalibrationConfig(dd_budget_pct=8.0, current_risk_pct=1.0,
                                min_trades=10, n_simulations=300)
    res = calibrate(_trades_with_r(rs), cfg)
    import math
    expected = math.floor((cfg.dd_budget_pct / res.worst_dd_per_1pct) * 100) / 100
    expected = max(cfg.risk_floor_pct, min(expected, cfg.risk_ceiling_pct))
    assert res.recommended_risk_pct == pytest.approx(expected)


def test_recommended_clamped_to_ceiling():
    # Tiny DD per 1% + huge budget → raw recommendation exceeds ceiling → clamp.
    rs = [-0.1] * 20 + [3.0] * 60     # shallow losses, strong wins
    cfg = RiskCalibrationConfig(dd_budget_pct=40.0, current_risk_pct=1.0,
                                min_trades=10, n_simulations=300, risk_ceiling_pct=5.0)
    res = calibrate(_trades_with_r(rs), cfg)
    assert res.recommended_risk_pct <= 5.0


def test_reproducible_same_seed():
    rs = [-1.0] * 20 + [1.5] * 25
    cfg = RiskCalibrationConfig(dd_budget_pct=10.0, current_risk_pct=1.0,
                                min_trades=10, n_simulations=300, seed=7)
    a = calibrate(_trades_with_r(rs), cfg)
    b = calibrate(_trades_with_r(rs), cfg)
    assert a.worst_dd_per_1pct == b.worst_dd_per_1pct
    assert a.recommended_risk_pct == b.recommended_risk_pct


def test_to_dict_json_serializable():
    rs = [-1.0] * 20 + [1.5] * 25
    res = calibrate(_trades_with_r(rs),
                    RiskCalibrationConfig(dd_budget_pct=10.0, current_risk_pct=1.0,
                                          min_trades=10, n_simulations=200))
    d = res.to_dict()
    json.dumps(d)
    assert d["verdict"] in {"OK", "REDUCE_RISK", "ROOM_TO_INCREASE", "INSUFFICIENT_DATA"}
    assert "recommended_risk_pct" in d and "notes" in d


def test_result_is_frozen():
    res = calibrate(_trades_with_r([1.0] * 30),
                    RiskCalibrationConfig(dd_budget_pct=10.0, current_risk_pct=1.0,
                                          min_trades=10, n_simulations=200))
    assert isinstance(res, RiskCalibrationResult)
    with pytest.raises((AttributeError, TypeError)):
        res.verdict = "X"   # type: ignore[misc]


# ── offline_validation wiring ─────────────────────────────────────────────────


import apps.api.src.agents.trader.analysis.offline_validation as ov


def _arts(closed_trades):
    return SimpleNamespace(perf_report=None, closed_trades=closed_trades)


def test_run_risk_calibration_uses_closed_trades(monkeypatch):
    trades = _trades_with_r([-1.0] * 25 + [2.0] * 25)
    monkeypatch.setattr(ov, "run_backtest", lambda *a, **k: _arts(trades))
    res = ov.run_risk_calibration(
        ov.production_params("XAUUSD", Path("d")),
        datetime(2024, 1, 1, tzinfo=_UTC),
        datetime(2024, 4, 1, tzinfo=_UTC),
        dd_budget_pct=10.0, current_risk_pct=1.0, n_simulations=200,
    )
    assert res.n_trades_valid == 50
    assert res.verdict in {"OK", "REDUCE_RISK", "ROOM_TO_INCREASE"}


def test_run_risk_calibration_defaults_from_riskconfig():
    # The module wires DD budget / current risk from the live RiskConfig.
    from apps.api.src.agents.trader.models.config import RiskConfig
    rc_defaults = RiskConfig()
    assert ov.DEFAULT_DD_BUDGET_PCT == rc_defaults.max_drawdown
    assert ov.DEFAULT_CURRENT_RISK_PCT == rc_defaults.risk_per_trade


def test_cli_has_calibration_flags():
    ns = ov._parse_args(["--start", "2024-01-01", "--end", "2026-01-01",
                         "--data-path", "d"])
    assert ns.dd_budget_pct == ov.DEFAULT_DD_BUDGET_PCT
    assert ns.current_risk_pct == ov.DEFAULT_CURRENT_RISK_PCT
    assert ns.calib_sims == 10_000
    assert ns.skip_risk_calibration is False


def test_cli_skip_risk_calibration():
    ns = ov._parse_args(["--start", "2024-01-01", "--end", "2026-01-01",
                         "--data-path", "d", "--skip-risk-calibration"])
    assert ns.skip_risk_calibration is True
    assert ns.skip_monte_carlo is False


# ── review follow-ups: closed coverage gaps + robustness ─────────────────────


def test_room_to_increase_verdict():
    # Shallow losses + strong wins → low worst-DD/1% → budget allows >> current risk.
    cfg = RiskCalibrationConfig(dd_budget_pct=10.0, current_risk_pct=1.0,
                                min_trades=10, n_simulations=500, headroom_factor=1.25)
    res = calibrate(_trades_with_r([-0.3] * 30 + [2.0] * 50), cfg)
    assert res.verdict == "ROOM_TO_INCREASE"
    assert res.within_budget is True
    assert res.recommended_risk_pct >= cfg.current_risk_pct * cfg.headroom_factor
    assert res.recommended_risk_pct <= cfg.risk_ceiling_pct


def test_room_to_increase_gated_when_at_ceiling():
    # current_risk == ceiling → cannot recommend more → verdict reverts to OK.
    cfg = RiskCalibrationConfig(dd_budget_pct=15.0, current_risk_pct=5.0,
                                min_trades=10, n_simulations=500, risk_ceiling_pct=5.0)
    res = calibrate(_trades_with_r([-0.3] * 30 + [2.0] * 50), cfg)
    assert res.within_budget is True
    assert res.verdict == "OK"   # NOT ROOM_TO_INCREASE — already at ceiling


def test_recommended_clamped_to_floor():
    # Deep clustered losses → huge worst-DD/1% → raw recommendation below floor.
    cfg = RiskCalibrationConfig(dd_budget_pct=5.0, current_risk_pct=2.0,
                                min_trades=10, n_simulations=500, risk_floor_pct=0.15)
    res = calibrate(_trades_with_r([-5.0] * 60 + [2.0] * 40), cfg)
    raw = cfg.dd_budget_pct / res.worst_dd_per_1pct
    assert raw < cfg.risk_floor_pct                 # would undershoot the floor
    assert res.recommended_risk_pct == cfg.risk_floor_pct  # clamped UP to floor
    assert res.verdict == "REDUCE_RISK"


def test_recommended_is_conservative_round_down():
    # Two independent properties (not the impl formula): 2-decimal precision and
    # never exceeding the raw budget-implied risk (round DOWN, conservative).
    cfg = RiskCalibrationConfig(dd_budget_pct=7.0, current_risk_pct=1.0,
                                min_trades=10, n_simulations=500)
    res = calibrate(_trades_with_r([-1.0] * 40 + [1.7] * 40), cfg)
    raw = cfg.dd_budget_pct / res.worst_dd_per_1pct
    if cfg.risk_floor_pct < raw < cfg.risk_ceiling_pct:   # only when not clamped
        assert res.recommended_risk_pct <= raw + 1e-12     # never rounds UP
        cents = res.recommended_risk_pct * 100.0
        assert abs(cents - round(cents)) < 1e-9            # 2-decimal precision


def test_dropped_count_in_sufficient_data_path():
    # 49 valid + 1 malformed; still enough trades → not INSUFFICIENT_DATA.
    trades = _trades_with_r([1.0, -1.0] * 24 + [1.0]) + [{"pnl": 5.0, "entry_price": 2000.0}]
    cfg = RiskCalibrationConfig(dd_budget_pct=10.0, current_risk_pct=1.0,
                                min_trades=10, n_simulations=300)
    res = calibrate(trades, cfg)
    assert res.n_trades_total == 50
    assert res.n_trades_valid == 49
    assert res.n_trades_dropped == 1
    assert res.verdict != "INSUFFICIENT_DATA"


def test_avg_r_multiple_correctness():
    # avg over VALID trades only (not total, not including dropped).
    rs = [1.0, -1.0, 2.0, 0.5]
    res = calibrate(_trades_with_r(rs),
                    RiskCalibrationConfig(dd_budget_pct=10.0, current_risk_pct=1.0,
                                          min_trades=2, n_simulations=200))
    assert res.avg_r_multiple == pytest.approx(sum(rs) / len(rs))


def test_avg_r_multiple_excludes_dropped():
    rs = [1.0, -1.0, 2.0]
    trades = _trades_with_r(rs) + [{"pnl": 99.0}]   # dropped, must not affect avg
    res = calibrate(trades, RiskCalibrationConfig(dd_budget_pct=10.0, current_risk_pct=1.0,
                                                  min_trades=2, n_simulations=200))
    assert res.avg_r_multiple == pytest.approx(sum(rs) / len(rs))


def test_non_finite_pnl_is_filtered_no_crash():
    # inf / nan PnL (or zero-risk) must be dropped before the Monte-Carlo so the
    # offline job never crashes on math.floor(NaN/inf). Recommendation stays finite.
    import math
    good = _trades_with_r([-1.0] * 20 + [1.5] * 20)
    bad = [
        {"pnl": float("inf"), "entry_price": 2000.0, "sl": 1990.0, "lot": 0.01},
        {"pnl": float("nan"), "entry_price": 2000.0, "sl": 1990.0, "lot": 0.01},
        {"pnl": 5.0, "entry_price": 2000.0, "sl": 2000.0, "lot": 0.01},  # zero risk
    ]
    res = calibrate(good + bad, RiskCalibrationConfig(dd_budget_pct=10.0, current_risk_pct=1.0,
                                                      min_trades=10, n_simulations=300))
    assert res.n_trades_valid == 40
    assert res.n_trades_dropped == 3
    assert math.isfinite(res.recommended_risk_pct)
    assert math.isfinite(res.worst_dd_per_1pct)


def test_reference_risk_mapping_is_1pct_of_100_balance():
    # Pin claim C2: calibrate() runs the reference MC at initial_balance=100 with
    # pnls = R_i (1R = 1% of balance). If _REF_BALANCE / _REF_RISK_PCT drift, this
    # breaks. Recompute the reference MC independently and require an exact match.
    from apps.api.src.agents.trader.analysis.monte_carlo import (
        MonteCarloConfig, MonteCarloSimulator,
    )
    rs = [1.0, -1.0] * 15
    cfg = RiskCalibrationConfig(dd_budget_pct=10.0, current_risk_pct=1.0,
                                min_trades=10, n_simulations=500, seed=42)
    res = calibrate(_trades_with_r(rs), cfg)
    ref = MonteCarloSimulator(
        MonteCarloConfig(n_simulations=500, initial_balance=100.0, seed=42)
    ).run(rs)
    assert res.worst_dd_per_1pct == pytest.approx(ref.worst_case_dd_pct)
    assert res.p95_dd_per_1pct == pytest.approx(ref.max_dd_p95)


def test_different_seeds_produce_different_mc_results():
    rs = [-1.0] * 25 + [1.5] * 25
    a = calibrate(_trades_with_r(rs),
                  RiskCalibrationConfig(dd_budget_pct=10.0, current_risk_pct=1.0,
                                        min_trades=10, n_simulations=400, seed=7))
    b = calibrate(_trades_with_r(rs),
                  RiskCalibrationConfig(dd_budget_pct=10.0, current_risk_pct=1.0,
                                        min_trades=10, n_simulations=400, seed=8))
    assert a.worst_dd_per_1pct != b.worst_dd_per_1pct


def test_reproducible_same_seed_all_mc_fields():
    rs = [-1.0] * 20 + [1.5] * 25
    cfg = RiskCalibrationConfig(dd_budget_pct=10.0, current_risk_pct=1.0,
                                min_trades=10, n_simulations=300, seed=7)
    a = calibrate(_trades_with_r(rs), cfg)
    b = calibrate(_trades_with_r(rs), cfg)
    assert a.worst_dd_per_1pct == b.worst_dd_per_1pct
    assert a.p95_dd_per_1pct == b.p95_dd_per_1pct
    assert a.expected_max_losing_streak_p95 == b.expected_max_losing_streak_p95


@pytest.mark.parametrize("rs,risk,budget,min_trades", [
    ([1.0, -1.0], 1.0, 10.0, 20),                 # INSUFFICIENT_DATA
    ([1.0] * 30, 1.0, 10.0, 10),                  # OK (all wins, no DD)
    ([-1.0] * 54 + [1.38] * 46, 2.0, 5.0, 10),    # REDUCE_RISK
    ([-0.3] * 30 + [2.0] * 50, 1.0, 10.0, 10),    # ROOM_TO_INCREASE / OK
])
def test_notes_are_console_safe(rs, risk, budget, min_trades):
    # The offline job logs `notes` via loguru to a Windows cp1251 console; any
    # non-ASCII (->, ~, em-dash) there would crash the run. Lock it ASCII-clean.
    res = calibrate(_trades_with_r(rs),
                    RiskCalibrationConfig(dd_budget_pct=budget, current_risk_pct=risk,
                                          min_trades=min_trades, n_simulations=300))
    res.notes.encode("cp1251")   # raises on non-ASCII-cp1251 chars
    res.notes.encode("ascii")    # stricter: notes must be plain ASCII


def test_monte_carlo_from_trades_pure():
    trades = [{"pnl": 1.0}, {"pnl": -0.5}, {"pnl": 2.0}]
    res = ov.monte_carlo_from_trades(trades, n_simulations=200, initial_balance=10_000.0)
    assert res.n_trades == 3
    assert res.n_simulations == 200
