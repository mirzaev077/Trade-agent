"""
F2-2.F: Backtest CLI entry point.

Invocation::

    python -m apps.api.src.agents.trader.engine \\
        --start 2024-01-01 --end 2024-06-30 \\
        --symbol XAUUSD --data-path data/historical/

For self-testing without real candle files, pass ``--synthetic``::

    python -m apps.api.src.agents.trader.engine \\
        --start 2024-01-01 --end 2024-01-08 \\
        --symbol XAUUSD --synthetic

The synthetic generator writes random-walk M15/H1/H4 CSV files to a temp
directory and feeds them through the standard ``HistoricalDataManager`` →
``BacktestEngine`` path.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger

from apps.api.src.agents.trader.analysis import performance as perf
from apps.api.src.agents.trader.analysis import report_html
from apps.api.src.agents.trader.core.broker import BrokerConfig
from apps.api.src.agents.trader.core.clock import VirtualClock, set_clock
from apps.api.src.agents.trader.core.data import HistoricalDataManager
from apps.api.src.agents.trader.engine.analyst_ict import ICTAnalyst
from apps.api.src.agents.trader.engine.regime import (
    DEFAULT_REGIME_BLOCKED_SETUPS,
    RegimeGate,
)
from apps.api.src.agents.trader.engine.config import BacktestConfig
from apps.api.src.agents.trader.engine.config import RiskConfig as EngineRiskConfig
from apps.api.src.agents.trader.engine.engine import BacktestEngine
from apps.api.src.agents.trader.engine.result import BacktestResult


# ── Synthetic data generator (used by --synthetic) ───────────────────────────


_TIMEFRAME_FREQ: dict[str, str] = {
    "M15": "15min",
    "H1": "1h",
    "H4": "4h",
}


def _make_synthetic_candles(
    start: datetime,
    end: datetime,
    freq: str,
    base: float = 2000.0,
    seed: int = 42,
) -> pd.DataFrame:
    """Random-walk OHLCV candles between ``start`` and ``end``."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start=start, end=end, freq=freq, inclusive="left")
    n = len(idx)
    if n == 0:
        return pd.DataFrame(
            columns=["timestamp", "open", "high", "low", "close", "tick_volume"]
        )

    returns = rng.normal(0, 0.0015, n)
    close = base * np.exp(np.cumsum(returns))
    open_ = np.roll(close, 1)
    open_[0] = base
    body = np.abs(rng.normal(0, 0.0005, n))
    high = np.maximum(open_, close) * (1 + body)
    low = np.minimum(open_, close) * (1 - body)
    vol = rng.integers(100, 1000, n)

    df = pd.DataFrame(
        {
            "timestamp": idx,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "tick_volume": vol,
        }
    )
    return df


def _generate_synthetic_data(symbol: str, start: datetime, end: datetime) -> Path:
    """Generate M15/H1/H4 CSVs into a fresh temp dir; return the path."""
    out = Path(tempfile.mkdtemp(prefix=f"synth_{symbol}_"))
    # Pad warmup so HistoricalDataManager._warmup load works (30 days backstop)
    warmup_start = start - timedelta(days=35)
    for tf, freq in _TIMEFRAME_FREQ.items():
        df = _make_synthetic_candles(warmup_start, end, freq=freq, seed=42 + len(tf))
        if df.empty:
            continue
        df.to_csv(out / f"{symbol}_{tf}.csv", index=False)
    logger.info("Synthetic data written to {}", out)
    return out


