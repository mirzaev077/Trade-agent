"""
F2-3: Backtest performance metrics.

Pure functions + a PerformanceReport dataclass. Takes lists of closed trades
+ equity curve points -> produces metrics suitable for BacktestResult population
or HTML rendering.

All functions are stateless and side-effect-free.

Wilson CI: hand-coded (no statsmodels dependency). Formula uses z=1.96 for
95% confidence. Reference:
https://en.wikipedia.org/wiki/Binomial_proportion_confidence_interval#Wilson_score_interval
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from math import sqrt
from typing import Any, Iterable

import numpy as np


# --- Constants ----------------------------------------------------------
Z_95 = 1.95996398454  # standard normal 0.975 quantile, exact-ish for 95% CI
Z_99 = 2.57582930355  # for 99% CI
DEFAULT_PERIODS_PER_YEAR = 252  # trading days


# --- Wilson confidence interval (hand-coded) ----------------------------
def wilson_ci(successes: int, n: int, confidence: float = 0.95) -> tuple[float, float]:
    """
    Wilson score interval for a binomial proportion.

    Returns (lower_bound, upper_bound) - both in [0, 1].

    Edge cases:
      * n == 0           -> (0.0, 0.0)
      * successes == 0   -> (0.0, upper_bound)
      * successes == n   -> (lower_bound, 1.0)

    No external dependencies. Uses z=1.96 for 95% (hardcoded; for other
    confidence levels we'd need a lookup. 95% is the only common case in
    Tasks.md F2-3 acceptance.)

    Formula:
      p_hat = successes / n
      z2 = Z_95**2
      denominator = 1 + z2/n
      center      = (p_hat + z2/(2n)) / denominator
      margin      = z * sqrt(p_hat(1-p_hat)/n + z2/(4n**2)) / denominator
      return (center - margin, center + margin)
    """
    if n <= 0:
        return (0.0, 0.0)
    if successes < 0 or successes > n:
        raise ValueError(f"successes ({successes}) must be in [0, {n}]")
    if confidence == 0.95:
        z = Z_95
    elif confidence == 0.99:
        z = Z_99
    else:
        raise ValueError(f"only 0.95 / 0.99 supported (got {confidence})")

    p = successes / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    margin = z * sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


# --- Core metrics --------------------------------------------------------

def _pnl_list(trades: Iterable[Any]) -> list[float]:
    """Extract pnl floats from trade dicts or dataclasses."""
    pnls: list[float] = []
    for t in trades:
        if isinstance(t, dict):
            p = t.get("pnl")
        else:
            p = getattr(t, "pnl", None)
        if p is not None:
            pnls.append(float(p))
    return pnls


def win_rate(trades: Iterable[Any]) -> float:
    """Fraction of trades with pnl > 0 (excludes pnl == 0 from numerator, but counts in n)."""
    pnls = _pnl_list(trades)
    n = len(pnls)
    if n == 0:
        return 0.0
    wins = sum(1 for p in pnls if p > 0)
    return wins / n


def win_rate_with_ci(
    trades: Iterable[Any], confidence: float = 0.95
) -> tuple[float, float, float]:
    """Return (wr, ci_low, ci_high)."""
    pnls = _pnl_list(trades)
    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    wr = wins / n if n > 0 else 0.0
    lo, hi = wilson_ci(wins, n, confidence)
    return wr, lo, hi


def profit_factor(trades: Iterable[Any]) -> float:
    """gross_profit / |gross_loss|. Returns inf if no losses + has wins. 0 if both empty."""
    pnls = _pnl_list(trades)
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = sum(p for p in pnls if p < 0)
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / abs(gross_loss)


def max_drawdown_pct(equity_curve: list[dict]) -> tuple[float, float]:
    """
    Returns (max_dd_pct, max_dd_dollar).

    max_dd_pct = max((peak - equity) / peak * 100) over the curve.

    Uses the journal's pre-computed drawdown_pct field if present (each point
    already has running peak-tracked drawdown), but recomputes if absent or if
    we have only (timestamp, equity) tuples.
    """
    if not equity_curve:
        return (0.0, 0.0)

    # If points have drawdown_pct already, just take the max
    if isinstance(equity_curve[0], dict) and "drawdown_pct" in equity_curve[0]:
        max_dd_pct = max(float(p.get("drawdown_pct", 0.0)) for p in equity_curve)
        # Compute dollar drawdown: peak * (dd_pct / 100)
        peak = 0.0
        max_dd_dollar = 0.0
        for p in equity_curve:
            eq = float(p["equity"])
            peak = max(peak, eq)
            dd = peak - eq
            if dd > max_dd_dollar:
                max_dd_dollar = dd
        return (max_dd_pct, max_dd_dollar)

    # Fallback: recompute from equity values
    equities = [
        float(p["equity"] if isinstance(p, dict) else p[1]) for p in equity_curve
    ]
    peak = equities[0]
    max_dd_pct = 0.0
    max_dd_dollar = 0.0
    for eq in equities:
        peak = max(peak, eq)
        if peak > 0:
            dd_pct = (peak - eq) / peak * 100
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct
        dd_dollar = peak - eq
        if dd_dollar > max_dd_dollar:
            max_dd_dollar = dd_dollar
    return (max_dd_pct, max_dd_dollar)


def sharpe_ratio(
    equity_curve: list[dict],
    risk_free_rate: float = 0.0,
    periods_per_year: int = DEFAULT_PERIODS_PER_YEAR,
) -> float:
    """
    Annualized Sharpe ratio.

    Computed on equity returns:
      r_t = (equity_t - equity_{t-1}) / equity_{t-1}
      sharpe = (mean(r) - rf/N) / std(r) * sqrt(N)

    Where N = periods_per_year. Equity curve assumed to be sampled at uniform
    intervals - if sampled per-tick (M15), use periods_per_year ~= 252 * 24 * 4
    = 24192 for true annualization. For per-day, use 252.

    Default 252 = trading days. Caller responsible for choosing the right value.

    Returns 0.0 if std == 0 (no variance, all returns equal).
    """
    if len(equity_curve) < 2:
        return 0.0
    equities = np.array(
        [float(p["equity"] if isinstance(p, dict) else p[1]) for p in equity_curve]
    )
    # Per-period returns
    rets = np.diff(equities) / equities[:-1]
    rets = rets[~np.isnan(rets)]
    if len(rets) == 0 or rets.std(ddof=1) == 0:
        return 0.0
    excess = rets - (risk_free_rate / periods_per_year)
    return float((excess.mean() / rets.std(ddof=1)) * np.sqrt(periods_per_year))


def avg_rr(trades: Iterable[Any]) -> float:
    """
    Average REALIZED Risk-Reward across closed trades.

    Realized R = |exit - entry| / |entry - sl|.

    Only counts trades where SL is known and risk > 0. Excludes trades closed
    at break-even or for non-SL/non-TP reasons (still counted, but their R may
    be small or zero).

    If a trade record lacks `sl`, skip it (some manual closes may not have SL).
    """
    rrs = []
    for t in trades:
        if isinstance(t, dict):
            sl = t.get("sl")
            entry = t.get("entry_price")
            exit_p = t.get("exit_price")
        else:
            sl = getattr(t, "sl", None)
            entry = getattr(t, "entry_price", None)
            exit_p = getattr(t, "exit_price", None)
        if sl is None or entry is None or exit_p is None:
            continue
        risk = abs(float(entry) - float(sl))
        reward = abs(float(exit_p) - float(entry))
        if risk > 0:
            rrs.append(reward / risk)
    return float(np.mean(rrs)) if rrs else 0.0


def setup_breakdown(trades: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """
    Group trades by `setup_type` and compute per-setup stats.

    Returns:
      {
        "H1_OB": {"count": 12, "win_rate": 0.58, "profit_factor": 1.3, "total_pnl": 45.7},
        "FVG":   {...},
        ...
      }

    Sorted by count descending.
    """
    by_setup: dict[str, list[Any]] = {}
    for t in trades:
        if isinstance(t, dict):
            setup = t.get("setup_type", "UNKNOWN")
        else:
            setup = getattr(t, "setup_type", "UNKNOWN")
        by_setup.setdefault(setup or "UNKNOWN", []).append(t)

    out: dict[str, dict[str, Any]] = {}
    for setup, group in by_setup.items():
        pf = profit_factor(group)
        out[setup] = {
            "count": len(group),
            "win_rate": round(win_rate(group), 4),
            "profit_factor": round(pf, 4) if pf != float("inf") else 999.0,
            "total_pnl": round(sum(_pnl_list(group)), 2),
        }
    # Sort by count desc
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["count"]))


def total_return_pct(equity_curve: list[dict], starting_balance: float) -> float:
    """((final_equity - starting_balance) / starting_balance) * 100"""
    if not equity_curve or starting_balance <= 0:
        return 0.0
    final = float(
        equity_curve[-1]["equity"]
        if isinstance(equity_curve[-1], dict)
        else equity_curve[-1][1]
    )
    return (final - starting_balance) / starting_balance * 100


# --- PerformanceReport dataclass ----------------------------------------

@dataclass
class PerformanceReport:
    """Single-shot performance report. JSON-serializable via asdict."""

    run_id: str
    total_trades: int
    win_rate: float
    win_rate_ci_low: float
    win_rate_ci_high: float
    profit_factor: float
    avg_rr: float
    max_dd_pct: float
    max_dd_dollar: float
    sharpe_ratio: float
    total_return_pct: float
    total_pnl: float
    setup_breakdown: dict[str, dict[str, Any]] = field(default_factory=dict)
    verdict: str = "PENDING"

    def to_dict(self) -> dict:
        return asdict(self)


# --- Top-level compute() ------------------------------------------------

def compute(
    closed_trades: Iterable[Any],
    equity_curve: list[dict],
    starting_balance: float,
    run_id: str = "",
    sharpe_periods_per_year: int = DEFAULT_PERIODS_PER_YEAR,
) -> PerformanceReport:
    """One-shot performance computation.

    Returns a PerformanceReport. Caller can stuff its fields into BacktestResult
    or pass to HTML renderer.

    Verdict logic (matches Tasks.md F2-3 acceptance):
      ACCEPT  - total_trades >= 100, wr_ci_low > 0.50, profit_factor > 1.5, max_dd_pct < 15
      WARN    - total_trades >= 50 with marginal metrics
      REJECT  - fewer than 50 trades or profit_factor < 1.0
      PENDING - anything else
    """
    trades_list = list(closed_trades)
    n = len(trades_list)

    wr, lo, hi = win_rate_with_ci(trades_list)
    pf = profit_factor(trades_list)
    avg_r = avg_rr(trades_list)
    dd_pct, dd_dollar = max_drawdown_pct(equity_curve)
    sharpe = sharpe_ratio(equity_curve, periods_per_year=sharpe_periods_per_year)
    total_pnl = sum(_pnl_list(trades_list))
    total_ret = total_return_pct(equity_curve, starting_balance)
    breakdown = setup_breakdown(trades_list)

    # Verdict
    if n >= 100 and lo > 0.50 and pf > 1.5 and dd_pct < 15:
        verdict = "ACCEPT"
    elif n < 50 or pf < 1.0:
        verdict = "REJECT"
    elif n >= 50:
        verdict = "WARN"
    else:
        verdict = "PENDING"

    return PerformanceReport(
        run_id=run_id,
        total_trades=n,
        win_rate=round(wr, 4),
        win_rate_ci_low=round(lo, 4),
        win_rate_ci_high=round(hi, 4),
        profit_factor=round(pf, 4) if pf != float("inf") else 999.0,
        avg_rr=round(avg_r, 4),
        max_dd_pct=round(dd_pct, 2),
        max_dd_dollar=round(dd_dollar, 2),
        sharpe_ratio=round(sharpe, 4),
        total_return_pct=round(total_ret, 2),
        total_pnl=round(total_pnl, 2),
        setup_breakdown=breakdown,
        verdict=verdict,
    )
