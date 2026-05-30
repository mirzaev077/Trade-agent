"""
F4-1: Weekly report generator.

Reads `trades.csv` (live trade journal), filters trades within a configurable
date window (default last 7 days), reuses `analysis/performance.compute()`
for metrics + `analysis/report_html.render()` for HTML, and emits a compact
Telegram summary text.

Entrypoints:
    generate(config) -> WeeklyReportResult
    CLI:  python -m apps.api.src.agents.trader.analysis.weekly_report \
              --balance 10000 [--end 2026-05-30] [--days 7] [--csv PATH]

The CLI does NOT auto-send Telegram; a scheduler (F4 deployment phase) will
wire `notify_bot.send(result.telegram_text)` on every Friday 23:00 UTC.

CSV → performance.compute schema mapping:
    entry      -> entry_price
    exit       -> exit_price
    label      -> setup_type
    sl, pnl    -> pass-through

Equity curve is reconstructed from starting_balance + cumulative chronological pnl.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from . import performance as perf
from . import report_html


# Default trades.csv lives in brain/ (see utils/trade_analytics.py)
_DEFAULT_CSV = Path(__file__).resolve().parent.parent / "brain" / "trades.csv"


# ---------------------------------------------------------------------------
# Config + Result dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WeeklyReportConfig:
    """Inputs to generate(). Frozen for safety."""

    starting_balance: float
    window_end: datetime | None = None      # default: now UTC
    window_days: int = 7
    csv_path: str | Path = _DEFAULT_CSV
    output_dir: str | Path = "reports"
    symbol: str | None = None                # filter rows by symbol if set
    sharpe_periods_per_year: int = 252       # daily equity → 252 trading days

    def __post_init__(self) -> None:
        if self.window_days <= 0:
            raise ValueError(f"window_days must be > 0 (got {self.window_days})")
        if self.starting_balance <= 0:
            raise ValueError(
                f"starting_balance must be > 0 (got {self.starting_balance})"
            )


@dataclass
class WeeklyReportResult:
    """Outputs from generate()."""

    report: perf.PerformanceReport
    telegram_text: str
    html_path: str
    window_start: datetime
    window_end: datetime
    trade_count: int
    ending_balance: float

    def to_dict(self) -> dict:
        return {
            "report": self.report.to_dict(),
            "telegram_text": self.telegram_text,
            "html_path": self.html_path,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "trade_count": self.trade_count,
            "ending_balance": round(self.ending_balance, 2),
        }


# ---------------------------------------------------------------------------
# CSV → trade-dict pipeline
# ---------------------------------------------------------------------------

def _parse_csv_timestamp(value: str) -> datetime | None:
    """trades.csv writes '%Y-%m-%d %H:%M:%S' (UTC naive). Coerce to tz-aware UTC."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _safe_float(v: Any, default: float = 0.0) -> float:
    if v in (None, ""):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    """
    Map a trades.csv row to the schema expected by performance.compute() and
    report_html.render(). Keeps all original fields too (preserved for HTML
    monthly table + setup breakdown debug).
    """
    return {
        **row,
        "timestamp": row.get("timestamp", ""),
        "entry_price": _safe_float(row.get("entry")),
        "exit_price": _safe_float(row.get("exit")),
        "sl": _safe_float(row.get("sl")),
        "pnl": _safe_float(row.get("pnl")),
        "setup_type": row.get("label") or "UNKNOWN",
        "session": row.get("session", "unknown") or "unknown",
        "result": row.get("result", ""),
    }


def load_trades_from_csv(
    csv_path: str | Path,
    *,
    symbol: str | None = None,
) -> list[dict[str, Any]]:
    """
    Read all rows from `trades.csv` and normalize. Returns [] if file missing.
    Caller filters by window separately (so the same load can be reused).
    """
    path = Path(csv_path)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if symbol and row.get("symbol") != symbol:
                continue
            out.append(_normalize_row(row))
    return out


