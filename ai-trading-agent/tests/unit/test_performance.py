"""Unit tests for F2-3 performance analytics module.

Covers:
  * Wilson CI (5 tests)
  * Profit factor (3 tests)
  * Max drawdown (2 tests)
  * Sharpe ratio (1 test)
  * Avg RR (2 tests)
  * Setup breakdown (1 test)
  * compute() integration (2 tests + extras)
"""
from __future__ import annotations

import pytest

from apps.api.src.agents.trader.analysis.performance import (
    PerformanceReport,
    avg_rr,
    compute,
    max_drawdown_pct,
    profit_factor,
    setup_breakdown,
    sharpe_ratio,
    wilson_ci,
    win_rate,
    win_rate_with_ci,
)


# --- Wilson CI ----------------------------------------------------------

def test_wilson_ci_50_of_100_reasonable_bounds():
    lo, hi = wilson_ci(50, 100)
    # Wilson CI for p=0.5, n=100 is approx (0.404, 0.596)
    assert 0.40 < lo < 0.50
    assert 0.50 < hi < 0.60


def test_wilson_ci_empty_no_crash():
    lo, hi = wilson_ci(0, 0)
    assert lo == 0.0
    assert hi == 0.0


def test_wilson_ci_zero_successes_all_losses():
    lo, hi = wilson_ci(0, 100)
    assert lo == 0.0
    # Wilson upper bound at 0/100 ~ 0.037
    assert 0.0 < hi < 0.05


def test_wilson_ci_perfect_score():
    lo, hi = wilson_ci(100, 100)
    # Wilson lower bound at 100/100 ~ 0.963
    assert 0.95 < lo < 1.0
    assert hi == 1.0


def test_wilson_ci_invalid_successes_raises():
    with pytest.raises(ValueError):
        wilson_ci(-1, 100)
    with pytest.raises(ValueError):
        wilson_ci(101, 100)


# --- Profit factor ------------------------------------------------------

def test_profit_factor_all_winners_returns_inf():
    trades = [{"pnl": 10.0}, {"pnl": 20.0}, {"pnl": 5.0}]
    assert profit_factor(trades) == float("inf")


def test_profit_factor_mix_wins_losses():
    # gross_profit = 30, gross_loss = -10 -> PF = 3.0
    trades = [{"pnl": 10.0}, {"pnl": 20.0}, {"pnl": -10.0}]
    assert profit_factor(trades) == pytest.approx(3.0)


def test_profit_factor_all_losses_zero():
    trades = [{"pnl": -10.0}, {"pnl": -20.0}]
    assert profit_factor(trades) == 0.0


# --- Max drawdown -------------------------------------------------------

def test_max_drawdown_increasing_equity_zero():
    curve = [
        {"equity": 1000.0, "drawdown_pct": 0.0},
        {"equity": 1100.0, "drawdown_pct": 0.0},
        {"equity": 1200.0, "drawdown_pct": 0.0},
    ]
    dd_pct, dd_dollar = max_drawdown_pct(curve)
    assert dd_pct == 0.0
    assert dd_dollar == 0.0


def test_max_drawdown_known_peak_trough():
    # Peak 1200, trough 900 -> dd = 25%, $300
    # Use no drawdown_pct field -> recompute path
    curve = [
        {"equity": 1000.0},
        {"equity": 1200.0},  # peak
        {"equity": 900.0},   # trough -> 25% dd
        {"equity": 1100.0},
    ]
    dd_pct, dd_dollar = max_drawdown_pct(curve)
    assert dd_pct == pytest.approx(25.0)
    assert dd_dollar == pytest.approx(300.0)


# --- Sharpe ratio -------------------------------------------------------

def test_sharpe_constant_equity_zero():
    curve = [{"equity": 1000.0} for _ in range(10)]
    assert sharpe_ratio(curve) == 0.0


# --- Avg RR -------------------------------------------------------------

def test_avg_rr_known_values():
    # Trade 1: entry=2000, sl=1995, exit=2010 -> risk=5, reward=10 -> R=2.0
    # Trade 2: entry=2000, sl=1990, exit=2005 -> risk=10, reward=5 -> R=0.5
    # Mean = 1.25
    trades = [
        {"entry_price": 2000.0, "sl": 1995.0, "exit_price": 2010.0},
        {"entry_price": 2000.0, "sl": 1990.0, "exit_price": 2005.0},
    ]
    assert avg_rr(trades) == pytest.approx(1.25)


def test_avg_rr_no_sl_skipped():
    trades = [
        {"entry_price": 2000.0, "exit_price": 2010.0},  # no sl -> skipped
        {"entry_price": 2000.0, "sl": None, "exit_price": 2005.0},  # None -> skipped
    ]
    assert avg_rr(trades) == 0.0


