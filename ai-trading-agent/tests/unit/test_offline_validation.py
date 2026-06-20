"""F4 Phase B — offline validator orchestration.

run_backtest is mocked everywhere so these stay fast (no real backtests). We
verify: the metrics adapter maps PerformanceReport's *_ratio/*_pct names onto
what the validators read; the production config matches the v5rr sweep; the
engine factory threads windows through run_backtest; and walk-forward /
Monte-Carlo orchestration produce well-formed results.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import apps.api.src.agents.trader.analysis.offline_validation as ov

_UTC = timezone.utc


def _perf(**kw):
    base = dict(total_trades=50, win_rate=0.5, profit_factor=1.2,
                sharpe_ratio=1.0, max_dd_pct=5.0, total_return_pct=10.0)
    base.update(kw)
    return SimpleNamespace(**base)


def _arts(perf=None, closed_trades=None):
    return SimpleNamespace(
        perf_report=perf or _perf(),
        closed_trades=closed_trades if closed_trades is not None else [],
    )


# ── adapter ───────────────────────────────────────────────────────────────────

class TestMetricsAdapter:
    def test_maps_ratio_and_pct_names(self) -> None:
        ns = ov._metrics_ns(_perf(sharpe_ratio=1.7, total_return_pct=12.5))
        assert ns.sharpe == 1.7          # sharpe_ratio -> sharpe
        assert ns.total_return == 12.5   # total_return_pct -> total_return
        assert ns.profit_factor == 1.2
        assert ns.total_trades == 50

    def test_none_fields_coerce_to_zero(self) -> None:
        ns = ov._metrics_ns(_perf(profit_factor=None, sharpe_ratio=None))
        assert ns.profit_factor == 0.0
        assert ns.sharpe == 0.0


# ── production config ───────────────────────────────────────────────────────

class TestProductionParams:
    def test_matches_v5rr_sweep(self) -> None:
        p = ov.production_params("XAUUSD", Path("data/historical"))
        assert p.regime_atr_threshold == 0.18
        assert p.min_rr == 1.0
        assert "H1_OTE" in p.block_setup and "M15_PDH" in p.block_setup
        assert len(p.block_setup) == 14

    def test_overrides(self) -> None:
        p = ov.production_params("XAUUSD", Path("d"), min_rr=1.5, regime_atr_threshold=0.0)
        assert p.min_rr == 1.5
        assert p.regime_atr_threshold == 0.0


# ── engine factory ────────────────────────────────────────────────────────────

def test_factory_calls_run_backtest_per_window(monkeypatch) -> None:
    calls = []

    def fake_run(start, end, params, **kw):
        calls.append((start, end))
        return _arts(_perf(sharpe_ratio=2.0))

    monkeypatch.setattr(ov, "run_backtest", fake_run)
    factory = ov.build_engine_factory(ov.production_params("XAUUSD", Path("d")))
    s = datetime(2024, 1, 1, tzinfo=_UTC)
    e = datetime(2024, 4, 1, tzinfo=_UTC)
    m = factory(s, e)
    assert calls == [(s, e)]
    assert m.sharpe == 2.0


# ── walk-forward orchestration ──────────────────────────────────────────────

def test_run_walk_forward_threads_windows(monkeypatch) -> None:
    seen = []

    def fake_run(start, end, params, **kw):
        seen.append((start.date(), end.date()))
        return _arts(_perf())

    monkeypatch.setattr(ov, "run_backtest", fake_run)
    res = ov.run_walk_forward(
        ov.production_params("XAUUSD", Path("d")),
        datetime(2024, 1, 1, tzinfo=_UTC),
        datetime(2024, 7, 1, tzinfo=_UTC),
        train_months=3, test_months=1, step_months=1,
    )
    # >=1 window, each window = 2 runs (train + test)
    assert len(res.windows) >= 1
    assert len(seen) == 2 * len(res.windows)
    assert isinstance(res.verdict, str)
    assert res.to_dict()["n_windows"] == len(res.windows)


# ── monte-carlo orchestration ───────────────────────────────────────────────

def test_run_monte_carlo_uses_closed_trade_pnls(monkeypatch) -> None:
    trades = [{"pnl": 1.0}, {"pnl": -0.5}, {"pnl": 2.0}, {"pnl": -1.0}, {"pnl": 0.5}]

    def fake_run(start, end, params, **kw):
        return _arts(closed_trades=trades)

    monkeypatch.setattr(ov, "run_backtest", fake_run)
    res = ov.run_monte_carlo(
        ov.production_params("XAUUSD", Path("d")),
        datetime(2024, 1, 1, tzinfo=_UTC),
        datetime(2024, 4, 1, tzinfo=_UTC),
        n_simulations=200,
    )
    assert res.n_trades == len(trades)
    assert 0.0 <= res.probability_of_ruin <= 1.0
    assert res.n_simulations == 200


def test_run_monte_carlo_empty_trades_safe(monkeypatch) -> None:
    monkeypatch.setattr(ov, "run_backtest", lambda *a, **k: _arts(closed_trades=[]))
    res = ov.run_monte_carlo(
        ov.production_params("XAUUSD", Path("d")),
        datetime(2024, 1, 1, tzinfo=_UTC),
        datetime(2024, 4, 1, tzinfo=_UTC),
        n_simulations=200,
    )
    assert res.n_trades == 0
    assert res.probability_of_ruin == 0.0


# ── CLI ───────────────────────────────────────────────────────────────────────

class TestCLI:
    def test_defaults_are_production(self) -> None:
        ns = ov._parse_args(["--start", "2024-01-01", "--end", "2026-01-01",
                             "--data-path", "data/historical"])
        assert ns.regime_atr_threshold == 0.18
        assert ns.min_rr == 1.0
        assert ns.symbol == "XAUUSD"

    def test_skip_flags(self) -> None:
        ns = ov._parse_args(["--start", "2024-01-01", "--end", "2026-01-01",
                             "--data-path", "d", "--skip-walk-forward"])
        assert ns.skip_walk_forward and not ns.skip_monte_carlo

    def test_telegram_flag_defaults(self) -> None:
        ns = ov._parse_args(["--start", "2024-01-01", "--end", "2026-01-01",
                             "--data-path", "d"])
        assert ns.telegram is False
        assert ns.overfit_threshold == ov.OVERFIT_WARN_THRESHOLD


# ── F4-2: monthly Telegram alert builder ────────────────────────────────────────

def _report(overfit: float, verdict: str = "WARN") -> dict:
    return {
        "window": {"start": "2025-01-01T00:00:00+00:00", "end": "2026-01-01T00:00:00+00:00"},
        "walk_forward": {
            "verdict": verdict, "avg_overfit_score": overfit,
            "oos_sharpe": 0.16, "oos_profit_factor": 1.17, "oos_max_dd_pct": 2.3,
            "n_windows": 6, "oos_total_trades": 1500,
        },
        "monte_carlo": {"probability_of_ruin": 0.0, "worst_case_dd_pct": 2.34},
        "risk_calibration": {"verdict": "OK", "recommended_risk_pct": 0.46},
    }


class TestValidationAlert:
    def test_no_warning_when_overfit_below_threshold(self) -> None:
        is_warn, text = ov.build_validation_alert(_report(0.30))
        assert is_warn is False
        assert "OK" in text
        assert "✅" in text

    def test_warns_when_overfit_above_threshold(self) -> None:
        is_warn, text = ov.build_validation_alert(_report(0.75))
        assert is_warn is True
        assert "OGOHLANTIRISH" in text
        assert "⚠️" in text

    def test_warns_on_reject_even_if_overfit_low(self) -> None:
        is_warn, _ = ov.build_validation_alert(_report(0.10, verdict="REJECT"))
        assert is_warn is True

    def test_threshold_is_strict(self) -> None:
        # exactly at threshold → not a warning (uses >, not >=)
        is_warn, _ = ov.build_validation_alert(_report(0.6))
        assert is_warn is False

    def test_custom_threshold(self) -> None:
        is_warn, _ = ov.build_validation_alert(_report(0.45), overfit_threshold=0.4)
        assert is_warn is True

    def test_surfaces_mc_and_calib_sections(self) -> None:
        _, text = ov.build_validation_alert(_report(0.30))
        assert "p_ruin" in text
        assert "Risk calib" in text

    def test_handles_missing_sections(self) -> None:
        # Only walk-forward present (MC + calib skipped) — must not crash.
        rep = {"window": {"start": "2025-01-01", "end": "2026-01-01"},
               "walk_forward": {"verdict": "WARN", "avg_overfit_score": 0.2}}
        is_warn, text = ov.build_validation_alert(rep)
        assert is_warn is False
        assert "Walk-forward" in text
