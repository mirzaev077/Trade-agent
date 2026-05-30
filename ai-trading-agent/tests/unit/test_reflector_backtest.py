"""
F4-3 acceptance: ReflectorBacktester — config validation, verdict logic,
end-to-end run_comparison() with mock engine_factory.

Real BacktestEngine chaqirilmaydi.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from apps.api.src.agents.trader.analysis.reflector_backtest import (
    ReflectorABConfig,
    ReflectorABResult,
    ReflectorBacktester,
)
from apps.api.src.agents.trader.engine.result import BacktestResult


def _utc(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=timezone.utc)


def _mock_result(
    *,
    sharpe: float = 1.0,
    pf: float = 1.5,
    total_return: float = 5.0,
    max_dd: float = 5.0,
    total_trades: int = 50,
) -> BacktestResult:
    return BacktestResult(
        run_id="test",
        started_at=_utc(2026, 1, 1),
        ended_at=_utc(2026, 1, 2),
        sharpe=sharpe,
        profit_factor=pf,
        total_return=total_return,
        max_dd_pct=max_dd,
        total_trades=total_trades,
    )


# ── Config validation ────────────────────────────────────────────────────────


def test_config_rejects_naive_datetime():
    with pytest.raises(ValueError, match="tz-aware"):
        ReflectorABConfig(start=datetime(2024, 1, 1), end=_utc(2025, 1, 1))


def test_config_rejects_end_before_start():
    with pytest.raises(ValueError, match="end > start"):
        ReflectorABConfig(start=_utc(2025, 1, 1), end=_utc(2024, 1, 1))


def test_config_rejects_invalid_sharpe_thresholds():
    with pytest.raises(ValueError, match="sharpe"):
        ReflectorABConfig(
            start=_utc(2024, 1, 1),
            end=_utc(2025, 1, 1),
            sharpe_harm_threshold=1.5,
            sharpe_improvement_threshold=0.5,
        )


def test_config_rejects_invalid_dd_thresholds():
    with pytest.raises(ValueError, match="dd"):
        ReflectorABConfig(
            start=_utc(2024, 1, 1),
            end=_utc(2025, 1, 1),
            dd_tolerance=0.5,  # < 1.0
        )


# ── run_comparison() factory calls ───────────────────────────────────────────


def test_run_comparison_calls_factory_twice():
    cfg = ReflectorABConfig(start=_utc(2024, 1, 1), end=_utc(2025, 1, 1))
    factory = MagicMock(return_value=_mock_result())
    ReflectorBacktester(cfg, factory).run_comparison()
    assert factory.call_count == 2
    # Birinchi baseline (False), ikkinchi treatment (True)
    args1 = factory.call_args_list[0]
    args2 = factory.call_args_list[1]
    assert args1.args[2] is False
    assert args2.args[2] is True


# ── Verdict: BENEFICIAL ──────────────────────────────────────────────────────


def test_verdict_beneficial_when_sharpe_15pct_better():
    cfg = ReflectorABConfig(start=_utc(2024, 1, 1), end=_utc(2025, 1, 1))
    baseline = _mock_result(sharpe=1.0, max_dd=5.0, total_trades=50)
    treatment = _mock_result(sharpe=1.2, max_dd=5.0, total_trades=50)
    factory = MagicMock(side_effect=[baseline, treatment])
    res = ReflectorBacktester(cfg, factory).run_comparison()
    assert res.verdict.startswith("BENEFICIAL")
    assert res.sharpe_improvement == pytest.approx(0.2)


def test_verdict_beneficial_when_baseline_sharpe_negative():
    cfg = ReflectorABConfig(start=_utc(2024, 1, 1), end=_utc(2025, 1, 1))
    baseline = _mock_result(sharpe=-0.3, max_dd=8.0, total_trades=50)
    treatment = _mock_result(sharpe=0.8, max_dd=5.0, total_trades=50)
    factory = MagicMock(side_effect=[baseline, treatment])
    res = ReflectorBacktester(cfg, factory).run_comparison()
    assert res.verdict.startswith("BENEFICIAL")


# ── Verdict: HARMFUL ─────────────────────────────────────────────────────────


def test_verdict_harmful_when_sharpe_drops_below_threshold():
    cfg = ReflectorABConfig(start=_utc(2024, 1, 1), end=_utc(2025, 1, 1))
    baseline = _mock_result(sharpe=1.0, max_dd=5.0, total_trades=50)
    treatment = _mock_result(sharpe=0.5, max_dd=5.0, total_trades=50)
    factory = MagicMock(side_effect=[baseline, treatment])
    res = ReflectorBacktester(cfg, factory).run_comparison()
    assert res.verdict.startswith("HARMFUL")
    assert "Sharpe" in res.verdict


def test_verdict_harmful_when_dd_worsens():
    cfg = ReflectorABConfig(start=_utc(2024, 1, 1), end=_utc(2025, 1, 1))
    baseline = _mock_result(sharpe=1.0, max_dd=5.0, total_trades=50)
    treatment = _mock_result(sharpe=1.05, max_dd=7.0, total_trades=50)  # +40% DD
    factory = MagicMock(side_effect=[baseline, treatment])
    res = ReflectorBacktester(cfg, factory).run_comparison()
    assert res.verdict.startswith("HARMFUL")
    assert "DD" in res.verdict


# ── Verdict: NEUTRAL ─────────────────────────────────────────────────────────


def test_verdict_neutral_small_difference():
    cfg = ReflectorABConfig(start=_utc(2024, 1, 1), end=_utc(2025, 1, 1))
    baseline = _mock_result(sharpe=1.0, max_dd=5.0, total_trades=50)
    treatment = _mock_result(sharpe=1.05, max_dd=5.1, total_trades=50)
    factory = MagicMock(side_effect=[baseline, treatment])
    res = ReflectorBacktester(cfg, factory).run_comparison()
    assert res.verdict.startswith("NEUTRAL")


def test_verdict_neutral_when_both_bad_sharpe():
    cfg = ReflectorABConfig(start=_utc(2024, 1, 1), end=_utc(2025, 1, 1))
    baseline = _mock_result(sharpe=-0.2, max_dd=10.0, total_trades=50)
    treatment = _mock_result(sharpe=-0.1, max_dd=9.0, total_trades=50)
    factory = MagicMock(side_effect=[baseline, treatment])
    res = ReflectorBacktester(cfg, factory).run_comparison()
    assert res.verdict.startswith("NEUTRAL")


# ── Verdict: WARN (insufficient trades) ──────────────────────────────────────


def test_verdict_warn_when_trades_too_few_baseline():
    cfg = ReflectorABConfig(
        start=_utc(2024, 1, 1),
        end=_utc(2025, 1, 1),
        min_trades_for_significance=30,
    )
    baseline = _mock_result(sharpe=1.0, total_trades=5)
    treatment = _mock_result(sharpe=1.5, total_trades=50)
    factory = MagicMock(side_effect=[baseline, treatment])
    res = ReflectorBacktester(cfg, factory).run_comparison()
    assert res.verdict.startswith("WARN")
    assert "Statistik" in res.verdict


def test_verdict_warn_when_trades_too_few_treatment():
    cfg = ReflectorABConfig(start=_utc(2024, 1, 1), end=_utc(2025, 1, 1))
    baseline = _mock_result(total_trades=50)
    treatment = _mock_result(total_trades=10)
    factory = MagicMock(side_effect=[baseline, treatment])
    res = ReflectorBacktester(cfg, factory).run_comparison()
    assert res.verdict.startswith("WARN")


# ── Aggregate diffs ──────────────────────────────────────────────────────────


def test_improvements_calculated_correctly():
    cfg = ReflectorABConfig(start=_utc(2024, 1, 1), end=_utc(2025, 1, 1))
    baseline = _mock_result(
        sharpe=1.0, pf=1.5, total_return=5.0, max_dd=10.0, total_trades=50
    )
    treatment = _mock_result(
        sharpe=1.4, pf=1.8, total_return=7.5, max_dd=8.0, total_trades=50
    )
    factory = MagicMock(side_effect=[baseline, treatment])
    res = ReflectorBacktester(cfg, factory).run_comparison()

    assert res.sharpe_improvement == pytest.approx(0.4)
    assert res.pf_improvement == pytest.approx(0.3)
    assert res.return_improvement_pct == pytest.approx(2.5)
    assert res.dd_reduction_pct == pytest.approx(2.0)


def test_handles_none_metrics_safely():
    cfg = ReflectorABConfig(start=_utc(2024, 1, 1), end=_utc(2025, 1, 1))
    baseline = BacktestResult(
        run_id="b",
        started_at=_utc(2026, 1, 1),
        ended_at=_utc(2026, 1, 2),
        total_trades=50,  # sharpe/pf/etc None
    )
    treatment = BacktestResult(
        run_id="t",
        started_at=_utc(2026, 1, 1),
        ended_at=_utc(2026, 1, 2),
        total_trades=50,
    )
    factory = MagicMock(side_effect=[baseline, treatment])
    res = ReflectorBacktester(cfg, factory).run_comparison()
    # Crash bo'lmasin; default 0.0
    assert res.baseline_sharpe == 0.0
    assert res.treatment_sharpe == 0.0


# ── to_dict ──────────────────────────────────────────────────────────────────


def test_to_dict_json_serializable():
    cfg = ReflectorABConfig(start=_utc(2024, 1, 1), end=_utc(2025, 1, 1))
    baseline = _mock_result(sharpe=1.0)
    treatment = _mock_result(sharpe=1.3)
    factory = MagicMock(side_effect=[baseline, treatment])
    res = ReflectorBacktester(cfg, factory).run_comparison()
    d = res.to_dict()

    import json
    json.dumps(d)

    assert d["verdict"] == res.verdict
    assert d["baseline"]["sharpe"] == 1.0
    assert d["treatment"]["sharpe"] == 1.3
    assert d["improvements"]["sharpe"] == pytest.approx(0.3)
