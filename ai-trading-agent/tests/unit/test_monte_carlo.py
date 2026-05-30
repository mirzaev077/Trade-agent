"""
F4-4 acceptance: MonteCarloSimulator — helpers + simulator.

Testlar:
  • equity_curve: cumulative sum invariant
  • max_drawdown_pct: known sequences (no-DD, monotone, V-shape)
  • longest_losing_streak: explicit examples
  • run(): reproducibility, empty input, percentile ordering,
    ruin probability bounds, edge cases (all-win, all-loss)
"""
from __future__ import annotations

import numpy as np
import pytest

from apps.api.src.agents.trader.analysis.monte_carlo import (
    MonteCarloConfig,
    MonteCarloResult,
    MonteCarloSimulator,
    equity_curve,
    longest_losing_streak,
    max_drawdown_pct,
)


# ── Config validation ────────────────────────────────────────────────────────


def test_config_rejects_n_simulations_below_100():
    with pytest.raises(ValueError, match=">= 100"):
        MonteCarloConfig(n_simulations=50)


def test_config_rejects_non_positive_balance():
    with pytest.raises(ValueError, match="initial_balance"):
        MonteCarloConfig(initial_balance=0)


def test_config_rejects_out_of_range_ruin_threshold():
    with pytest.raises(ValueError, match="ruin_threshold_pct"):
        MonteCarloConfig(ruin_threshold_pct=0)
    with pytest.raises(ValueError, match="ruin_threshold_pct"):
        MonteCarloConfig(ruin_threshold_pct=100)


# ── equity_curve ─────────────────────────────────────────────────────────────


def test_equity_curve_basic():
    pnls = np.array([10.0, -5.0, 20.0])
    eq = equity_curve(pnls, initial=100.0)
    assert eq.tolist() == [110.0, 105.0, 125.0]


def test_equity_curve_empty():
    eq = equity_curve(np.array([]), initial=100.0)
    assert eq.size == 0


# ── max_drawdown_pct ─────────────────────────────────────────────────────────


def test_max_dd_monotonic_growth_is_zero():
    eq = np.array([100.0, 110.0, 120.0, 130.0])
    assert max_drawdown_pct(eq) == 0.0


def test_max_dd_v_shape():
    # peak 200, trough 100 → DD = 50%
    eq = np.array([100.0, 150.0, 200.0, 100.0, 130.0])
    assert max_drawdown_pct(eq) == pytest.approx(50.0)


def test_max_dd_takes_max_of_multiple_drawdowns():
    # peak1 100→80 (20%), peak2 120→90 (25%) → eng yomon 25%
    eq = np.array([100.0, 80.0, 120.0, 90.0])
    assert max_drawdown_pct(eq) == pytest.approx(25.0)


def test_max_dd_empty_returns_zero():
    assert max_drawdown_pct(np.array([])) == 0.0


# ── longest_losing_streak ────────────────────────────────────────────────────


def test_streak_all_wins_is_zero():
    assert longest_losing_streak(np.array([10.0, 20.0, 5.0])) == 0


def test_streak_all_losses():
    assert longest_losing_streak(np.array([-1.0, -2.0, -3.0])) == 3


def test_streak_resets_on_win():
    # -, -, +, -, -, -, +, - → eng uzun 3
    pnls = np.array([-1.0, -2.0, 5.0, -1.0, -2.0, -3.0, 4.0, -1.0])
    assert longest_losing_streak(pnls) == 3


def test_streak_zero_is_neutral():
    # zero (BE) — streakni uzmaydi va uzaytirmaydi
    pnls = np.array([-1.0, 0.0, -2.0])
    assert longest_losing_streak(pnls) == 2


def test_streak_empty():
    assert longest_losing_streak(np.array([])) == 0


# ── run() ────────────────────────────────────────────────────────────────────


@pytest.fixture
def small_cfg() -> MonteCarloConfig:
    """Tezroq testlar uchun minimal n_simulations."""
    return MonteCarloConfig(
        n_simulations=200,
        initial_balance=10_000.0,
        ruin_threshold_pct=50.0,
        seed=42,
    )


