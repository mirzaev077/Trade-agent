"""
F4-2 acceptance: WalkForwardValidator — window construction, overfit score,
verdict, aggregation, edge cases.

Real `BacktestEngine` chaqirilmaydi — `engine_factory` callable mock qilinadi.
Bu test'ning yagona maqsadi — walk-forward logikasini izolyatsiyada tekshirish.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from apps.api.src.agents.trader.analysis.walk_forward import (
    WalkForwardConfig,
    WalkForwardValidator,
    WalkForwardWindow,
    WindowMetrics,
    build_windows,
    calc_overfit_score,
)
from apps.api.src.agents.trader.engine.result import BacktestResult


def _utc(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=timezone.utc)


def _mock_result(
    *,
    sharpe: float = 1.0,
    pf: float = 1.5,
    wr: float = 55.0,
    total_trades: int = 50,
    max_dd: float = 5.0,
) -> BacktestResult:
    return BacktestResult(
        run_id="test",
        started_at=_utc(2026, 1, 1),
        ended_at=_utc(2026, 1, 2),
        sharpe=sharpe,
        profit_factor=pf,
        win_rate=wr,
        total_trades=total_trades,
        max_dd_pct=max_dd,
    )


# ── Config validation ─────────────────────────────────────────────────────────


def test_config_rejects_non_tz_aware():
    with pytest.raises(ValueError, match="tz-aware"):
        WalkForwardConfig(
            start=datetime(2024, 1, 1),  # naive
            end=_utc(2025, 1, 1),
        )


def test_config_rejects_end_before_start():
    with pytest.raises(ValueError, match="end > start"):
        WalkForwardConfig(start=_utc(2025, 1, 1), end=_utc(2024, 1, 1))


def test_config_rejects_non_positive_months():
    with pytest.raises(ValueError, match="> 0"):
        WalkForwardConfig(
            start=_utc(2024, 1, 1), end=_utc(2025, 1, 1), train_months=0
        )


# ── Window construction ──────────────────────────────────────────────────────


def test_build_windows_basic_12_1_1():
    """24 oy data, train=12, test=1, step=1 → 12 oyna kutiladi
    (2025-01..2025-12 testlar)."""
    cfg = WalkForwardConfig(
        start=_utc(2024, 1, 1),
        end=_utc(2026, 1, 1),
        train_months=12,
        test_months=1,
        step_months=1,
    )
    ws = build_windows(cfg)
    assert len(ws) == 12
    # 1-oyna: train 2024-01..2025-01, test 2025-01..2025-02
    assert ws[0].train_start == _utc(2024, 1, 1)
    assert ws[0].train_end == _utc(2025, 1, 1)
    assert ws[0].test_start == _utc(2025, 1, 1)
    assert ws[0].test_end == _utc(2025, 2, 1)
    # Oxirgi: test_end ≤ end
    assert ws[-1].test_end == _utc(2026, 1, 1)


def test_build_windows_no_data_returns_empty():
    """Train + test > data => bo'sh."""
    cfg = WalkForwardConfig(
        start=_utc(2024, 1, 1),
        end=_utc(2024, 6, 1),  # 5 oy
        train_months=12,
        test_months=1,
    )
    assert build_windows(cfg) == []


def test_build_windows_step_3_months():
    cfg = WalkForwardConfig(
        start=_utc(2024, 1, 1),
        end=_utc(2026, 1, 1),
        train_months=12,
        test_months=3,
        step_months=3,
    )
    ws = build_windows(cfg)
    # cursor: Jan, Apr, Jul, Oct 2024; Jan 2025
    #   test_end: Apr, Jul, Oct 2025; Jan 2026; Apr 2026 (rejected — > end)
    assert len(ws) == 4
    assert ws[0].test_end == _utc(2025, 4, 1)
    assert ws[-1].test_end == _utc(2026, 1, 1)


def test_build_windows_handles_month_end_clamp():
    """31-yanvar + 1 oy = 28/29-fevral (last-day clamping)."""
    cfg = WalkForwardConfig(
        start=_utc(2024, 1, 31),
        end=_utc(2025, 6, 1),
        train_months=1,
        test_months=1,
        step_months=1,
    )
    ws = build_windows(cfg)
    assert ws  # eng kamida bittasi
    # 2024 leap year — Feb 29
    assert ws[0].train_end == _utc(2024, 2, 29)


# ── Overfit score formula ────────────────────────────────────────────────────


def test_calc_overfit_score_no_overfit():
    assert calc_overfit_score(train_sharpe=1.0, test_sharpe=1.0) == 0.0


def test_calc_overfit_score_serious_overfit():
    # Train 2.0, Test 0.5 → (2-0.5)/2 = 0.75
    assert calc_overfit_score(train_sharpe=2.0, test_sharpe=0.5) == pytest.approx(0.75)


def test_calc_overfit_score_test_beats_train_clipped_to_zero():
    """Test > train bo'lsa — manfiy emas, 0 qaytadi."""
    assert calc_overfit_score(train_sharpe=1.0, test_sharpe=2.0) == 0.0


def test_calc_overfit_score_negative_train_returns_zero():
    assert calc_overfit_score(train_sharpe=-0.5, test_sharpe=0.3) == 0.0


# ── End-to-end run() ─────────────────────────────────────────────────────────


def _make_factory(window_results: dict[tuple, BacktestResult]):
    """Window key (train_start, train_end) yoki (test_start, test_end) -> result.

    Test uchun: factory har chaqiruvni log qiladi va kerakli result qaytaradi.
    """
    call_log: list[tuple] = []

    def factory(start: datetime, end: datetime) -> BacktestResult:
        call_log.append((start, end))
        key = (start, end)
        if key in window_results:
            return window_results[key]
        return _mock_result(sharpe=1.0, pf=1.5, wr=55.0, total_trades=50, max_dd=5.0)

    factory.call_log = call_log  # type: ignore[attr-defined]
    return factory