# --- Setup breakdown ----------------------------------------------------

def test_setup_breakdown_grouping_and_sort():
    trades = [
        {"setup_type": "H1_OB", "pnl": 10.0},
        {"setup_type": "H1_OB", "pnl": 20.0},
        {"setup_type": "H1_OB", "pnl": -5.0},
        {"setup_type": "FVG", "pnl": 15.0},
        {"setup_type": "FVG", "pnl": -10.0},
        {"setup_type": "MMXM", "pnl": 5.0},
    ]
    bd = setup_breakdown(trades)
    keys = list(bd.keys())
    # Sorted by count desc: H1_OB(3), FVG(2), MMXM(1)
    assert keys == ["H1_OB", "FVG", "MMXM"]
    assert bd["H1_OB"]["count"] == 3
    assert bd["H1_OB"]["win_rate"] == pytest.approx(2 / 3, abs=1e-3)
    assert bd["FVG"]["count"] == 2
    assert bd["FVG"]["total_pnl"] == 5.0
    assert bd["MMXM"]["count"] == 1
    # MMXM: all winners -> profit_factor sentinel 999.0
    assert bd["MMXM"]["profit_factor"] == 999.0


# --- compute() integration ---------------------------------------------

def test_compute_empty_inputs_no_crash():
    report = compute([], [], starting_balance=10000.0, run_id="empty")
    assert isinstance(report, PerformanceReport)
    assert report.total_trades == 0
    # 0 trades < 50 -> REJECT
    assert report.verdict == "REJECT"
    assert report.win_rate == 0.0
    assert report.profit_factor == 0.0
    assert report.max_dd_pct == 0.0
    assert report.sharpe_ratio == 0.0


def test_compute_100_winning_trades_high_wr_accept():
    # Mostly winners: 80 wins of +20, 20 losses of -5 -> PF = 1600/100 = 16.0
    # Win rate = 0.80, n=100 -> CI low well above 0.5
    trades = []
    for _ in range(80):
        trades.append({
            "pnl": 20.0,
            "entry_price": 2000.0,
            "sl": 1995.0,
            "exit_price": 2010.0,
            "setup_type": "H1_OB",
        })
    for _ in range(20):
        trades.append({
            "pnl": -5.0,
            "entry_price": 2000.0,
            "sl": 1995.0,
            "exit_price": 1995.0,
            "setup_type": "H1_OB",
        })
    # Equity curve growing steadily from 10000 to 11500
    curve = []
    eq = 10000.0
    for t in trades:
        eq += t["pnl"]
        curve.append({"equity": eq, "drawdown_pct": 0.0})
    report = compute(trades, curve, starting_balance=10000.0, run_id="happy")
    assert report.total_trades == 100
    assert report.win_rate == pytest.approx(0.80)
    assert report.win_rate_ci_low > 0.50
    assert report.profit_factor > 1.5
    # Curve is monotone increasing -> max_dd_pct == 0 < 15 -> ACCEPT
    assert report.verdict == "ACCEPT"


# --- Extra coverage ----------------------------------------------------

def test_win_rate_basic():
    trades = [{"pnl": 5.0}, {"pnl": -3.0}, {"pnl": 10.0}, {"pnl": 0.0}]
    # 2 wins of 4 (pnl=0 not a win)
    assert win_rate(trades) == 0.5


def test_win_rate_with_ci_returns_triple():
    trades = [{"pnl": 5.0}] * 50 + [{"pnl": -5.0}] * 50
    wr, lo, hi = win_rate_with_ci(trades)
    assert wr == 0.5
    assert lo < wr < hi
    assert 0.0 <= lo <= 1.0
    assert 0.0 <= hi <= 1.0


def test_compute_with_dataclass_like_trades():
    """Verify compute() works with objects (not just dicts)."""

    class FakeTrade:
        def __init__(self, pnl, entry, sl, exit_p, setup):
            self.pnl = pnl
            self.entry_price = entry
            self.sl = sl
            self.exit_price = exit_p
            self.setup_type = setup

    trades = [FakeTrade(10.0, 2000.0, 1995.0, 2010.0, "FVG") for _ in range(5)]
    curve = [{"equity": 10000.0 + i * 10, "drawdown_pct": 0.0} for i in range(5)]
    report = compute(trades, curve, starting_balance=10000.0)
    assert report.total_trades == 5
    assert report.win_rate == 1.0
    # 5 trades < 50 -> REJECT (correct per verdict rules)
    assert report.verdict == "REJECT"


def test_performance_report_to_dict_serializable():
    report = compute([], [], starting_balance=10000.0, run_id="serial")
    d = report.to_dict()
    assert isinstance(d, dict)
    assert d["run_id"] == "serial"
    assert d["total_trades"] == 0
    assert "setup_breakdown" in d
