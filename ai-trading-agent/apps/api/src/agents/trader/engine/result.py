"""
BacktestResult — backtest natijasining skeleton konteyneri.

Bu modul faqat meta maydonlar (run_id, started_at, ended_at, verdict) va
total_trades bilan to'ldirilgan skeleton natijani taqdim etadi. Qolgan
metrikalar (returns, risk-adjusted, drawdown, trade stats, distribution,
statistical CI) keyingi PerformanceAnalyzer tomonidan hisoblanadi va
to'ldiriladi. Vaqt maydonlari har doim tz-aware (UTC) bo'lishi shart.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any

_VALID_VERDICTS = frozenset({"PENDING", "REJECT", "WARN", "MARGINAL", "ACCEPT"})


@dataclass(frozen=True)
class BacktestResult:
    """Backtest natijasi — barcha metrikalar va statistika."""

    # ── Meta (required) ───────────────────────────────────────
    run_id: str
    started_at: datetime
    ended_at: datetime
    verdict: str = "PENDING"

    # ── Returns ───────────────────────────────────────────────
    total_return: float | None = None
    cagr: float | None = None
    best_month: float | None = None
    worst_month: float | None = None

    # ── Risk-adjusted ─────────────────────────────────────────
    sharpe: float | None = None
    sortino: float | None = None
    calmar: float | None = None
    mar: float | None = None

    # ── Drawdown ──────────────────────────────────────────────
    max_dd_pct: float | None = None
    max_dd_dollar: float | None = None
    max_dd_duration_days: float | None = None

    # ── Trade stats ───────────────────────────────────────────
    total_trades: int = 0
    win_rate: float | None = None
    profit_factor: float | None = None
    expectancy: float | None = None
    expectancy_in_r: float | None = None
    avg_rr: float | None = None

    # ── Distribution ──────────────────────────────────────────
    skewness: float | None = None
    kurtosis: float | None = None
    tail_ratio: float | None = None

    # ── Statistical CI ────────────────────────────────────────
    returns_p_value: float | None = None
    sharpe_ci_low: float | None = None
    sharpe_ci_high: float | None = None
    win_rate_ci_low: float | None = None
    win_rate_ci_high: float | None = None
    is_statistically_significant: bool = False

    def __post_init__(self) -> None:
        if self.started_at.tzinfo is None:
            raise ValueError(
                "BacktestResult.started_at must be tz-aware (UTC). Got naive datetime."
            )
        if self.ended_at.tzinfo is None:
            raise ValueError(
                "BacktestResult.ended_at must be tz-aware (UTC). Got naive datetime."
            )
        if self.ended_at < self.started_at:
            raise ValueError(
                f"BacktestResult.ended_at ({self.ended_at.isoformat()}) cannot be "
                f"before BacktestResult.started_at ({self.started_at.isoformat()})."
            )
        if self.verdict not in _VALID_VERDICTS:
            raise ValueError(
                f"BacktestResult.verdict ({self.verdict!r}) must be one of "
                f"{sorted(_VALID_VERDICTS)!r}."
            )
        if self.total_trades < 0:
            raise ValueError(
                f"BacktestResult.total_trades cannot be negative, got {self.total_trades}"
            )

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict — DB JSONB saqlash uchun (datetime → ISO 8601)."""
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, datetime):
                out[f.name] = value.isoformat()
            else:
                out[f.name] = value
        return out
