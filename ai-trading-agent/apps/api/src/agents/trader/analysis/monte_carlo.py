"""
F4-4: Monte Carlo robustness — trade order tasodifiyligini test qiladi.

TMAS Component 7 pattern. Real backtest yaxshi natija bersa-da, bu "lucky
sequence" bo'lishi mumkin. Trade'larni N marta shuffle qilib, har bir
permutatsiya uchun equity curve, DD, losing-streak hisoblaymiz va
percentile statistikani chiqaramiz.

Asosiy savollar:
  • 5th percentile DD — eng yomon 5% holatlar nima ko'rsatadi? (haqiqiy
    risk shu, backtest'dagi DD emas)
  • Probability of ruin — balans `ruin_threshold` (default 50% initial)
    dan tushish ehtimoli
  • Longest losing streak — 95th percentile (kutilishi mumkin bo'lgan
    eng uzun yo'qotuvchi seriya)

Ishlatish:
    sim = MonteCarloSimulator(MonteCarloConfig(n_simulations=10000))
    result = sim.run(pnls=[20, -10, 15, -8, 22, -5, ...])
    print(f"Worst-case DD: {result.worst_case_dd_pct:.2f}%")
    print(f"P(ruin): {result.probability_of_ruin:.2%}")
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np


@dataclass(frozen=True)
class MonteCarloConfig:
    n_simulations: int = 10_000
    initial_balance: float = 10_000.0
    ruin_threshold_pct: float = 50.0   # initial_balance * (1 - x/100) dan past → ruin
    seed: int = 42

    def __post_init__(self) -> None:
        if self.n_simulations < 100:
            raise ValueError("n_simulations >= 100 bo'lishi kerak")
        if self.initial_balance <= 0:
            raise ValueError("initial_balance > 0 bo'lishi kerak")
        if not (0.0 < self.ruin_threshold_pct < 100.0):
            raise ValueError("ruin_threshold_pct 0..100 oralig'ida bo'lishi kerak")


@dataclass(frozen=True)
class MonteCarloResult:
    n_simulations: int
    n_trades: int
    initial_balance: float

    # Final equity percentilelar (USD)
    final_equity_p5: float
    final_equity_p50: float
    final_equity_p95: float

    # Drawdown percentilelar (foiz)
    max_dd_p5: float
    max_dd_p50: float
    max_dd_p95: float
    worst_case_dd_pct: float   # 100-pct percentile (eng yomon)

    # Losing-streak (trade soni)
    expected_max_losing_streak_p95: int

    # Ruin probability
    probability_of_ruin: float
    ruin_threshold_usd: float

    def to_dict(self) -> dict:
        return {
            "n_simulations": self.n_simulations,
            "n_trades": self.n_trades,
            "initial_balance": self.initial_balance,
            "final_equity_p5": self.final_equity_p5,
            "final_equity_p50": self.final_equity_p50,
            "final_equity_p95": self.final_equity_p95,
            "max_dd_p5": self.max_dd_p5,
            "max_dd_p50": self.max_dd_p50,
            "max_dd_p95": self.max_dd_p95,
            "worst_case_dd_pct": self.worst_case_dd_pct,
            "expected_max_losing_streak_p95": self.expected_max_losing_streak_p95,
            "probability_of_ruin": self.probability_of_ruin,
            "ruin_threshold_usd": self.ruin_threshold_usd,
        }


# ── Helpers ──────────────────────────────────────────────────────────────────


def equity_curve(pnls: np.ndarray, initial: float) -> np.ndarray:
    """Cumulative equity curve. equity[i] = initial + sum(pnls[:i+1])."""
    return initial + np.cumsum(pnls)


def max_drawdown_pct(equity: np.ndarray) -> float:
    """Maksimum drawdown foizda (positive, 0..100).

    DD = (peak - trough) / peak * 100. Peak = `np.maximum.accumulate`.
    Equity bo'sh yoki peak <= 0 bo'lsa → 0.0.
    """
    if equity.size == 0:
        return 0.0
    peak = np.maximum.accumulate(equity)
    # peak <= 0 bo'lgan joylarda 0 deb hisoblaymiz (negative equity holati)
    safe_peak = np.where(peak > 0, peak, 1.0)
    dd_pct = (peak - equity) / safe_peak * 100.0
    dd_pct = np.where(peak > 0, dd_pct, 0.0)
    return float(np.max(dd_pct))


def longest_losing_streak(pnls: np.ndarray) -> int:
    """Eng uzun ketma-ket yo'qotish (pnl < 0) seriya uzunligi.

    `pnl == 0` neutral — streak'ni uzmaydi va uzaytirmaydi (BE trade).
    """
    if pnls.size == 0:
        return 0
    longest = 0
    current = 0
    for v in pnls:
        if v < 0:
            current += 1
            if current > longest:
                longest = current
        elif v > 0:
            current = 0
        # v == 0 → neutral, do nothing
    return int(longest)


# ── Simulator ────────────────────────────────────────────────────────────────


class MonteCarloSimulator:
    """Bootstrap permutation Monte Carlo robustness analyzer."""

    def __init__(self, config: MonteCarloConfig | None = None) -> None:
        self.config = config or MonteCarloConfig()

    def run(self, pnls: Iterable[float]) -> MonteCarloResult:
        cfg = self.config
        pnls_arr = np.asarray(list(pnls), dtype=float)

        if pnls_arr.size == 0:
            ruin_usd = cfg.initial_balance * (1.0 - cfg.ruin_threshold_pct / 100.0)
            return MonteCarloResult(
                n_simulations=0,
                n_trades=0,
                initial_balance=cfg.initial_balance,
                final_equity_p5=cfg.initial_balance,
                final_equity_p50=cfg.initial_balance,
                final_equity_p95=cfg.initial_balance,
                max_dd_p5=0.0,
                max_dd_p50=0.0,
                max_dd_p95=0.0,
                worst_case_dd_pct=0.0,
                expected_max_losing_streak_p95=0,
                probability_of_ruin=0.0,
                ruin_threshold_usd=ruin_usd,
            )

        rng = np.random.default_rng(cfg.seed)
        final_eq = np.empty(cfg.n_simulations, dtype=float)
        max_dd = np.empty(cfg.n_simulations, dtype=float)
        streaks = np.empty(cfg.n_simulations, dtype=int)

        for i in range(cfg.n_simulations):
            shuffled = rng.permutation(pnls_arr)
            eq = equity_curve(shuffled, cfg.initial_balance)
            final_eq[i] = float(eq[-1])
            max_dd[i] = max_drawdown_pct(eq)
            streaks[i] = longest_losing_streak(shuffled)

        ruin_usd = cfg.initial_balance * (1.0 - cfg.ruin_threshold_pct / 100.0)
        n_ruined = int(np.sum(final_eq < ruin_usd))
        p_ruin = n_ruined / cfg.n_simulations

        return MonteCarloResult(
            n_simulations=cfg.n_simulations,
            n_trades=int(pnls_arr.size),
            initial_balance=cfg.initial_balance,
            final_equity_p5=float(np.percentile(final_eq, 5)),
            final_equity_p50=float(np.percentile(final_eq, 50)),
            final_equity_p95=float(np.percentile(final_eq, 95)),
            max_dd_p5=float(np.percentile(max_dd, 5)),
            max_dd_p50=float(np.percentile(max_dd, 50)),
            max_dd_p95=float(np.percentile(max_dd, 95)),
            worst_case_dd_pct=float(np.max(max_dd)),
            expected_max_losing_streak_p95=int(np.percentile(streaks, 95)),
            probability_of_ruin=p_ruin,
            ruin_threshold_usd=ruin_usd,
        )