def test_run_calls_factory_for_each_train_and_test():
    cfg = WalkForwardConfig(
        start=_utc(2024, 1, 1),
        end=_utc(2025, 3, 1),
        train_months=12,
        test_months=1,
    )
    factory = _make_factory({})
    result = WalkForwardValidator(cfg, factory).run()

    n_windows = len(result.windows)
    assert n_windows == 2  # 2025-01-02 va 2025-02-03 testlar
    # Har oyna uchun 2 ta chaqiruv (train + test)
    assert len(factory.call_log) == n_windows * 2


def test_run_accept_verdict_when_strategy_solid():
    cfg = WalkForwardConfig(
        start=_utc(2024, 1, 1),
        end=_utc(2025, 3, 1),
        train_months=12,
        test_months=1,
    )
    # Train va test bir xil yaxshi metrics — overfit = 0, Sharpe = 1.2
    good = _mock_result(sharpe=1.2, pf=1.6, wr=58.0, total_trades=50, max_dd=4.0)
    factory = MagicMock(return_value=good)

    result = WalkForwardValidator(cfg, factory).run()
    assert result.verdict.startswith("ACCEPT")
    assert result.avg_overfit_score == 0.0
    assert result.oos_sharpe == pytest.approx(1.2)


def test_run_reject_on_serious_overfit():
    cfg = WalkForwardConfig(
        start=_utc(2024, 1, 1),
        end=_utc(2025, 3, 1),
        train_months=12,
        test_months=1,
    )

    # Sequence: train returned high, test returned low → overfit huge
    train_res = _mock_result(sharpe=2.0, pf=2.5, wr=70.0, total_trades=60, max_dd=3.0)
    test_res = _mock_result(sharpe=0.3, pf=1.0, wr=50.0, total_trades=40, max_dd=10.0)

    call_count = {"n": 0}

    def factory(start, end):
        call_count["n"] += 1
        # Odd calls = train, even = test (in our loop: train first, then test)
        return train_res if call_count["n"] % 2 == 1 else test_res

    result = WalkForwardValidator(cfg, factory).run()
    assert result.verdict.startswith("REJECT")
    # avg_overfit = (2-0.3)/2 = 0.85
    assert result.avg_overfit_score == pytest.approx(0.85)


def test_run_reject_when_sharpe_below_0_5():
    cfg = WalkForwardConfig(
        start=_utc(2024, 1, 1),
        end=_utc(2025, 3, 1),
        train_months=12,
        test_months=1,
    )
    bad = _mock_result(sharpe=0.2, pf=0.9, wr=45.0, total_trades=50, max_dd=8.0)
    factory = MagicMock(return_value=bad)

    result = WalkForwardValidator(cfg, factory).run()
    assert result.verdict.startswith("REJECT")
    assert "Sharpe" in result.verdict


def test_run_warn_when_insufficient_trades():
    cfg = WalkForwardConfig(
        start=_utc(2024, 1, 1),
        end=_utc(2025, 3, 1),
        train_months=12,
        test_months=1,
    )
    sparse = _mock_result(sharpe=1.2, pf=1.6, wr=58.0, total_trades=5, max_dd=4.0)
    factory = MagicMock(return_value=sparse)

    result = WalkForwardValidator(cfg, factory).run()
    assert result.verdict.startswith("WARN")
    assert "statistik" in result.verdict.lower() or "trade" in result.verdict.lower()


def test_run_marginal_verdict():
    cfg = WalkForwardConfig(
        start=_utc(2024, 1, 1),
        end=_utc(2025, 3, 1),
        train_months=12,
        test_months=1,
    )
    # Sharpe 0.5-1.0, no overfit, DD OK, trades enough
    mid = _mock_result(sharpe=0.7, pf=1.2, wr=53.0, total_trades=80, max_dd=6.0)
    factory = MagicMock(return_value=mid)

    result = WalkForwardValidator(cfg, factory).run()
    assert result.verdict.startswith("MARGINAL")


def test_run_aggregates_oos_trades():
    cfg = WalkForwardConfig(
        start=_utc(2024, 1, 1),
        end=_utc(2025, 3, 1),  # 2 windows
        train_months=12,
        test_months=1,
    )
    factory = MagicMock(
        return_value=_mock_result(
            sharpe=1.0, pf=1.5, wr=55.0, total_trades=50, max_dd=5.0
        )
    )
    result = WalkForwardValidator(cfg, factory).run()
    # 2 ta test window × 50 trade = 100
    assert result.oos_total_trades == 100


def test_run_empty_windows_raises():
    cfg = WalkForwardConfig(
        start=_utc(2024, 1, 1),
        end=_utc(2024, 6, 1),
        train_months=12,
        test_months=1,
    )
    with pytest.raises(ValueError, match="bo'sh"):
        WalkForwardValidator(cfg, MagicMock()).run()


def test_to_dict_serializable():
    cfg = WalkForwardConfig(
        start=_utc(2024, 1, 1),
        end=_utc(2025, 3, 1),
        train_months=12,
        test_months=1,
    )
    factory = MagicMock(return_value=_mock_result())
    result = WalkForwardValidator(cfg, factory).run()
    d = result.to_dict()

    import json
    json.dumps(d)  # JSON serializable bo'lishi shart

    assert d["n_windows"] == len(result.windows)
    assert d["verdict"] == result.verdict
    assert "config" in d