def _mixed_pnls() -> list[float]:
    """50 ta trade, 55% wins ~20$, 45% losses ~-15$ — slight positive edge."""
    return [20.0] * 28 + [-15.0] * 22  # net +560-330 = +230


def test_run_empty_trades_returns_neutral_result(small_cfg):
    res = MonteCarloSimulator(small_cfg).run([])
    assert res.n_trades == 0
    assert res.final_equity_p50 == small_cfg.initial_balance
    assert res.max_dd_p50 == 0.0
    assert res.probability_of_ruin == 0.0


def test_run_reproducible_with_same_seed(small_cfg):
    pnls = _mixed_pnls()
    a = MonteCarloSimulator(small_cfg).run(pnls)
    b = MonteCarloSimulator(small_cfg).run(pnls)
    assert a.final_equity_p50 == b.final_equity_p50
    assert a.max_dd_p95 == b.max_dd_p95
    assert a.probability_of_ruin == b.probability_of_ruin


def test_run_different_seed_different_results():
    pnls = _mixed_pnls()
    cfg_a = MonteCarloConfig(n_simulations=200, seed=42)
    cfg_b = MonteCarloConfig(n_simulations=200, seed=99)
    a = MonteCarloSimulator(cfg_a).run(pnls)
    b = MonteCarloSimulator(cfg_b).run(pnls)
    # p50 ham aniq teng bo'lmasligi kerak (yetarli farq)
    assert (
        a.final_equity_p50 != b.final_equity_p50
        or a.max_dd_p95 != b.max_dd_p95
    )


def test_run_percentile_ordering(small_cfg):
    res = MonteCarloSimulator(small_cfg).run(_mixed_pnls())
    # Final equity: p5 <= p50 <= p95
    assert res.final_equity_p5 <= res.final_equity_p50 <= res.final_equity_p95
    # Max DD: p5 <= p50 <= p95
    assert res.max_dd_p5 <= res.max_dd_p50 <= res.max_dd_p95
    # worst_case_dd_pct = max → p95 dan kichik bo'lmasligi kerak
    assert res.worst_case_dd_pct >= res.max_dd_p95


def test_run_p_ruin_within_unit_interval(small_cfg):
    res = MonteCarloSimulator(small_cfg).run(_mixed_pnls())
    assert 0.0 <= res.probability_of_ruin <= 1.0


def test_run_all_winning_trades_zero_ruin(small_cfg):
    res = MonteCarloSimulator(small_cfg).run([10.0] * 30)
    # Hech qanday yo'qotish yo'q → DD = 0, ruin yo'q
    assert res.max_dd_p95 == 0.0
    assert res.worst_case_dd_pct == 0.0
    assert res.probability_of_ruin == 0.0
    # Final equity = initial + sum(pnls) = 10000 + 300
    assert res.final_equity_p50 == pytest.approx(10_300.0)


def test_run_all_losing_trades_high_ruin():
    # 100 ta -100$ → -10000$ → balans 0 → ruin 100%
    cfg = MonteCarloConfig(
        n_simulations=200,
        initial_balance=10_000.0,
        ruin_threshold_pct=50.0,
        seed=42,
    )
    res = MonteCarloSimulator(cfg).run([-100.0] * 100)
    assert res.probability_of_ruin == 1.0
    # Final equity == 0 (cumsum bir xil — permutation pnls ham bir xil)
    assert res.final_equity_p50 == pytest.approx(0.0)


def test_run_to_dict_serializable(small_cfg):
    res = MonteCarloSimulator(small_cfg).run(_mixed_pnls())
    d = res.to_dict()
    import json
    json.dumps(d)
    assert d["n_simulations"] == small_cfg.n_simulations
    assert "probability_of_ruin" in d


def test_run_n_trades_reported(small_cfg):
    pnls = [1.0] * 17
    res = MonteCarloSimulator(small_cfg).run(pnls)
    assert res.n_trades == 17


def test_default_simulator_uses_default_config():
    """Config bermay yaratish ham ishlaydi."""
    sim = MonteCarloSimulator()
    # Default 10000 sim — biroz uzoq, lekin test'da kichik input
    res = sim.run([10.0, -5.0] * 5)
    assert res.n_simulations == 10_000
    assert res.n_trades == 10
