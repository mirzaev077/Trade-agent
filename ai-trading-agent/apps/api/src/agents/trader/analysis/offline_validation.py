"""Offline validation job (F4 Phase B) — heavy backtest validators.

walk-forward + Monte-Carlo + risk calibration over the production config, run
OFFLINE (NOT in the live MT5 loop) because each needs historical re-runs of the
engine. Wire this to a monthly Windows Task Scheduler / cron job; it writes a
combined JSON report.

F4-4 risk calibration: the Monte-Carlo worst-case drawdown is translated into a
recommended ``risk_per_trade`` via sizing-independent R-multiples, measured
against the ``RiskConfig.max_drawdown`` budget. Advisory only — no auto-mutation
(F1-1 approval-gate philosophy). See ``risk_calibration.py``.

reflector_backtest is intentionally EXCLUDED: the backtest engine uses a stub
reflector (no real treatment), so an A/B comparison would run two identical
backtests → NEUTRAL. It becomes meaningful only once the reflector is wired into
the engine; until then it would be a misleading no-op.

The validators read engine results through a thin adapter because the raw
``BacktestResult`` carries None for the headline metrics (serialization gap) and
because the validators read ``.sharpe`` / ``.total_return`` while
``PerformanceReport`` exposes ``.sharpe_ratio`` / ``.total_return_pct``.

CLI (production config = v5rr by default):
    python -m apps.api.src.agents.trader.analysis.offline_validation \
        --start 2024-01-01 --end 2026-01-01 --symbol XAUUSD \
        --data-path data/historical/ --out reports/validation/
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from loguru import logger

from apps.api.src.agents.trader.analysis import monte_carlo as mc
from apps.api.src.agents.trader.analysis import risk_calibration as rc
from apps.api.src.agents.trader.analysis import walk_forward as wf
from apps.api.src.agents.trader.engine.runner import BacktestParams, run_backtest
from apps.api.src.agents.trader.models.config import RiskConfig

# Live RiskConfig defaults — the Monte-Carlo worst-case DD calibrates *these*.
_RISK_DEFAULTS = RiskConfig()
DEFAULT_DD_BUDGET_PCT = _RISK_DEFAULTS.max_drawdown      # 10.0% session DD breaker
DEFAULT_CURRENT_RISK_PCT = _RISK_DEFAULTS.risk_per_trade  # 1.0% per trade

# ── Production strategy config (= v5rr) ───────────────────────────────────────
# The v3/v4 CLI blocklist, kept here as the single in-code source of truth so the
# offline job validates exactly what the run_v5rr.ps1 sweep validated. H1_OB,
# M15_OTE, M15_DR_Eq come free via ICTAnalyst.DEFAULT_BLOCKED_SETUPS.
PRODUCTION_BLOCK_EXTRAS: tuple[str, ...] = (
    "H1_BEAR", "H1_BULL", "M15_BEAR", "H1_DR_Eq", "H4_DR_Eq", "H4_RB",
    "M15_RB", "M15_SSL_sweep", "M15_BULL", "H1_BSL_sweep",
    "M15_BSL_sweep", "H1_SSL_sweep", "H1_OTE", "M15_PDH",
)
PRODUCTION_REGIME_ATR = 0.18
PRODUCTION_MIN_RR = 1.0

# F4-2: walk-forward overfit score above this → Telegram OGOHLANTIRISH.
OVERFIT_WARN_THRESHOLD = 0.6


def production_params(
    symbol: str,
    data_path: Path,
    *,
    regime_atr_threshold: float = PRODUCTION_REGIME_ATR,
    min_rr: float = PRODUCTION_MIN_RR,
    initial_balance: float = 10_000.0,
) -> BacktestParams:
    return BacktestParams(
        symbol=symbol,
        data_path=data_path,
        block_setup=PRODUCTION_BLOCK_EXTRAS,
        regime_atr_threshold=regime_atr_threshold,
        regime_atr_period=14,
        min_rr=min_rr,
        initial_balance=initial_balance,
    )


# ── Engine-factory adapter ────────────────────────────────────────────────────

def _metrics_ns(perf_report) -> SimpleNamespace:
    """Map a PerformanceReport to the attribute names the validators read.

    walk_forward._to_metrics → total_trades, win_rate, profit_factor, sharpe,
    max_dd_pct. reflector (unused here) also reads total_return. Mapping the
    *_ratio / *_pct names here is exactly the glue the validators expect.
    """
    return SimpleNamespace(
        total_trades=int(getattr(perf_report, "total_trades", 0) or 0),
        win_rate=float(getattr(perf_report, "win_rate", 0.0) or 0.0),
        profit_factor=float(getattr(perf_report, "profit_factor", 0.0) or 0.0),
        sharpe=float(getattr(perf_report, "sharpe_ratio", 0.0) or 0.0),
        max_dd_pct=float(getattr(perf_report, "max_dd_pct", 0.0) or 0.0),
        total_return=float(getattr(perf_report, "total_return_pct", 0.0) or 0.0),
    )


def build_engine_factory(
    params: BacktestParams, *, progress_every_bars: int = 0,
) -> Callable[[datetime, datetime], SimpleNamespace]:
    """Return an `engine_factory(start, end) -> metrics` for WalkForwardValidator.

    Each call runs a full backtest over the window with the production wiring and
    returns the adapted metrics namespace.
    """
    def factory(start: datetime, end: datetime) -> SimpleNamespace:
        logger.info("  [factory] backtest {} -> {}", start.date(), end.date())
        arts = run_backtest(start, end, params, progress_every_bars=progress_every_bars)
        return _metrics_ns(arts.perf_report)
    return factory


# ── Orchestration ─────────────────────────────────────────────────────────────

def run_walk_forward(
    params: BacktestParams,
    start: datetime,
    end: datetime,
    *,
    train_months: int = 6,
    test_months: int = 3,
    step_months: int = 3,
    progress_every_bars: int = 0,
) -> wf.WalkForwardResult:
    cfg = wf.WalkForwardConfig(
        start=start, end=end,
        train_months=train_months, test_months=test_months, step_months=step_months,
    )
    factory = build_engine_factory(params, progress_every_bars=progress_every_bars)
    return wf.WalkForwardValidator(cfg, factory).run()


def monte_carlo_from_trades(
    closed_trades: list,
    *,
    n_simulations: int,
    initial_balance: float,
) -> mc.MonteCarloResult:
    """Realized per-trade PnL → bootstrap Monte-Carlo (pure; no backtest)."""
    pnls = [float(t.get("pnl") or 0.0) for t in closed_trades]
    cfg = mc.MonteCarloConfig(
        n_simulations=n_simulations, initial_balance=initial_balance,
    )
    return mc.MonteCarloSimulator(cfg).run(pnls)


def run_monte_carlo(
    params: BacktestParams,
    start: datetime,
    end: datetime,
    *,
    n_simulations: int = 10_000,
    progress_every_bars: int = 0,
) -> mc.MonteCarloResult:
    """One full-period backtest → realized per-trade PnL → bootstrap MC."""
    arts = run_backtest(start, end, params, progress_every_bars=progress_every_bars)
    return monte_carlo_from_trades(
        arts.closed_trades,
        n_simulations=n_simulations,
        initial_balance=params.initial_balance,
    )


def run_risk_calibration(
    params: BacktestParams,
    start: datetime,
    end: datetime,
    *,
    dd_budget_pct: float = DEFAULT_DD_BUDGET_PCT,
    current_risk_pct: float = DEFAULT_CURRENT_RISK_PCT,
    n_simulations: int = 10_000,
    progress_every_bars: int = 0,
) -> rc.RiskCalibrationResult:
    """F4-4: one backtest → R-multiples → worst-case DD → risk_per_trade tavsiya.

    The DD budget defaults to ``RiskConfig.max_drawdown`` and the current risk to
    ``RiskConfig.risk_per_trade`` — i.e. the Monte-Carlo worst-case DD calibrates
    the live risk lever directly. Does NOT mutate config (advisory only)."""
    arts = run_backtest(start, end, params, progress_every_bars=progress_every_bars)
    cfg = rc.RiskCalibrationConfig(
        dd_budget_pct=dd_budget_pct,
        current_risk_pct=current_risk_pct,
        n_simulations=n_simulations,
    )
    return rc.calibrate(arts.closed_trades, cfg)


# ── F4-2: monthly Telegram alert ───────────────────────────────────────────────

def build_validation_alert(
    report: dict, *, overfit_threshold: float = OVERFIT_WARN_THRESHOLD,
) -> tuple[bool, str]:
    """Pure: validation report dict → ``(is_warning, telegram_text)``.

    F4-2 rule: walk-forward ``avg_overfit_score > overfit_threshold`` OR a REJECT
    verdict raises a warning. Monte-Carlo and risk-calibration sections are
    surfaced when present. Pure + side-effect-free so the threshold logic is
    unit-testable without running any backtest.
    """
    wf = report.get("walk_forward") or {}
    overfit = float(wf.get("avg_overfit_score", 0.0) or 0.0)
    wf_verdict = str(wf.get("verdict", "N/A"))
    overfit_hot = overfit > overfit_threshold
    is_warning = overfit_hot or wf_verdict == "REJECT"

    win = report.get("window") or {}
    head = "⚠️ Oylik validatsiya — OGOHLANTIRISH" if is_warning else "✅ Oylik validatsiya — OK"
    lines = [head, f"Oyna: {win.get('start', '?')[:10]} → {win.get('end', '?')[:10]}", ""]

    if wf:
        warn_tag = f"  ⚠️ > {overfit_threshold}" if overfit_hot else ""
        lines += [
            f"Walk-forward: {wf_verdict}",
            f"  Overfit: {overfit:.2f}{warn_tag}",
            f"  OOS Sharpe {float(wf.get('oos_sharpe', 0.0)):.2f} | "
            f"PF {float(wf.get('oos_profit_factor', 0.0)):.2f} | "
            f"DD {float(wf.get('oos_max_dd_pct', 0.0)):.1f}%",
            f"  Oyna={wf.get('n_windows', 0)}  OOS savdo={wf.get('oos_total_trades', 0)}",
        ]

    mc = report.get("monte_carlo") or {}
    if mc:
        lines.append(
            f"Monte-Carlo: p_ruin={float(mc.get('probability_of_ruin', 0.0)):.3f}  "
            f"worst-DD={float(mc.get('worst_case_dd_pct', 0.0)):.1f}%"
        )

    calib = report.get("risk_calibration") or {}
    if calib:
        lines.append(
            f"Risk calib: {calib.get('verdict', 'N/A')}  "
            f"tavsiya risk={float(calib.get('recommended_risk_pct', 0.0)):.2f}%"
        )

    return is_warning, "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_date_utc(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m apps.api.src.agents.trader.analysis.offline_validation",
        description="F4 Phase B offline validators: walk-forward + Monte-Carlo.",
    )
    p.add_argument("--start", required=True, help="ISO date (UTC).")
    p.add_argument("--end", required=True, help="ISO date (UTC, exclusive).")
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--data-path", required=True)
    p.add_argument("--out", default="reports/validation",
                   help="Directory for the combined JSON report.")
    p.add_argument("--initial-balance", type=float, default=10_000.0)
    p.add_argument("--regime-atr-threshold", type=float, default=PRODUCTION_REGIME_ATR)
    p.add_argument("--min-rr", type=float, default=PRODUCTION_MIN_RR)
    p.add_argument("--train-months", type=int, default=6)
    p.add_argument("--test-months", type=int, default=3)
    p.add_argument("--step-months", type=int, default=3)
    p.add_argument("--mc-sims", type=int, default=10_000)
    p.add_argument("--calib-sims", type=int, default=10_000,
                   help="Monte-Carlo sims for the risk calibration R-multiple pass.")
    p.add_argument("--dd-budget-pct", type=float, default=DEFAULT_DD_BUDGET_PCT,
                   help="Worst-case DD ceiling for risk calibration (= RiskConfig.max_drawdown).")
    p.add_argument("--current-risk-pct", type=float, default=DEFAULT_CURRENT_RISK_PCT,
                   help="Live risk_per_trade to project worst-case DD against.")
    p.add_argument("--progress-every-bars", type=int, default=2000)
    p.add_argument("--skip-walk-forward", action="store_true")
    p.add_argument("--skip-monte-carlo", action="store_true")
    p.add_argument("--skip-risk-calibration", action="store_true")
    p.add_argument("--telegram", action="store_true",
                   help="F4-2: send a Telegram summary (warns if overfit>0.6 / REJECT) "
                        "after the report is written. Reads token/chat from .env.")
    p.add_argument("--overfit-threshold", type=float, default=OVERFIT_WARN_THRESHOLD,
                   help="Walk-forward overfit score that triggers the Telegram warning.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    start = _parse_date_utc(args.start)
    end = _parse_date_utc(args.end)
    data_path = Path(args.data_path)
    if not data_path.exists():
        logger.error("data path does not exist: {}", data_path)
        return 2

    params = production_params(
        args.symbol, data_path,
        regime_atr_threshold=args.regime_atr_threshold,
        min_rr=args.min_rr,
        initial_balance=args.initial_balance,
    )

    report: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "config": {
            "symbol": args.symbol,
            "block_extras": list(PRODUCTION_BLOCK_EXTRAS),
            "regime_atr_threshold": args.regime_atr_threshold,
            "min_rr": args.min_rr,
            "dd_budget_pct": args.dd_budget_pct,
            "current_risk_pct": args.current_risk_pct,
        },
    }

    if not args.skip_walk_forward:
        logger.info("=== Walk-forward (train {}m / test {}m / step {}m) ===",
                    args.train_months, args.test_months, args.step_months)
        wf_res = run_walk_forward(
            params, start, end,
            train_months=args.train_months, test_months=args.test_months,
            step_months=args.step_months, progress_every_bars=args.progress_every_bars,
        )
        report["walk_forward"] = wf_res.to_dict()
        logger.info("Walk-forward verdict: {}", wf_res.verdict)
        logger.info("  OOS sharpe={:.2f} pf={:.2f} dd={:.2f}% trades={} avg_overfit={:.2f}",
                    wf_res.oos_sharpe, wf_res.oos_profit_factor, wf_res.oos_max_dd_pct,
                    wf_res.oos_total_trades, wf_res.avg_overfit_score)

    # Monte-Carlo and risk calibration both derive from the realized trade set, so
    # run ONE backtest over the full period and feed its closed trades to both.
    if not args.skip_monte_carlo or not args.skip_risk_calibration:
        logger.info("=== Backtest (full period) for Monte-Carlo + risk calibration ===")
        arts = run_backtest(
            start, end, params, progress_every_bars=args.progress_every_bars,
        )
        closed = arts.closed_trades

        if not args.skip_monte_carlo:
            logger.info("=== Monte-Carlo ({} sims) ===", args.mc_sims)
            mc_res = monte_carlo_from_trades(
                closed, n_simulations=args.mc_sims,
                initial_balance=params.initial_balance,
            )
            report["monte_carlo"] = mc_res.to_dict()
            logger.info("Monte-Carlo: p_ruin={:.3f}  final_eq p5/p50/p95=${:.0f}/${:.0f}/${:.0f}  "
                        "worst_dd={:.1f}%  p95_losing_streak={}",
                        mc_res.probability_of_ruin, mc_res.final_equity_p5,
                        mc_res.final_equity_p50, mc_res.final_equity_p95,
                        mc_res.worst_case_dd_pct, mc_res.expected_max_losing_streak_p95)

        if not args.skip_risk_calibration:
            logger.info("=== Risk calibration (DD budget {:.1f}%, current risk {:.2f}%) ===",
                        args.dd_budget_pct, args.current_risk_pct)
            calib_cfg = rc.RiskCalibrationConfig(
                dd_budget_pct=args.dd_budget_pct,
                current_risk_pct=args.current_risk_pct,
                n_simulations=args.calib_sims,
            )
            calib_res = rc.calibrate(closed, calib_cfg)
            report["risk_calibration"] = calib_res.to_dict()
            logger.info("Risk calibration: verdict={}  valid_trades={}  worst_dd@1%={:.2f}%  "
                        "projected_worst_dd@{:.2f}%={:.1f}%  recommended_risk={:.2f}%",
                        calib_res.verdict, calib_res.n_trades_valid,
                        calib_res.worst_dd_per_1pct, calib_res.current_risk_pct,
                        calib_res.projected_worst_dd_pct, calib_res.recommended_risk_pct)
            logger.info("  {}", calib_res.notes)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"validation_{start.date()}_{end.date()}.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    logger.info("Report written -> {}", out_path)
    print(f"validation_report: {out_path}")

    # F4-2: monthly Telegram alert (⚠️ on overfit>threshold / REJECT).
    if args.telegram:
        is_warning, text = build_validation_alert(
            report, overfit_threshold=args.overfit_threshold,
        )
        try:
            from apps.api.src.agents.trader.models.config import TradingConfig
            from apps.api.src.agents.trader.utils import telegram_bot as tg
            cfg = TradingConfig()
            tg.init(cfg.telegram_bot_token, cfg.telegram_chat_id)
            tg.send_sync(text)
            logger.info("Telegram alert sent (warning={})", is_warning)
        except Exception as e:  # noqa: BLE001 — alert must never fail the job
            logger.error("Telegram alert failed: {}", e)

    return 0


if __name__ == "__main__":
    sys.exit(main())
