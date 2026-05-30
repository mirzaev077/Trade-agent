"""
F3-3: Daily summary generator.

Compact Telegram message for an end-of-day report (default: today UTC).
Sister to weekly_report.py but text-only (no HTML), shorter scope:
PnL, trades, win rate, profit factor, max intraday drawdown, best setup.

Scheduling (Friday 23:00 UTC) is wired by main.py / F4 scheduler — this
module is the pure content generator.

CLI:
    python -m apps.api.src.agents.trader.analysis.daily_summary \
        --balance 10000 [--date 2026-05-30] [--csv PATH] [--symbol XAUUSD]
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from . import performance as perf
from . import weekly_report as wr


# ---------------------------------------------------------------------------
# Config + Result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DailySummaryConfig:
    starting_balance: float
    summary_date: date | None = None       # default: today UTC
    csv_path: str | Path = wr._DEFAULT_CSV
    symbol: str | None = None

    def __post_init__(self) -> None:
        if self.starting_balance <= 0:
            raise ValueError(
                f"starting_balance must be > 0 (got {self.starting_balance})"
            )


@dataclass
class DailySummaryResult:
    telegram_text: str
    summary_date: date
    trade_count: int
    pnl: float
    win_rate: float
    profit_factor: float
    max_drawdown_pct: float
    ending_balance: float

    def to_dict(self) -> dict:
        return {
            "telegram_text": self.telegram_text,
            "summary_date": self.summary_date.isoformat(),
            "trade_count": self.trade_count,
            "pnl": round(self.pnl, 2),
            "win_rate": round(self.win_rate, 4),
            "profit_factor": round(self.profit_factor, 4),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "ending_balance": round(self.ending_balance, 2),
        }


# ---------------------------------------------------------------------------
# Telegram text builder
# ---------------------------------------------------------------------------

def build_telegram_text(
    report: perf.PerformanceReport,
    summary_date: date,
    starting_balance: float,
    ending_balance: float,
    trades: list[dict] | None = None,
) -> str:
    """Compact HTML-formatted daily summary message.

    ``trades`` (optional) gives accurate W/L/BE breakdown; without it we fall
    back to win_rate-derived counts which lump break-evens with losses.
    """
    date_str = summary_date.strftime("%d.%m.%Y")

    if report.total_trades == 0:
        return (
            f"<b>KUNLIK HISOBOT</b> · {date_str}\n"
            "Bugun trade yo'q."
        )

    pnl = report.total_pnl
    pnl_pct = (
        (ending_balance - starting_balance) / starting_balance * 100
        if starting_balance > 0
        else 0.0
    )
    pnl_sign = "+" if pnl >= 0 else ""
    pct_sign = "+" if pnl_pct >= 0 else ""

    if trades is not None:
        wins = sum(1 for t in trades if float(t.get("pnl") or 0) > 0)
        losses = sum(1 for t in trades if float(t.get("pnl") or 0) < 0)
        be = report.total_trades - wins - losses
        wl_str = f"{wins}W / {losses}L"
        if be:
            wl_str += f" / {be}BE"
    else:
        wins = round(report.win_rate * report.total_trades)
        losses = report.total_trades - wins
        wl_str = f"{wins}W / {losses}L"

    lines = [
        f"<b>KUNLIK HISOBOT</b> · {date_str}",
        f"Trades : {report.total_trades} ({wl_str})",
        f"WR     : <b>{report.win_rate * 100:.1f}%</b>",
        f"PnL    : <b>{pnl_sign}${pnl:.2f}</b> ({pct_sign}{pnl_pct:.2f}%)",
        f"PF     : {report.profit_factor:.2f}",
        f"DD     : {report.max_dd_pct:.2f}% (intraday)",
    ]

    # Best setup
    best_setup = None
    best_pnl = None
    for name, stats in report.setup_breakdown.items():
        if best_setup is None or stats["total_pnl"] > best_pnl:
            best_setup = name
            best_pnl = stats["total_pnl"]
    if best_setup is not None:
        bd = report.setup_breakdown[best_setup]
        lines.append(
            f"🎯 Best setup: {best_setup} ({bd['total_pnl']:+.2f}$, {bd['count']} tr)"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# generate()
# ---------------------------------------------------------------------------

def generate(config: DailySummaryConfig) -> DailySummaryResult:
    """One-shot daily summary. Pure: no disk writes, no Telegram side-effects."""
    target_date = config.summary_date or datetime.now(timezone.utc).date()
    window_start = datetime.combine(target_date, time.min, tzinfo=timezone.utc)
    window_end = window_start + timedelta(days=1)

    all_trades = wr.load_trades_from_csv(config.csv_path, symbol=config.symbol)
    today_trades = wr.filter_window(all_trades, window_start, window_end)

    equity_curve = wr.build_equity_curve(config.starting_balance, today_trades)
    ending_balance = float(equity_curve[-1]["equity"])

    report = perf.compute(
        today_trades,
        equity_curve,
        starting_balance=config.starting_balance,
        run_id=f"daily_{target_date.isoformat()}",
        sharpe_periods_per_year=252,
    )

    telegram_text = build_telegram_text(
        report,
        target_date,
        config.starting_balance,
        ending_balance,
        trades=today_trades,
    )

    return DailySummaryResult(
        telegram_text=telegram_text,
        summary_date=target_date,
        trade_count=report.total_trades,
        pnl=report.total_pnl,
        win_rate=report.win_rate,
        profit_factor=report.profit_factor,
        max_drawdown_pct=report.max_dd_pct,
        ending_balance=ending_balance,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="daily_summary",
        description="Compact end-of-day report (PnL / WR / PF / intraday DD)",
    )
    p.add_argument("--balance", type=float, required=True,
                   help="Starting balance at day open (USD)")
    p.add_argument("--date", type=str, default=None,
                   help="Date YYYY-MM-DD (default: today UTC)")
    p.add_argument("--csv", type=str, default=str(wr._DEFAULT_CSV),
                   help="Path to trades.csv")
    p.add_argument("--symbol", type=str, default=None,
                   help="Filter trades by symbol (e.g. XAUUSD)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    if args.date:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError as e:
            print(f"error: invalid --date (use YYYY-MM-DD): {e}", file=sys.stderr)
            return 2
    else:
        target_date = None

    cfg = DailySummaryConfig(
        starting_balance=args.balance,
        summary_date=target_date,
        csv_path=args.csv,
        symbol=args.symbol,
    )
    result = generate(cfg)
    print(result.telegram_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