def filter_window(
    trades: Iterable[dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
) -> list[dict[str, Any]]:
    """Inclusive of window_start, exclusive of window_end. Skips unparseable rows."""
    out = []
    for t in trades:
        ts = _parse_csv_timestamp(t.get("timestamp", ""))
        if ts is None:
            continue
        if window_start <= ts < window_end:
            out.append(t)
    return out


# ---------------------------------------------------------------------------
# Equity curve from running balance
# ---------------------------------------------------------------------------

def build_equity_curve(
    starting_balance: float,
    trades: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Cumulative balance after each chronological trade. Each point:
        {"timestamp": <ISO str>, "equity": <float>, "drawdown_pct": <float>}

    drawdown_pct is computed against running peak (matches performance.compute
    expectations — see analysis/performance.py:132).
    """
    if not trades:
        return [{
            "timestamp": "",
            "equity": starting_balance,
            "drawdown_pct": 0.0,
        }]

    sorted_trades = sorted(
        trades, key=lambda t: _parse_csv_timestamp(t.get("timestamp", "")) or datetime.min.replace(tzinfo=timezone.utc)
    )
    curve = [{
        "timestamp": "",
        "equity": float(starting_balance),
        "drawdown_pct": 0.0,
    }]
    running = float(starting_balance)
    peak = running
    for t in sorted_trades:
        running += _safe_float(t.get("pnl"))
        peak = max(peak, running)
        dd_pct = (peak - running) / peak * 100 if peak > 0 else 0.0
        curve.append({
            "timestamp": t.get("timestamp", ""),
            "equity": round(running, 2),
            "drawdown_pct": round(dd_pct, 4),
        })
    return curve


# ---------------------------------------------------------------------------
# Session breakdown (Telegram text only)
# ---------------------------------------------------------------------------

def _session_breakdown(trades: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-session win/loss/pnl stats. Used by Telegram text."""
    stats: dict[str, dict[str, Any]] = {}
    for t in trades:
        s = t.get("session") or "unknown"
        bucket = stats.setdefault(s, {"wins": 0, "losses": 0, "pnl": 0.0, "total": 0})
        pnl = _safe_float(t.get("pnl"))
        result = t.get("result", "")
        if result == "win" or pnl > 0:
            bucket["wins"] += 1
        elif result == "loss" or pnl < 0:
            bucket["losses"] += 1
        bucket["pnl"] = round(bucket["pnl"] + pnl, 2)
        bucket["total"] += 1
    return stats


# ---------------------------------------------------------------------------
# Telegram text
# ---------------------------------------------------------------------------

def build_telegram_text(
    report: perf.PerformanceReport,
    window_start: datetime,
    window_end: datetime,
    trades: list[dict[str, Any]],
    starting_balance: float,
    ending_balance: float,
) -> str:
    """
    Compact HTML-formatted Telegram message. Caller passes to
    telegram_bot.send(text, parse_mode='HTML').
    """
    win_range = window_start.strftime("%d.%m") + " – " + window_end.strftime("%d.%m.%Y")

    if report.total_trades == 0:
        return (
            "<b>HAFTALIK HISOBOT</b>\n"
            f"{win_range}\n\n"
            "Bu hafta trade yo'q."
        )

    pnl = report.total_pnl
    pnl_pct = (
        (ending_balance - starting_balance) / starting_balance * 100
        if starting_balance > 0
        else 0.0
    )
    pnl_sign = "+" if pnl >= 0 else ""
    pct_sign = "+" if pnl_pct >= 0 else ""

    # Session winner/loser
    sessions = _session_breakdown(trades)
    best_session = max(sessions, key=lambda k: sessions[k]["pnl"], default=None)
    worst_session = min(sessions, key=lambda k: sessions[k]["pnl"], default=None)

    # Best setup
    best_setup = None
    best_setup_pnl = None
    for name, stats in report.setup_breakdown.items():
        if best_setup is None or stats["total_pnl"] > best_setup_pnl:
            best_setup = name
            best_setup_pnl = stats["total_pnl"]

    lines = [
        f"<b>HAFTALIK HISOBOT</b> &middot; <i>{report.verdict}</i>",
        win_range,
        "",
        f"Trades  : {report.total_trades}",
        f"Win Rate: <b>{report.win_rate * 100:.1f}%</b> "
        f"(CI {report.win_rate_ci_low * 100:.1f}–{report.win_rate_ci_high * 100:.1f})",
        f"PnL     : <b>{pnl_sign}${pnl:.2f}</b> ({pct_sign}{pnl_pct:.2f}%)",
        f"PF      : {report.profit_factor:.2f}",
        f"Sharpe  : {report.sharpe_ratio:.2f}",
        f"Max DD  : {report.max_dd_pct:.2f}% (${report.max_dd_dollar:.2f})",
        f"Avg RR  : {report.avg_rr:.2f}",
    ]
    if best_session is not None:
        lines.append("")
        lines.append(
            f"🏆 Best session : {best_session} "
            f"({sessions[best_session]['pnl']:+.2f}$, {sessions[best_session]['total']} tr)"
        )
    if worst_session is not None and worst_session != best_session:
        lines.append(
            f"❌ Worst session: {worst_session} "
            f"({sessions[worst_session]['pnl']:+.2f}$, {sessions[worst_session]['total']} tr)"
        )
    if best_setup is not None:
        bd = report.setup_breakdown[best_setup]
        lines.append(
            f"🎯 Best setup   : {best_setup} "
            f"({bd['total_pnl']:+.2f}$, {bd['count']} tr, WR {bd['win_rate'] * 100:.1f}%)"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate(config: WeeklyReportConfig) -> WeeklyReportResult:
    """One-shot weekly report. Saves HTML to disk, returns result struct."""
    window_end = config.window_end or datetime.now(timezone.utc)
    if window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=timezone.utc)
    window_start = window_end - timedelta(days=config.window_days)

    all_trades = load_trades_from_csv(config.csv_path, symbol=config.symbol)
    weekly_trades = filter_window(all_trades, window_start, window_end)

    equity_curve = build_equity_curve(config.starting_balance, weekly_trades)
    ending_balance = float(equity_curve[-1]["equity"])

    run_id = f"weekly_{window_end.strftime('%Y-%m-%d')}"
    report = perf.compute(
        weekly_trades,
        equity_curve,
        starting_balance=config.starting_balance,
        run_id=run_id,
        sharpe_periods_per_year=config.sharpe_periods_per_year,
    )

    telegram_text = build_telegram_text(
        report,
        window_start,
        window_end,
        weekly_trades,
        config.starting_balance,
        ending_balance,
    )

    html_str = report_html.render(
        report,
        equity_curve,
        closed_trades=weekly_trades,
        config_summary={
            "window_start": window_start.strftime("%Y-%m-%d %H:%M UTC"),
            "window_end": window_end.strftime("%Y-%m-%d %H:%M UTC"),
            "starting_balance": f"${config.starting_balance:.2f}",
            "symbol": config.symbol or "ALL",
        },
    )

    html_path = Path(config.output_dir) / f"{run_id}.html"
    report_html.save(html_str, html_path)

    return WeeklyReportResult(
        report=report,
        telegram_text=telegram_text,
        html_path=str(html_path),
        window_start=window_start,
        window_end=window_end,
        trade_count=report.total_trades,
        ending_balance=ending_balance,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="weekly_report",
        description="Generate weekly performance report from trades.csv",
    )
    p.add_argument("--balance", type=float, required=True,
                   help="Starting balance at window_start (USD)")
    p.add_argument("--end", type=str, default=None,
                   help="Window end date YYYY-MM-DD (default: now UTC)")
    p.add_argument("--days", type=int, default=7,
                   help="Window length in days (default 7)")
    p.add_argument("--csv", type=str, default=str(_DEFAULT_CSV),
                   help=f"Path to trades.csv (default {_DEFAULT_CSV})")
    p.add_argument("--out", type=str, default="reports",
                   help="Output directory for HTML (default reports/)")
    p.add_argument("--symbol", type=str, default=None,
                   help="Filter trades by symbol (e.g. XAUUSD)")
    p.add_argument("--print-telegram", action="store_true",
                   help="Print Telegram text to stdout after generation")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    # Telegram text contains emojis; reconfigure stdout to UTF-8 on Windows
    # consoles that default to cp1251/cp866. Safe no-op on POSIX.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    if args.end:
        try:
            end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(
                hour=23, minute=0, tzinfo=timezone.utc
            )
        except ValueError as e:
            print(f"error: invalid --end (use YYYY-MM-DD): {e}", file=sys.stderr)
            return 2
    else:
        end_dt = None

    cfg = WeeklyReportConfig(
        starting_balance=args.balance,
        window_end=end_dt,
        window_days=args.days,
        csv_path=args.csv,
        output_dir=args.out,
        symbol=args.symbol,
    )
    result = generate(cfg)

    print(f"HTML:    {result.html_path}")
    print(f"Window:  {result.window_start.date()} -> {result.window_end.date()}")
    print(f"Trades:  {result.trade_count}")
    print(f"Verdict: {result.report.verdict}")
    print(f"PnL:     ${result.report.total_pnl:+.2f}")
    if args.print_telegram:
        print("\n--- Telegram ---")
        print(result.telegram_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
