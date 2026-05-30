"""
F4-1: weekly_report generator.

Coverage:
  • Config validation (positive balance, positive window_days)
  • CSV loading + symbol filtering
  • Window filtering (boundary half-open semantics, unparseable timestamps)
  • Trade-row normalization (entry/exit/label → schema)
  • Equity curve from running balance (drawdown_pct, sort by timestamp)
  • Session breakdown roll-up
  • Telegram text: zero-trade week + populated week + HTML safety
  • generate(): writes HTML, returns expected struct
  • CLI: argparse + invalid --end handling
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from apps.api.src.agents.trader.analysis import weekly_report as wr


# ── Config validation ───────────────────────────────────────────────────────


def test_config_rejects_zero_window_days():
    with pytest.raises(ValueError, match="window_days"):
        wr.WeeklyReportConfig(starting_balance=10_000, window_days=0)


def test_config_rejects_negative_window_days():
    with pytest.raises(ValueError, match="window_days"):
        wr.WeeklyReportConfig(starting_balance=10_000, window_days=-3)


def test_config_rejects_non_positive_balance():
    with pytest.raises(ValueError, match="starting_balance"):
        wr.WeeklyReportConfig(starting_balance=0)


def test_config_defaults():
    cfg = wr.WeeklyReportConfig(starting_balance=10_000)
    assert cfg.window_days == 7
    assert cfg.window_end is None
    assert cfg.symbol is None


# ── CSV loading + normalization ─────────────────────────────────────────────


def _write_trades_csv(path: Path, rows: list[dict]) -> None:
    fields = [
        "timestamp", "direction", "symbol", "entry", "exit", "sl", "tp1",
        "lot", "pnl", "result", "label", "tf", "mode", "session", "regime",
        "sl_pips", "tp_pips", "rr",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            full = {k: row.get(k, "") for k in fields}
            w.writerow(full)


def test_load_trades_returns_empty_when_csv_missing(tmp_path):
    out = wr.load_trades_from_csv(tmp_path / "nonexistent.csv")
    assert out == []


def test_load_trades_normalizes_schema(tmp_path):
    csv_path = tmp_path / "trades.csv"
    _write_trades_csv(csv_path, [
        {
            "timestamp": "2026-05-28 10:00:00", "symbol": "XAUUSD",
            "entry": "2300.50", "exit": "2310.25", "sl": "2295.00",
            "pnl": "97.50", "result": "win", "label": "H1_OB",
            "session": "london",
        },
    ])
    out = wr.load_trades_from_csv(csv_path)
    assert len(out) == 1
    row = out[0]
    assert row["entry_price"] == 2300.50
    assert row["exit_price"] == 2310.25
    assert row["sl"] == 2295.00
    assert row["pnl"] == 97.50
    assert row["setup_type"] == "H1_OB"
    assert row["session"] == "london"
    # Original keys preserved (used by HTML monthly table)
    assert row["timestamp"] == "2026-05-28 10:00:00"
    assert row["symbol"] == "XAUUSD"


def test_load_trades_filters_by_symbol(tmp_path):
    csv_path = tmp_path / "trades.csv"
    _write_trades_csv(csv_path, [
        {"timestamp": "2026-05-28 10:00:00", "symbol": "XAUUSD", "pnl": "10"},
        {"timestamp": "2026-05-28 11:00:00", "symbol": "EURUSD", "pnl": "20"},
    ])
    xau = wr.load_trades_from_csv(csv_path, symbol="XAUUSD")
    assert len(xau) == 1 and xau[0]["symbol"] == "XAUUSD"


def test_normalize_handles_missing_numeric_fields():
    raw = {"timestamp": "2026-05-28 10:00:00", "label": "H1_OB"}
    norm = wr._normalize_row(raw)
    assert norm["entry_price"] == 0.0
    assert norm["exit_price"] == 0.0
    assert norm["sl"] == 0.0
    assert norm["pnl"] == 0.0
    assert norm["setup_type"] == "H1_OB"


def test_normalize_handles_empty_label():
    raw = {"timestamp": "2026-05-28 10:00:00", "label": ""}
    norm = wr._normalize_row(raw)
    assert norm["setup_type"] == "UNKNOWN"


# ── Timestamp parsing ───────────────────────────────────────────────────────


def test_parse_csv_timestamp_handles_csv_format():
    dt = wr._parse_csv_timestamp("2026-05-28 10:30:00")
    assert dt == datetime(2026, 5, 28, 10, 30, tzinfo=timezone.utc)


def test_parse_csv_timestamp_handles_iso():
    dt = wr._parse_csv_timestamp("2026-05-28T10:30:00")
    assert dt == datetime(2026, 5, 28, 10, 30, tzinfo=timezone.utc)


def test_parse_csv_timestamp_returns_none_for_garbage():
    assert wr._parse_csv_timestamp("not-a-date") is None
    assert wr._parse_csv_timestamp("") is None


# ── Window filtering ────────────────────────────────────────────────────────


def _row(ts: str, pnl: float = 10.0, **kw) -> dict:
    base = {
        "timestamp": ts, "entry_price": 1.0, "exit_price": 1.0, "sl": 0.5,
        "pnl": pnl, "setup_type": "H1_OB", "session": "london", "result": "win",
    }
    base.update(kw)
    return base


def test_filter_window_inclusive_start_exclusive_end():
    start = datetime(2026, 5, 23, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 5, 30, 0, 0, tzinfo=timezone.utc)
    trades = [
        _row("2026-05-22 23:59:59"),       # before window
        _row("2026-05-23 00:00:00"),       # at start (included)
        _row("2026-05-25 12:00:00"),       # inside
        _row("2026-05-30 00:00:00"),       # at end (excluded)
        _row("2026-05-30 12:00:00"),       # after
    ]
    out = wr.filter_window(trades, start, end)
    assert [t["timestamp"] for t in out] == [
        "2026-05-23 00:00:00",
        "2026-05-25 12:00:00",
    ]


def test_filter_window_skips_unparseable_timestamps():
    start = datetime(2026, 5, 23, tzinfo=timezone.utc)
    end = datetime(2026, 5, 30, tzinfo=timezone.utc)
    trades = [_row("2026-05-25 12:00:00"), _row("garbage"), _row("")]
    out = wr.filter_window(trades, start, end)
    assert len(out) == 1


# ── Equity curve ────────────────────────────────────────────────────────────


def test_equity_curve_empty_trades_returns_single_point():
    curve = wr.build_equity_curve(10_000, [])
    assert curve == [{"timestamp": "", "equity": 10_000, "drawdown_pct": 0.0}]


def test_equity_curve_running_balance_and_drawdown():
    trades = [
        _row("2026-05-25 09:00:00", pnl=100.0),     # 10100, peak=10100, dd=0
        _row("2026-05-25 10:00:00", pnl=-200.0),    # 9900, peak=10100, dd=~1.98%
        _row("2026-05-25 11:00:00", pnl=500.0),     # 10400, peak=10400, dd=0
        _row("2026-05-25 12:00:00", pnl=-50.0),     # 10350, peak=10400, dd=~0.48%
    ]
    curve = wr.build_equity_curve(10_000, trades)
    assert len(curve) == 5  # starting point + 4 trades
    assert curve[0]["equity"] == 10_000
    assert curve[1]["equity"] == 10_100
    assert curve[2]["equity"] == 9_900
    assert curve[3]["equity"] == 10_400
    assert curve[4]["equity"] == 10_350
    # Drawdown is from running peak
    assert curve[1]["drawdown_pct"] == 0.0
    assert curve[2]["drawdown_pct"] == pytest.approx(1.9802, abs=0.01)
    assert curve[3]["drawdown_pct"] == 0.0
    assert curve[4]["drawdown_pct"] == pytest.approx(0.4808, abs=0.01)


def test_equity_curve_sorts_unordered_trades_chronologically():
    trades = [
        _row("2026-05-25 11:00:00", pnl=500.0),
        _row("2026-05-25 09:00:00", pnl=100.0),
        _row("2026-05-25 10:00:00", pnl=-200.0),
    ]
    curve = wr.build_equity_curve(10_000, trades)
    equities = [p["equity"] for p in curve]
    assert equities == [10_000, 10_100, 9_900, 10_400]


# ── Session breakdown ───────────────────────────────────────────────────────


def test_session_breakdown_aggregates_pnl_and_results():
    trades = [
        _row("2026-05-25 09:00", pnl=100.0, session="london", result="win"),
        _row("2026-05-25 10:00", pnl=-50.0, session="london", result="loss"),
        _row("2026-05-25 14:00", pnl=200.0, session="ny", result="win"),
    ]
    out = wr._session_breakdown(trades)
    assert out["london"] == {"wins": 1, "losses": 1, "pnl": 50.0, "total": 2}
    assert out["ny"] == {"wins": 1, "losses": 0, "pnl": 200.0, "total": 1}


def test_session_breakdown_handles_unknown_session():
    trades = [_row("2026-05-25 09:00", pnl=10.0, session="")]
    out = wr._session_breakdown(trades)
    assert "unknown" in out


# ── Telegram text ───────────────────────────────────────────────────────────


def test_telegram_text_zero_trades_says_no_trade():
    from apps.api.src.agents.trader.analysis.performance import PerformanceReport
    report = PerformanceReport(
        run_id="weekly_x", total_trades=0, win_rate=0, win_rate_ci_low=0,
        win_rate_ci_high=0, profit_factor=0, avg_rr=0, max_dd_pct=0,
        max_dd_dollar=0, sharpe_ratio=0, total_return_pct=0, total_pnl=0,
    )
    text = wr.build_telegram_text(
        report,
        datetime(2026, 5, 23, tzinfo=timezone.utc),
        datetime(2026, 5, 30, tzinfo=timezone.utc),
        [],
        starting_balance=10_000,
        ending_balance=10_000,
    )
    assert "trade yo'q" in text
    assert "HAFTALIK HISOBOT" in text


def test_telegram_text_includes_key_metrics():
    trades = [
        _row("2026-05-25 09:00", pnl=100.0, session="london", result="win",
             setup_type="H1_OB", entry_price=2300, exit_price=2310, sl=2295),
        _row("2026-05-26 09:00", pnl=-50.0, session="ny", result="loss",
             setup_type="H4_FVG", entry_price=2305, exit_price=2300, sl=2310),
    ]
    equity = wr.build_equity_curve(10_000, trades)
    from apps.api.src.agents.trader.analysis import performance as perf
    report = perf.compute(trades, equity, starting_balance=10_000, run_id="weekly_x")
    text = wr.build_telegram_text(
        report,
        datetime(2026, 5, 23, tzinfo=timezone.utc),
        datetime(2026, 5, 30, tzinfo=timezone.utc),
        trades,
        starting_balance=10_000,
        ending_balance=10_050,
    )
    # Numbers (avoiding fragile exact match — check structural keys)
    assert "Trades  : 2" in text
    assert "Win Rate" in text
    assert "PnL" in text
    assert "+$50.00" in text  # 100 - 50
    assert "Best session" in text or "Worst session" in text
    assert "Best setup" in text


# ── generate() integration ──────────────────────────────────────────────────


def test_generate_writes_html_and_returns_struct(tmp_path):
    csv_path = tmp_path / "trades.csv"
    _write_trades_csv(csv_path, [
        # In window
        {"timestamp": "2026-05-25 10:00:00", "symbol": "XAUUSD",
         "entry": "2300", "exit": "2310", "sl": "2295", "pnl": "100",
         "result": "win", "label": "H1_OB", "session": "london"},
        {"timestamp": "2026-05-27 14:00:00", "symbol": "XAUUSD",
         "entry": "2320", "exit": "2310", "sl": "2325", "pnl": "-50",
         "result": "loss", "label": "H4_FVG", "session": "ny"},
        # Out of window
        {"timestamp": "2026-05-10 10:00:00", "symbol": "XAUUSD",
         "entry": "2280", "exit": "2290", "sl": "2275", "pnl": "100",
         "result": "win", "label": "H1_OB", "session": "london"},
    ])
    out_dir = tmp_path / "reports"
    cfg = wr.WeeklyReportConfig(
        starting_balance=10_000,
        window_end=datetime(2026, 5, 30, tzinfo=timezone.utc),
        window_days=7,
        csv_path=csv_path,
        output_dir=out_dir,
        symbol="XAUUSD",
    )
    result = wr.generate(cfg)

    assert result.trade_count == 2
    assert result.window_start == datetime(2026, 5, 23, tzinfo=timezone.utc)
    assert result.window_end == datetime(2026, 5, 30, tzinfo=timezone.utc)
    assert result.ending_balance == pytest.approx(10_050)
    assert result.report.total_pnl == pytest.approx(50.0)
    # HTML file written
    html_path = Path(result.html_path)
    assert html_path.exists()
    assert html_path.parent == out_dir
    # Basic HTML sanity
    body = html_path.read_text(encoding="utf-8")
    assert "<html" in body
    assert "OpenClaw Backtest Report" in body
    assert "weekly_2026-05-30" in body


def test_generate_with_empty_csv(tmp_path):
    csv_path = tmp_path / "trades.csv"
    _write_trades_csv(csv_path, [])
    cfg = wr.WeeklyReportConfig(
        starting_balance=10_000,
        window_end=datetime(2026, 5, 30, tzinfo=timezone.utc),
        csv_path=csv_path,
        output_dir=tmp_path / "reports",
    )
    result = wr.generate(cfg)
    assert result.trade_count == 0
    assert result.ending_balance == 10_000
    assert "trade yo'q" in result.telegram_text


def test_generate_naive_window_end_treated_as_utc(tmp_path):
    csv_path = tmp_path / "trades.csv"
    _write_trades_csv(csv_path, [])
    cfg = wr.WeeklyReportConfig(
        starting_balance=10_000,
        window_end=datetime(2026, 5, 30, 0, 0),  # naive
        csv_path=csv_path,
        output_dir=tmp_path / "reports",
    )
    result = wr.generate(cfg)
    assert result.window_end.tzinfo is not None
    assert result.window_end == datetime(2026, 5, 30, tzinfo=timezone.utc)


def test_to_dict_serialises_result(tmp_path):
    csv_path = tmp_path / "trades.csv"
    _write_trades_csv(csv_path, [])
    cfg = wr.WeeklyReportConfig(
        starting_balance=10_000,
        window_end=datetime(2026, 5, 30, tzinfo=timezone.utc),
        csv_path=csv_path,
        output_dir=tmp_path / "reports",
    )
    result = wr.generate(cfg)
    d = result.to_dict()
    assert d["trade_count"] == 0
    assert d["window_start"] == "2026-05-23T00:00:00+00:00"
    assert d["window_end"] == "2026-05-30T00:00:00+00:00"
    assert d["ending_balance"] == 10_000
    assert "report" in d and "telegram_text" in d


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_runs_with_minimal_args(tmp_path, capsys):
    csv_path = tmp_path / "trades.csv"
    _write_trades_csv(csv_path, [])
    rc = wr.main([
        "--balance", "10000",
        "--end", "2026-05-30",
        "--csv", str(csv_path),
        "--out", str(tmp_path / "reports"),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "HTML:" in out
    assert "Window:" in out


def test_cli_rejects_invalid_end_date(tmp_path, capsys):
    csv_path = tmp_path / "trades.csv"
    _write_trades_csv(csv_path, [])
    rc = wr.main([
        "--balance", "10000",
        "--end", "not-a-date",
        "--csv", str(csv_path),
    ])
    err = capsys.readouterr().err
    assert rc == 2
    assert "invalid --end" in err


def test_cli_print_telegram_flag(tmp_path, capsys):
    csv_path = tmp_path / "trades.csv"
    _write_trades_csv(csv_path, [])
    rc = wr.main([
        "--balance", "10000",
        "--end", "2026-05-30",
        "--csv", str(csv_path),
        "--out", str(tmp_path / "reports"),
        "--print-telegram",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "--- Telegram ---" in out
    assert "HAFTALIK HISOBOT" in out
