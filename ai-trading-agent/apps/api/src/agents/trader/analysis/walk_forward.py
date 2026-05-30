"""
F4-2: Walk-forward validation — overfitting'ga qarshi eng kuchli usul.

TMAS pattern'idan ko'chirilgan (TREDING.md Component 6). Bizning miqyosda:
parameter optimization YO'Q — joriy parametrlar bilan train/test windows'da
backtest ishlatib, train→test metrika farqi (overfit_score) hisoblanadi.

Ishlatish:
    cfg = WalkForwardConfig(
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, tzinfo=UTC),
        train_months=12,
        test_months=1,
        step_months=1,
    )
    validator = WalkForwardValidator(cfg, engine_factory=my_factory)
    result = validator.run()
    print(result.verdict)  # ACCEPT / MARGINAL / WARN / REJECT

`engine_factory(start, end) -> BacktestResult` callable beriladi — bu testlarda
ham mock qilish, real'da `BacktestEngine` ishga tushirish uchun mos.

Verdict (TMAS spec'idan):
  ACCEPT    — OOS Sharpe ≥ 1.0, overfit < 0.6, max_dd < 25%
  MARGINAL  — OOS Sharpe 0.5-1.0, kichik kapital bilan ehtiyot bilan
  WARN      — OOS trades < 30 (statistik ahamiyatsiz)
  REJECT    — OOS Sharpe < 0.5 yoki overfit > 0.6 yoki DD > 25%
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from apps.api.src.agents.trader.engine.result import BacktestResult


# ── Config + window ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WalkForwardConfig:
    """Walk-forward sozlamalar.

    :param start:        Birinchi train oynaning boshlanishi (tz-aware)
    :param end:          Oxirgi test oynaning oxiri (exclusive)
    :param train_months: Har oynaning train davomiyligi (default 12)
    :param test_months:  Train'dan keyingi test davomiyligi (default 1)
    :param step_months:  Oynalar orasidagi qadam (default 1 = rolling)
    """
    start: datetime
    end: datetime
    train_months: int = 12
    test_months: int = 1
    step_months: int = 1

    def __post_init__(self) -> None:
        if self.train_months <= 0 or self.test_months <= 0 or self.step_months <= 0:
            raise ValueError("train/test/step_months > 0 bo'lishi kerak")
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("start va end tz-aware bo'lishi kerak (UTC)")
        if self.end <= self.start:
            raise ValueError("end > start bo'lishi kerak")


@dataclass(frozen=True)
class WalkForwardWindow:
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime

    @property
    def label(self) -> str:
        return (
            f"train[{self.train_start.date()}..{self.train_end.date()}] "
            f"test[{self.test_start.date()}..{self.test_end.date()}]"
        )


@dataclass(frozen=True)
class WindowMetrics:
    """Bir oynaning natija mini-snapshot'i (BacktestResult'dan ajratilgan)."""
    total_trades: int
    win_rate: float
    profit_factor: float
    sharpe: float
    max_dd_pct: float


@dataclass(frozen=True)
class WindowResult:
    window: WalkForwardWindow
    train: WindowMetrics
    test: WindowMetrics
    overfit_score: float


@dataclass(frozen=True)
class WalkForwardResult:
    config: WalkForwardConfig
    windows: list[WindowResult]
    oos_total_trades: int
    oos_win_rate: float
    oos_profit_factor: float
    oos_sharpe: float
    oos_max_dd_pct: float
    avg_overfit_score: float
    param_stability: float = 1.0  # placeholder; parameter opt yo'q → 1.0
    verdict: str = "PENDING"

    def to_dict(self) -> dict:
        return {
            "config": {
                "start": self.config.start.isoformat(),
                "end": self.config.end.isoformat(),
                "train_months": self.config.train_months,
                "test_months": self.config.test_months,
                "step_months": self.config.step_months,
            },
            "n_windows": len(self.windows),
            "windows": [
                {
                    "label": w.window.label,
                    "train": w.train.__dict__,
                    "test": w.test.__dict__,
                    "overfit_score": w.overfit_score,
                }
                for w in self.windows
            ],
            "oos_total_trades": self.oos_total_trades,
            "oos_win_rate": self.oos_win_rate,
            "oos_profit_factor": self.oos_profit_factor,
            "oos_sharpe": self.oos_sharpe,
            "oos_max_dd_pct": self.oos_max_dd_pct,
            "avg_overfit_score": self.avg_overfit_score,
            "param_stability": self.param_stability,
            "verdict": self.verdict,
        }


# ── Helpers ──────────────────────────────────────────────────────────────────


def _add_months(d: datetime, months: int) -> datetime:
    """Sodda month-add — sana bir xil bo'lmasligi mumkin (oxirgi kun edge case)."""
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    # Last-day clamping — masalan Jan 31 + 1 month = Feb 28
    import calendar
    day = min(d.day, calendar.monthrange(y, m)[1])
    return d.replace(year=y, month=m, day=day)


def build_windows(cfg: WalkForwardConfig) -> list[WalkForwardWindow]:
    """Rolling windows ro'yxati. Test oxiri cfg.end dan o'tmasligi shart."""
    out: list[WalkForwardWindow] = []
    cursor = cfg.start
    while True:
        train_start = cursor
        train_end = _add_months(train_start, cfg.train_months)
        test_start = train_end
        test_end = _add_months(test_start, cfg.test_months)
        if test_end > cfg.end:
            break
        out.append(
            WalkForwardWindow(
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
            )
        )
        cursor = _add_months(cursor, cfg.step_months)
    return out