# ── CLI helpers ──────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m apps.api.src.agents.trader.engine",
        description="Run an OpenClaw backtest against historical or synthetic data.",
    )
    p.add_argument(
        "--start",
        required=True,
        help="Backtest start date, ISO format YYYY-MM-DD (interpreted as UTC).",
    )
    p.add_argument(
        "--end",
        required=True,
        help="Backtest end date, ISO format YYYY-MM-DD (interpreted as UTC).",
    )
    p.add_argument("--symbol", required=True, help="Trading symbol, e.g. XAUUSD.")
    p.add_argument(
        "--data-path",
        default=None,
        help=(
            "Path to directory containing {SYMBOL}_{TF}.parquet or .csv files. "
            "Required unless --synthetic is given."
        ),
    )
    p.add_argument(
        "--primary-timeframe",
        default="M15",
        help="Primary bar step (default: M15).",
    )
    p.add_argument(
        "--initial-balance",
        type=float,
        default=10000.0,
        help="Starting balance USD (default: 10000.0).",
    )
    p.add_argument(
        "--risk-per-trade",
        type=float,
        default=1.0,
        help="Risk per trade as percent (default: 1.0).",
    )
    p.add_argument(
        "--report-path",
        default="reports/",
        help="Directory where the result JSON is written (default: reports/).",
    )
    p.add_argument(
        "--synthetic",
        action="store_true",
        help="Generate synthetic XAUUSD-ish data instead of reading --data-path.",
    )
    p.add_argument(
        "--html-report",
        action="store_true",
        help="F2-3: generate reports/<run_id>.html alongside the JSON.",
    )
    p.add_argument(
        "--progress-every-bars",
        type=int,
        default=1000,
        help=(
            "Engine'da har N bar'da progress log emit qiladi (default: 1000). "
            "0 yoki manfiy → log o'chiriladi."
        ),
    )
    p.add_argument(
        "--block-setup",
        action="append",
        default=[],
        metavar="LABEL",
        help=(
            "Block a setup label (e.g. H1_DR_Eq) in addition to the analyst's "
            "DEFAULT_BLOCKED_SETUPS. Repeatable. Diagnostic use: isolate a "
            "loser setup without code changes."
        ),
    )
    p.add_argument(
        "--regime-atr-threshold",
        type=float,
        default=0.0,
        metavar="PCT",
        help=(
            "F4 regime gate: block the core reversion setups (M15_OB/BB/CISD by "
            "default) when M15 ATR%% (ATR/price*100) exceeds PCT. <=0 disables "
            "(default), reproducing v3. Calibrated band 0.14-0.20; 0.18 blocks "
            "~50%% of high-vol bars and <15%% of normal-vol bars."
        ),
    )
    p.add_argument(
        "--regime-atr-period",
        type=int,
        default=14,
        metavar="N",
        help=(
            "Regime gate ATR lookback for the volatility measurement (default "
            "14, same window as ICTAnalysis._atr). Affects the gate's high-vol "
            "decision ONLY — zone sizing is unchanged."
        ),
    )
    p.add_argument(
        "--regime-block-setup",
        action="append",
        default=[],
        metavar="LABEL",
        help=(
            "Setup label to block while the regime is high-vol (repeatable). "
            "If omitted while --regime-atr-threshold>0, defaults to "
            "M15_OB, M15_BB, M15_CISD."
        ),
    )
    p.add_argument(
        "--min-rr",
        type=float,
        default=0.0,
        metavar="RR",
        help=(
            "Structural reward:risk floor. Drop any zone whose planned "
            "|tp1-entry|/|entry-sl| < RR before routing. <=0 disables (default), "
            "reproducing prior runs byte-for-byte. ~1.0 culls the fragile low-RR "
            "mean-reversion zones that drive the 2025-H2 bleed."
        ),
    )
    p.add_argument(
        "--dump-trades",
        metavar="PATH",
        default=None,
        help=(
            "Write every closed trade (entry_time, setup_type, direction, "
            "entry_price, pnl, session, ...) to PATH as CSV. Enables per-trade "
            "post-hoc analysis (e.g. high-vol vs normal-vol PnL by setup) that "
            "the aggregate setup_breakdown cannot answer."
        ),
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    return p.parse_args(argv)


# Scalar, analysis-relevant fields of a journal closed-trade row (drops the
# nested signal/risk_check/reflection blobs that are not CSV-friendly).
_TRADE_DUMP_FIELDS = (
    "ticket", "entry_time", "exit_time", "setup_type", "direction",
    "entry_price", "sl", "tp", "lot", "pnl", "session", "day_of_week",
    "close_reason",
)


def _dump_trades(closed_trades: list[dict], path: str) -> int:
    """Write closed-trade rows to ``path`` as CSV. Datetimes → ISO 8601.

    Returns the number of rows written. Returns 0 (and writes only a header)
    for an empty trade list. Used by ``--dump-trades`` for per-trade diagnostics.
    """
    import csv

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(_TRADE_DUMP_FIELDS), extrasaction="ignore")
        w.writeheader()
        for t in closed_trades:
            row = {k: t.get(k) for k in _TRADE_DUMP_FIELDS}
            for tk in ("entry_time", "exit_time"):
                v = row.get(tk)
                if hasattr(v, "isoformat"):
                    row[tk] = v.isoformat()
            w.writerow(row)
            n += 1
    return n


