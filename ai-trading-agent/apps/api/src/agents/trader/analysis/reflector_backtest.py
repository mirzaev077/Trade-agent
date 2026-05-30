"""
F4-3: ReflectorBacktester — SelfLearner/filter foydasini A/B test orqali o'lchash.

TMAS Component 8 pattern. Bizning paradigma'da "Reflector" = SelfLearner +
`blocked_setups` filter. Backtest engine SelfLearner loop'ni ishlatmaydi
(evolve/analyze faqat live'da), shu sababli A/B aslida `blocked_setups` ning
foydasini o'lchaydi (yoki kelajakda boshqa parametr toggle).

Ishlatish:
    cfg = ReflectorABConfig(start=..., end=...)
    factory = lambda s, e, treatment_on: build_engine(s, e, treatment_on).run()
    result = ReflectorBacktester(cfg, factory).run_comparison()
    print(result.verdict)  # HARMFUL / NEUTRAL / BENEFICIAL

`engine_factory(start, end, treatment_on: bool) -> BacktestResult`:
  • treatment_on=False → baseline (filter o'chirilgan / SelfLearner OFF)
  • treatment_on=True  → treatment (filter yoqilgan / SelfLearner ON)

Verdict logikasi (paired t-test daily_returns talab qiladi, bizning
BacktestResult'da hozircha yo'q — shu sababli aggregate heuristic):
  • HARMFUL    — treatment Sharpe < baseline * 0.9 yoki DD > baseline * 1.2
  • BENEFICIAL — treatment Sharpe > baseline * 1.1 va DD ≤ baseline * 1.05
  • NEUTRAL    — orasidagi farq sezilarli emas
  • WARN qo'shilishi mumkin: baseline yoki treatment trades < 30 → statistik ahamiyatsiz
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from apps.api.src.agents.trader.engine.result import BacktestResult


# ── Config + result ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReflectorABConfig:
    """A/B test sozlamalar.

    :param start: backtest boshlanish (tz-aware UTC)
    :param end:   backtest oxirgi (tz-aware UTC, exclusive)
    :param sharpe_improvement_threshold: BENEFICIAL bo'lishi uchun Sharpe necha
        marta yaxshilanishi kerak (default 1.1 = 10%+)
    :param sharpe_harm_threshold: HARMFUL chegarasi (default 0.9 = 10%- past)
    :param dd_tolerance: BENEFICIAL bo'lganda DD necha marta katta bo'lishi
        mumkin (default 1.05 = 5% tolerance)
    :param dd_harm_threshold: HARMFUL bo'lishi uchun DD necha marta katta
        (default 1.2 = 20%+ yomonlashishi)
    :param min_trades_for_significance: ikkala run uchun minimum (default 30)
    """
    start: datetime
    end: datetime
    sharpe_improvement_threshold: float = 1.10
    sharpe_harm_threshold: float = 0.90
    dd_tolerance: float = 1.05
    dd_harm_threshold: float = 1.20
    min_trades_for_significance: int = 30

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("start va end tz-aware bo'lishi kerak (UTC)")
        if self.end <= self.start:
            raise ValueError("end > start bo'lishi kerak")
        if not (
            0.0 < self.sharpe_harm_threshold < 1.0
            < self.sharpe_improvement_threshold
        ):
            raise ValueError(
                "sharpe_harm_threshold < 1.0 < sharpe_improvement_threshold bo'lishi kerak"
            )
        if self.dd_tolerance < 1.0 or self.dd_harm_threshold <= self.dd_tolerance:
            raise ValueError(
                "1.0 ≤ dd_tolerance < dd_harm_threshold bo'lishi kerak"
            )


@dataclass(frozen=True)
class ReflectorABResult:
    config: ReflectorABConfig

    # Aggregate metrics — baseline (Reflector/filter OFF)
    baseline_sharpe: float
    baseline_profit_factor: float
    baseline_total_return: float
    baseline_max_dd_pct: float
    baseline_total_trades: int

    # Treatment (Reflector/filter ON)
    treatment_sharpe: float
    treatment_profit_factor: float
    treatment_total_return: float
    treatment_max_dd_pct: float
    treatment_total_trades: int

    # Improvements (treatment - baseline)
    sharpe_improvement: float
    return_improvement_pct: float  # absolute diff (treatment_return - baseline_return)
    pf_improvement: float          # absolute diff
    dd_reduction_pct: float        # positive = better (baseline_dd - treatment_dd)

    verdict: str

    def to_dict(self) -> dict:
        return {
            "config": {
                "start": self.config.start.isoformat(),
                "end": self.config.end.isoformat(),
                "sharpe_improvement_threshold": self.config.sharpe_improvement_threshold,
                "sharpe_harm_threshold": self.config.sharpe_harm_threshold,
                "dd_tolerance": self.config.dd_tolerance,
                "dd_harm_threshold": self.config.dd_harm_threshold,
                "min_trades_for_significance": self.config.min_trades_for_significance,
            },
            "baseline": {
                "sharpe": self.baseline_sharpe,
                "profit_factor": self.baseline_profit_factor,
                "total_return": self.baseline_total_return,
                "max_dd_pct": self.baseline_max_dd_pct,
                "total_trades": self.baseline_total_trades,
            },
            "treatment": {
                "sharpe": self.treatment_sharpe,
                "profit_factor": self.treatment_profit_factor,
                "total_return": self.treatment_total_return,
                "max_dd_pct": self.treatment_max_dd_pct,
                "total_trades": self.treatment_total_trades,
            },
            "improvements": {
                "sharpe": self.sharpe_improvement,
                "return_pct": self.return_improvement_pct,
                "profit_factor": self.pf_improvement,
                "dd_reduction_pct": self.dd_reduction_pct,
            },
            "verdict": self.verdict,
        }


# ── Helpers ──────────────────────────────────────────────────────────────────


def _f(v: Optional[float], default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _verdict(
    *,
    baseline_sharpe: float,
    treatment_sharpe: float,
    baseline_dd: float,
    treatment_dd: float,
    baseline_trades: int,
    treatment_trades: int,
    cfg: ReflectorABConfig,
) -> str:
    # 1) Trade soni juda kam → statistik ahamiyatsiz
    if (
        baseline_trades < cfg.min_trades_for_significance
        or treatment_trades < cfg.min_trades_for_significance
    ):
        return (
            f"WARN — Statistik ahamiyatsiz "
            f"(baseline={baseline_trades}, treatment={treatment_trades}, "
            f"min={cfg.min_trades_for_significance} trade kerak)"
        )

    # 2) Baseline Sharpe noldan past yoki nol bo'lsa — multiplicative threshold
    # ma'no bermaydi; absolute diff bilan ishlaymiz
    if baseline_sharpe <= 0:
        if treatment_sharpe > 0.5:
            return (
                f"BENEFICIAL — Baseline Sharpe yomon ({baseline_sharpe:.2f}), "
                f"treatment ijobiy ({treatment_sharpe:.2f})"
            )
        return (
            f"NEUTRAL — Ikki run ham yomon Sharpe "
            f"(baseline={baseline_sharpe:.2f}, treatment={treatment_sharpe:.2f})"
        )

    # 3) Multiplicative thresholds
    sharpe_ratio = treatment_sharpe / baseline_sharpe
    # DD: agar baseline_dd ~ 0 bo'lsa, multiplicative bermaymiz
    dd_worse = False
    dd_ratio = 0.0
    if baseline_dd > 0.01:
        dd_ratio = treatment_dd / baseline_dd
        dd_worse = dd_ratio > cfg.dd_harm_threshold

    # HARMFUL
    if sharpe_ratio < cfg.sharpe_harm_threshold or dd_worse:
        reason = []
        if sharpe_ratio < cfg.sharpe_harm_threshold:
            reason.append(f"Sharpe {sharpe_ratio:.2f}x baseline")
        if dd_worse:
            reason.append(f"DD {dd_ratio:.2f}x katta")
        return f"HARMFUL — {', '.join(reason)}. O'CHIRISH kerak."

    # BENEFICIAL
    dd_acceptable = baseline_dd <= 0.01 or dd_ratio <= cfg.dd_tolerance
    if (
        sharpe_ratio > cfg.sharpe_improvement_threshold
        and dd_acceptable
    ):
        return (
            f"BENEFICIAL — Sharpe {sharpe_ratio:.2f}x baseline, "
            f"DD acceptable. Live'ga chiqarish mumkin."
        )

    dd_str = f"{dd_ratio:.2f}" if dd_ratio else "n/a"
    return (
        f"NEUTRAL — Sezilarli farq yo'q "
        f"(Sharpe ratio {sharpe_ratio:.2f}, DD ratio {dd_str})"
    )


# ── Backtester ───────────────────────────────────────────────────────────────


EngineFactory = Callable[[datetime, datetime, bool], BacktestResult]


class ReflectorBacktester:
    """A/B test orchestrator. `engine_factory(start, end, treatment_on)` bilan
    baseline va treatment'ni alohida ishga tushirib, natijalarni taqqoslaydi.
    """

    def __init__(
        self,
        config: ReflectorABConfig,
        engine_factory: EngineFactory,
    ) -> None:
        self.config = config
        self.engine_factory = engine_factory

    def run_comparison(self) -> ReflectorABResult:
        baseline_res = self.engine_factory(
            self.config.start, self.config.end, False
        )
        treatment_res = self.engine_factory(
            self.config.start, self.config.end, True
        )
        return self._compare(baseline_res, treatment_res)

    def _compare(
        self,
        baseline: BacktestResult,
        treatment: BacktestResult,
    ) -> ReflectorABResult:
        b_sharpe = _f(baseline.sharpe)
        t_sharpe = _f(treatment.sharpe)
        b_pf = _f(baseline.profit_factor)
        t_pf = _f(treatment.profit_factor)
        b_ret = _f(baseline.total_return)
        t_ret = _f(treatment.total_return)
        b_dd = _f(baseline.max_dd_pct)
        t_dd = _f(treatment.max_dd_pct)
        b_tr = int(baseline.total_trades or 0)
        t_tr = int(treatment.total_trades or 0)

        verdict = _verdict(
            baseline_sharpe=b_sharpe,
            treatment_sharpe=t_sharpe,
            baseline_dd=b_dd,
            treatment_dd=t_dd,
            baseline_trades=b_tr,
            treatment_trades=t_tr,
            cfg=self.config,
        )

        return ReflectorABResult(
            config=self.config,
            baseline_sharpe=b_sharpe,
            baseline_profit_factor=b_pf,
            baseline_total_return=b_ret,
            baseline_max_dd_pct=b_dd,
            baseline_total_trades=b_tr,
            treatment_sharpe=t_sharpe,
            treatment_profit_factor=t_pf,
            treatment_total_return=t_ret,
            treatment_max_dd_pct=t_dd,
            treatment_total_trades=t_tr,
            sharpe_improvement=t_sharpe - b_sharpe,
            return_improvement_pct=t_ret - b_ret,
            pf_improvement=t_pf - b_pf,
            dd_reduction_pct=b_dd - t_dd,
            verdict=verdict,
        )
