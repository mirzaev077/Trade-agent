"""BacktestResult dataclass uchun unit testlar.

Validatsiya, immutability, default qiymatlar va to_dict serializatsiyasini tekshiradi.

F2-2 port: TMAS tests/unit/test_result.py dan ko'chirilgan.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from apps.api.src.agents.trader.engine.result import BacktestResult


# ── Helpers ───────────────────────────────────────────────────────────────────


_BASE_START = datetime(2024, 1, 1, tzinfo=timezone.utc)
_BASE_END = _BASE_START + timedelta(hours=1)


def _make_result(**overrides) -> BacktestResult:
    """Minimal valid BacktestResult builder for tests."""
    defaults = dict(run_id="abc", started_at=_BASE_START, ended_at=_BASE_END)
    defaults.update(overrides)
    return BacktestResult(**defaults)


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_result_required_fields_minimal() -> None:
    """Minimal construction sets verdict, total_trades and significance defaults."""
    cfg = _make_result()

    assert cfg.verdict == "PENDING"
    assert cfg.total_trades == 0
    assert cfg.is_statistically_significant is False


def test_result_all_metric_defaults_are_none() -> None:
    """All 24 numeric metric fields default to None."""
    cfg = _make_result()

    none_fields = [
        "total_return",
        "cagr",
        "best_month",
        "worst_month",
        "sharpe",
        "sortino",
        "calmar",
        "mar",
        "max_dd_pct",
        "max_dd_dollar",
        "max_dd_duration_days",
        "win_rate",
        "profit_factor",
        "expectancy",
        "expectancy_in_r",
        "avg_rr",
        "skewness",
        "kurtosis",
        "tail_ratio",
        "returns_p_value",
        "sharpe_ci_low",
        "sharpe_ci_high",
        "win_rate_ci_low",
        "win_rate_ci_high",
    ]
    for field in none_fields:
        assert getattr(cfg, field) is None, f"{field} should default to None"


def test_result_requires_tz_aware_started_at() -> None:
    """Naive datetime for started_at or ended_at must raise ValueError."""
    with pytest.raises(ValueError, match="tz-aware"):
        _make_result(started_at=datetime(2024, 1, 1))

    with pytest.raises(ValueError, match="tz-aware"):
        _make_result(ended_at=datetime(2024, 1, 1))


def test_result_ended_before_started_raises() -> None:
    """ended_at < started_at must raise; equality is allowed."""
    with pytest.raises(ValueError):
        _make_result(
            started_at=_BASE_END,
            ended_at=_BASE_START,
        )

    # Equality must NOT raise.
    cfg = _make_result(started_at=_BASE_START, ended_at=_BASE_START)
    assert cfg.started_at == cfg.ended_at


def test_result_invalid_verdict_raises() -> None:
    """Invalid verdict raises; all 5 canonical verdicts construct successfully."""
    with pytest.raises(ValueError, match="verdict"):
        _make_result(verdict="WRONG")

    for v in ("PENDING", "REJECT", "WARN", "MARGINAL", "ACCEPT"):
        cfg = _make_result(verdict=v)
        assert cfg.verdict == v


def test_result_negative_trades_raises() -> None:
    """Negative total_trades raises; zero is allowed."""
    with pytest.raises(ValueError, match="negative"):
        _make_result(total_trades=-1)

    cfg = _make_result(total_trades=0)
    assert cfg.total_trades == 0


def test_to_dict_serializes_datetimes_to_iso() -> None:
    """to_dict converts datetime fields to ISO-8601 strings."""
    result = _make_result()
    d = result.to_dict()

    assert isinstance(d["started_at"], str)
    assert "T" in d["started_at"]
    assert isinstance(d["ended_at"], str)
    assert "T" in d["ended_at"]
    assert d["run_id"] == "abc"


def test_to_dict_preserves_none_values() -> None:
    """to_dict preserves None metric values and emits all 30 fields."""
    result = _make_result()
    d = result.to_dict()

    assert d["sharpe"] is None
    assert d["total_return"] is None
    assert d["max_dd_pct"] is None
    assert d["total_trades"] == 0
    assert d["is_statistically_significant"] is False
    assert "verdict" in d and d["verdict"] == "PENDING"
    assert len(d) == 30


def test_result_frozen() -> None:
    """BacktestResult is frozen — mutation must raise FrozenInstanceError."""
    result = _make_result()

    with pytest.raises(FrozenInstanceError):
        result.verdict = "ACCEPT"  # type: ignore[misc]