def _parse_date_utc(s: str) -> datetime:
    """Parse YYYY-MM-DD as midnight UTC. Also accepts full ISO datetimes."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _print_config_summary(config: BacktestConfig, data_path: Path) -> None:
    print("=" * 70)
    print("OpenClaw Backtest")
    print("=" * 70)
    print(f"  run_id            : {config.run_id}")
    print(f"  symbol            : {config.symbol}")
    print(f"  start             : {config.start.isoformat()}")
    print(f"  end               : {config.end.isoformat()}")
    duration_days = (config.end - config.start).total_seconds() / 86400.0
    print(f"  duration_days     : {duration_days:.2f}")
    print(f"  primary_timeframe : {config.primary_timeframe}")
    print(f"  timeframes        : {list(config.timeframes)}")
    print(f"  data_path         : {data_path}")
    print(f"  initial_balance   : {config.broker.initial_balance:.2f} USD")
    print(f"  risk_per_trade    : {config.risk_config.risk_per_trade_pct:.2f}%")
    print(f"  contract_size     : {config.broker.contract_size}")
    print(f"  commission_per_lot: {config.broker.commission_per_lot:.2f}")
    print("=" * 70)


def _print_summary(
    result: BacktestResult,
    broker_summary: dict,
    perf_report=None,
) -> None:
    print("-" * 70)
    print("Backtest finished")
    print("-" * 70)
    print(f"  run_id        : {result.run_id}")
    # F2-3: prefer perf_report verdict if computed
    verdict = perf_report.verdict if perf_report else result.verdict
    print(f"  verdict       : {verdict}")
    print(f"  total_trades  : {result.total_trades}")

    if perf_report:
        wr_pct = perf_report.win_rate * 100
        ci_lo = perf_report.win_rate_ci_low * 100
        ci_hi = perf_report.win_rate_ci_high * 100
        print(f"  win_rate      : {wr_pct:.2f}%  (95% CI: [{ci_lo:.1f}%, {ci_hi:.1f}%])")
        print(f"  profit_factor : {perf_report.profit_factor:.2f}")
        print(f"  avg_rr        : {perf_report.avg_rr:.2f}")
        print(f"  max_dd_pct    : {perf_report.max_dd_pct:.2f}%")
        print(f"  sharpe        : {perf_report.sharpe_ratio:.2f}")
        print(f"  total_return  : {perf_report.total_return_pct:+.2f}%")
        print(f"  total_pnl     : {perf_report.total_pnl:+.2f} USD")
    else:
        # Fallback for back-compat
        win_rate = broker_summary.get("win_rate_pct", 0.0)
        total_pnl = broker_summary.get("total_pnl", 0.0)
        print(f"  win_rate      : {win_rate:.2f}%")
        print(f"  total_pnl     : {total_pnl:.2f} USD")

    balance = broker_summary.get("balance", 0.0)
    equity = broker_summary.get("equity", 0.0)
    print(f"  final_balance : {balance:.2f} USD")
    print(f"  final_equity  : {equity:.2f} USD")

    if perf_report and perf_report.setup_breakdown:
        print("  setup breakdown:")
        for setup, stats in perf_report.setup_breakdown.items():
            wr_s = float(stats.get("win_rate", 0)) * 100
            pf_s = float(stats.get("profit_factor", 0))
            pnl_s = float(stats.get("total_pnl", 0))
            cnt = int(stats.get("count", 0))
            print(f"    {setup:>12}: {cnt:3d} trades, WR={wr_s:.1f}%, PF={pf_s:.2f}, PnL=${pnl_s:+.2f}")
    print("-" * 70)


def _save_result(result: BacktestResult, report_dir: Path, perf_report=None) -> Path:
    """Write result JSON. If perf_report is provided, merge its fields into the output."""
    report_dir.mkdir(parents=True, exist_ok=True)
    out_path = report_dir / f"{result.run_id}.json"
    payload = result.to_dict()
    if perf_report is not None:
        payload["performance"] = perf_report.to_dict()
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
    return out_path


# ── Main ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Logging
    logger.remove()
    level = "DEBUG" if args.verbose else "INFO"
    logger.add(sys.stderr, level=level)

    start = _parse_date_utc(args.start)
    end = _parse_date_utc(args.end)

    if args.synthetic:
        data_path = _generate_synthetic_data(args.symbol, start, end)
    else:
        if args.data_path is None:
            logger.error("--data-path is required unless --synthetic is given")
            return 2
        data_path = Path(args.data_path)
        if not data_path.exists():
            logger.error("data path does not exist: {}", data_path)
            return 2

    broker_cfg = BrokerConfig(
        initial_balance=args.initial_balance,
        commission_per_lot=3.0,
        slippage_model="fixed",
        swap_rates={},
        fixed_slippage_pips=1.0,
        leverage=100,
        contract_size=100,  # XAUUSD default
    )
    risk_cfg = EngineRiskConfig(risk_per_trade_pct=args.risk_per_trade)

    primary = args.primary_timeframe
    if primary == "M15":
        timeframes: tuple[str, ...] = ("M15", "H1", "H4")
    elif primary == "H1":
        timeframes = ("H1", "H4")
    elif primary == "H4":
        timeframes = ("H4",)
    else:
        timeframes = (primary,)

    config = BacktestConfig(
        start=start,
        end=end,
        symbol=args.symbol,
        data_path=data_path,
        broker=broker_cfg,
        risk_config=risk_cfg,
        primary_timeframe=primary,
        timeframes=timeframes,
    )

    _print_config_summary(config, data_path)

    # Wire global clock (production code paths consult get_clock()).
    clock = VirtualClock(start=start, end=end)
    set_clock(clock)

    # Preload candles into the data manager that the engine will share.
    data = HistoricalDataManager(data_path=data_path)
    data.preload(args.symbol, timeframes=list(timeframes))

    # F2-3.1 diagnostic: union CLI --block-setup labels with analyst defaults.
    blocked = set(ICTAnalyst.DEFAULT_BLOCKED_SETUPS) | set(args.block_setup)
    if args.block_setup:
        logger.info("Blocked setups (defaults + CLI): {}", sorted(blocked))

    # F4 regime gate: block core reversion setups in high realised volatility.
    # Disabled unless --regime-atr-threshold > 0 (so default run == v3).
    regime = None
    if args.regime_atr_threshold and args.regime_atr_threshold > 0:
        regime_blocked = (
            frozenset(args.regime_block_setup) if args.regime_block_setup
            else DEFAULT_REGIME_BLOCKED_SETUPS
        )
        regime = RegimeGate(
            atr_threshold=args.regime_atr_threshold,
            atr_period=args.regime_atr_period,
            blocked_setups=regime_blocked,
        )
        logger.info(
            "Regime gate ON: block {} when M15 ATR% > {} (period {})",
            sorted(regime.blocked_setups), regime.atr_threshold, regime.atr_period,
        )

    if args.min_rr and args.min_rr > 0:
        logger.info("Min-RR floor ON: drop zones with planned RR < {}", args.min_rr)

    analyst = ICTAnalyst(
        clock=clock, data=data, blocked_setups=blocked, regime=regime,
        min_rr=args.min_rr,
    )

    # NB: BacktestEngine constructs its OWN clock + data manager internally
    # from `config`. We pass the analyst that already references our preloaded
    # data; the analyst.data and engine.data are independent instances pointed
    # at the same data_path, which is fine for read-only access.
    engine = BacktestEngine(
        config,
        analyst=analyst,
        progress_every_bars=args.progress_every_bars,
    )
    # Make the analyst use the engine's clock + data so the analyst reads
    # the same simulated time as the engine's loop.
    analyst.clock = engine.clock
    analyst.data = engine.data

    logger.info("Starting backtest run {}", config.run_id)
    result = engine.run()

    broker_summary = engine.broker.get_summary()

    # F2-3.1: Use journal trade rows (have setup_type/sl/session) rather than
    # broker.history (ClosedTrade dataclass — no setup_type/sl). Without this,
    # setup_breakdown collapses to "UNKNOWN" and avg_rr is always 0.0.
    if hasattr(engine.journal, "get_closed_trades"):
        closed_trades = engine.journal.get_closed_trades()
    else:
        closed_trades = list(engine.broker.history)
    equity_curve = engine.journal.get_equity_curve() if hasattr(engine.journal, "get_equity_curve") else []

    if args.dump_trades:
        n = _dump_trades(closed_trades, args.dump_trades)
        logger.info("Dumped {} closed trades -> {}", n, args.dump_trades)
        print(f"  trades_csv    : {args.dump_trades} ({n} rows)")

    perf_report = perf.compute(
        closed_trades=closed_trades,
        equity_curve=equity_curve,
        starting_balance=float(config.broker.initial_balance),
        run_id=config.run_id,
    )

    _print_summary(result, broker_summary, perf_report)

    out_path = _save_result(result, Path(args.report_path), perf_report)
    print(f"  report_json   : {out_path}")

    # F2-3: HTML report (optional)
    if args.html_report:
        cfg_summary = {
            "symbol":            config.symbol,
            "start":             config.start.isoformat(),
            "end":               config.end.isoformat(),
            "primary_timeframe": config.primary_timeframe,
            "initial_balance":   f"${config.broker.initial_balance:.2f}",
            "risk_per_trade":    f"{config.risk_config.risk_per_trade_pct:.2f}%",
        }
        html_str = report_html.render(perf_report, equity_curve, closed_trades, cfg_summary)
        html_path = Path(args.report_path) / f"{config.run_id}.html"
        report_html.save(html_str, html_path)
        print(f"  report_html   : {html_path}")

    print("-" * 70)

    # Restore production clock so subsequent imports don't see the virtual one.
    set_clock(None)

    return 0


if __name__ == "__main__":
    sys.exit(main())