def _to_metrics(r: BacktestResult) -> WindowMetrics:
    """BacktestResult dan WindowMetrics ga konvertatsiya (None → 0.0)."""
    return WindowMetrics(
        total_trades=int(getattr(r, "total_trades", 0) or 0),
        win_rate=float(getattr(r, "win_rate", 0.0) or 0.0),
        profit_factor=float(getattr(r, "profit_factor", 0.0) or 0.0),
        sharpe=float(getattr(r, "sharpe", 0.0) or 0.0),
        max_dd_pct=float(getattr(r, "max_dd_pct", 0.0) or 0.0),
    )


def calc_overfit_score(train_sharpe: float, test_sharpe: float) -> float:
    """Overfit score (TMAS spec'idan):

    0.0  = no overfitting (test ≥ train)
    >0.5 = jiddiy overfitting
    >1.0 = strategiya umuman ishlamaydi OOS

    Train Sharpe <= 0 bo'lsa — 0 (overfit hisobi ma'nosiz).
    """
    if train_sharpe <= 0:
        return 0.0
    diff = (train_sharpe - test_sharpe) / train_sharpe
    return max(0.0, diff)


def _verdict(
    oos_sharpe: float,
    oos_dd_pct: float,
    avg_overfit: float,
    oos_trades: int,
    min_trades_for_significance: int = 30,
) -> str:
    """TMAS spec'idan verdict logikasi."""
    if oos_sharpe < 0.5:
        return "REJECT — Out-of-sample Sharpe juda past (< 0.5)"
    if avg_overfit > 0.6:
        return "REJECT — Jiddiy overfitting aniqlandi"
    if oos_dd_pct > 25.0:
        return "REJECT — Max drawdown juda katta (> 25%)"
    if oos_trades < min_trades_for_significance:
        return f"WARN — Natijalar statistik ahamiyatsiz ({oos_trades} trade < {min_trades_for_significance})"
    if oos_sharpe < 1.0:
        return "MARGINAL — Live'ga chiqarish mumkin, lekin kichik kapital bilan"
    return "ACCEPT — Strategiya live'ga tayyor"


# ── Validator ────────────────────────────────────────────────────────────────


EngineFactory = Callable[[datetime, datetime], BacktestResult]


class WalkForwardValidator:
    """Walk-forward analysis orchestrator.

    `engine_factory(start, end) -> BacktestResult` callable beriladi:
    bu testlarda mock, real'da `lambda s, e: BacktestEngine(make_cfg(s, e)).run()`.
    Parameter optimization YO'Q — joriy parametrlar bilan train/test windows
    o'lchanadi (overfitting drift ko'rsatkichi sifatida).
    """

    def __init__(
        self,
        config: WalkForwardConfig,
        engine_factory: EngineFactory,
    ) -> None:
        self.config = config
        self.engine_factory = engine_factory

    def run(self) -> WalkForwardResult:
        windows = build_windows(self.config)
        if not windows:
            raise ValueError(
                f"build_windows() bo'sh ro'yxat qaytardi — start={self.config.start}, "
                f"end={self.config.end}, train={self.config.train_months}, "
                f"test={self.config.test_months}, step={self.config.step_months}"
            )

        per_window: list[WindowResult] = []

        for w in windows:
            train_res = self.engine_factory(w.train_start, w.train_end)
            test_res = self.engine_factory(w.test_start, w.test_end)
            train_m = _to_metrics(train_res)
            test_m = _to_metrics(test_res)
            score = calc_overfit_score(train_m.sharpe, test_m.sharpe)
            per_window.append(
                WindowResult(
                    window=w, train=train_m, test=test_m, overfit_score=score
                )
            )

        return self._aggregate(per_window)

    def _aggregate(self, per_window: list[WindowResult]) -> WalkForwardResult:
        # OOS aggregate — testlarning gross natijasi. PF jami W/L ratio bo'yicha
        # to'g'ri hisoblansa-da, biz mavjud window-level PF'larni weighted-by-trades
        # bilan o'rtacha olamiz (kichik scope; full re-aggregation logic kerak bo'lsa
        # PerformanceAnalyzer chaqirish kerak — bu erda yo'q).
        total_trades = sum(w.test.total_trades for w in per_window)
        if total_trades == 0:
            oos_wr = 0.0
            oos_pf = 0.0
            oos_sharpe = 0.0
            oos_dd = 0.0
        else:
            # Weighted-by-trades aggregate
            oos_wr = sum(
                w.test.win_rate * w.test.total_trades for w in per_window
            ) / total_trades
            oos_pf = sum(
                w.test.profit_factor * w.test.total_trades for w in per_window
            ) / total_trades
            oos_sharpe = sum(
                w.test.sharpe * w.test.total_trades for w in per_window
            ) / total_trades
            oos_dd = max(w.test.max_dd_pct for w in per_window)

        avg_overfit = sum(w.overfit_score for w in per_window) / len(per_window)

        verdict = _verdict(
            oos_sharpe=oos_sharpe,
            oos_dd_pct=oos_dd,
            avg_overfit=avg_overfit,
            oos_trades=total_trades,
        )

        return WalkForwardResult(
            config=self.config,
            windows=per_window,
            oos_total_trades=total_trades,
            oos_win_rate=oos_wr,
            oos_profit_factor=oos_pf,
            oos_sharpe=oos_sharpe,
            oos_max_dd_pct=oos_dd,
            avg_overfit_score=avg_overfit,
            verdict=verdict,
        )
