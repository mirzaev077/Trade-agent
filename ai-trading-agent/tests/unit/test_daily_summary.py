"""
F3-3: daily_summary generator.

Coverage:
  • Config validation (positive balance)
  • Day window construction (UTC 00:00–24:00, half-open)
  • Out-of-day trades excluded
  • Trade aggregation (PnL, WR, PF, intraday DD)
  • Empty day → "no trade today" message
  • Best-setup detection
  • CLI: happy path, invalid date, default-today
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from apps.api.src.agents.trader.analysis import daily_summary as ds
from apps.api.src.agents.trader.analysis import performance as perf


# ── Helpers ──────────────────────────────────────────────────────────────────


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


# ── Config validation ───────────────────────────────────────────────────────


def test_config_rejects_non_positive_balance():
    with pytest.raises(ValueError, match="starting_balance"):
        ds.DailySummaryConfig(starting_balance=0)
    with pytest.raises(ValueError, match="starting_balance"):
        ds.DailySummaryConfig(starting_balance=-100)


def test_config_defaults():
    cfg = ds.DailySummaryConfig(starting_balance=10_000)
    assert cfg.summary_date is None
    assert cfg.symbol is None


# ── Day window ──────────────────────────────────────────────────────────────


def test_day_window_includes_full_utc_day(tmp_path):
    csv_path = tmp_path / "trades.csv"
    _write_trades_csv(csv_path, [
        # Inside 2026-05-30 UTC
        {"timestamp": "2026-05-30 00:00:00", "symbol": "XAUUSD", "pnl": "10",
         "result": "win", "label": "H1_OB", "session": "asia"},
        {"timestamp": "2026-05-30 23:59:59", "symbol": "XAUUSD", "pnl": "20",
         "result": "win", "label": "H1_OB", "session": "ny"},
        # Outside (previous day end)
        {"timestamp": "2026-05-29 23:59:59", "symbol": "XAUUSD", "pnl": "100",
         "result": "win", "label": "H1_OB", "session": "asia"},
        # Outside (next day start)
        {"timestamp": "2026-05-31 00:00:00", "symbol": "XAUUSD", "pnl": "200",
         "result": "win", "label": "H1_OB", "session": "asia"},
    ])
    cfg = ds.DailySummaryConfig(
        starting_balance=10_000,
        summary_date=date(2026, 5, 30),
        csv_path=csv_path,
    )
    result = ds.generate(cfg)
    assert result.trade_count == 2
    assert result.pnl == pytest.approx(30.0)


def test_day_window_default_is_today_utc(tmp_path, monkeypatch):
    """When summary_date is None, the window covers the current UTC day."""
    csv_path = tmp_path / "trades.csv"
    _write_trades_csv(csv_path, [])
    cfg = ds.DailySummaryConfig(starting_balance=10_000, csv_path=csv_path)
    result = ds.generate(cfg)
    today_utc = datetime.now(timezone.utc).date()
    assert result.summary_date == today_utc


# ── Aggregation ─────────────────────────────────────────────────────────────


def test_pnl_win_rate_and_profit_factor(tmp_path):
    csv_path = tmp_path / "trades.csv"
    _write_trades_csv(csv_path, [
        {"timestamp": "2026-05-30 09:00:00", "symbol": "XAUUSD", "pnl": "100",
         "result": "win", "label": "H1_OB", "session": "london",
         "entry": "2300", "exit": "2310", "sl": "2295"},
        {"timestamp": "2026-05-30 11:00:00", "symbol": "XAUUSD", "pnl": "-50",
         "result": "loss", "label": "H4_FVG", "session": "london",
         "entry": "2320", "exit": "2310", "sl": "2325"},
        {"timestamp": "2026-05-30 14:00:00", "symbol": "XAUUSD", "pnl": "60",
         "result": "win", "label": "H1_OB", "session": "ny",
         "entry": "2330", "exit": "2340", "sl": "2325"},
    ])
    cfg = ds.DailySummaryConfig(
        starting_balance=10_000,
        summary_date=date(2026, 5, 30),
        csv_path=csv_path,
    )
    result = ds.generate(cfg)
    assert result.trade_count == 3
    assert result.pnl == pytest.approx(110.0)
    assert result.win_rate == pytest.approx(2 / 3, abs=0.001)
    assert result.profit_factor == pytest.approx(160 / 50, abs=0.01)
    assert result.ending_balance == pytest.approx(10_110)


def test_intraday_drawdown(tmp_path):
    """Intraday DD = max running peak-to-trough across the day's trades."""
    csv_path = tmp_path / "trades.csv"
    _write_trades_csv(csv_path, [
        {"timestamp": "2026-05-30 09:00", "symbol": "XAUUSD", "pnl": "500",
         "result": "win", "label": "H1_OB", "session": "london"},
        {"timestamp": "2026-05-30 11:00", "symbol": "XAUUSD", "pnl": "-300",
         "result": "loss", "label": "H1_OB", "session": "london"},
        {"timestamp": "2026-05-30 14:00", "symbol": "XAUUSD", "pnl": "200",
         "result": "win", "label": "H1_OB", "session": "ny"},
    ])
    cfg = ds.DailySummaryConfig(
        starting_balance=10_000,
        summary_date=date(2026, 5, 30),
        csv_path=csv_path,
    )
    result = ds.generate(cfg)
    # Peak after T1: 10500. Trough after T2: 10200. DD = 300/10500 ≈ 2.857%
    assert result.max_drawdown_pct == pytest.approx(2.857, abs=0.01)


# ── Telegram text ───────────────────────────────────────────────────────────


def test_telegram_text_empty_day(tmp_path):
    csv_path = tmp_path / "trades.csv"
    _write_trades_csv(csv_path, [])
    cfg = ds.DailySummaryConfig(
        starting_balance=10_000,
        summary_date=date(2026, 5, 30),
        csv_path=csv_path,
    )
    result = ds.generate(cfg)
    assert "trade yo'q" in result.telegram_text
    assert "30.05.2026" in result.telegram_text
    assert "KUNLIK HISOBOT" in result.telegram_text


def test_telegram_text_populated_includes_key_metrics(tmp_path):
    csv_path = tmp_path / "trades.csv"
    _write_trades_csv(csv_path, [
        {"timestamp": "2026-05-30 09:00", "symbol": "XAUUSD", "pnl": "100",
         "result": "win", "label": "H1_OB", "session": "london",
         "entry": "2300", "exit": "2310", "sl": "2295"},
        {"timestamp": "2026-05-30 14:00", "symbol": "XAUUSD", "pnl": "-50",
         "result": "loss", "label": "H4_FVG", "session": "ny",
         "entry": "2320", "exit": "2315", "sl": "2325"},
    ])
    cfg = ds.DailySummaryConfig(
        starting_balance=10_000,
        summary_date=date(2026, 5, 30),
        csv_path=csv_path,
    )
    result = ds.generate(cfg)
    text = result.telegram_text
    assert "30.05.2026" in text
    assert "Trades : 2" in text
    assert "1W / 1L" in text
    assert "WR" in text and "50.0%" in text
    assert "+$50.00" in text
    assert "PF" in text
    assert "DD" in text
    assert "Best setup" in text
    assert "H1_OB" in text   # the winning setup


def test_breakeven_trades_shown_separately_from_losses(tmp_path):
    """BE (pnl=0) trades must not be lumped into the L bucket."""
    csv_path = tmp_path / "trades.csv"
    _write_trades_csv(csv_path, [
        {"timestamp": "2026-05-30 09:00", "symbol": "XAUUSD", "pnl": "100",
         "result": "win", "label": "H1_OB", "session": "london"},
        {"timestamp": "2026-05-30 10:00", "symbol": "XAUUSD", "pnl": "0",
         "result": "be", "label": "H1_OB", "session": "london"},
        {"timestamp": "2026-05-30 11:00", "symbol": "XAUUSD", "pnl": "0",
         "result": "be", "label": "H1_OB", "session": "london"},
        {"timestamp": "2026-05-30 12:00", "symbol": "XAUUSD", "pnl": "-50",
         "result": "loss", "label": "H4_FVG", "session": "ny"},
    ])
    cfg = ds.DailySummaryConfig(
        starting_balance=10_000,
        summary_date=date(2026, 5, 30),
        csv_path=csv_path,
    )
    result = ds.generate(cfg)
    # 4 total: 1W, 1L, 2BE — must read accurately
    assert "1W / 1L / 2BE" in result.telegram_text


def test_best_setup_is_top_pnl(tmp_path):
    csv_path = tmp_path / "trades.csv"
    _write_trades_csv(csv_path, [
        {"timestamp": "2026-05-30 09:00", "symbol": "XAUUSD", "pnl": "100",
         "result": "win", "label": "H1_OB", "session": "london"},
        {"timestamp": "2026-05-30 10:00", "symbol": "XAUUSD", "pnl": "200",
         "result": "win", "label": "H4_FVG", "session": "london"},
        {"timestamp": "2026-05-30 11:00", "symbol": "XAUUSD", "pnl": "-50",
         "result": "loss", "label": "H4_FVG", "session": "london"},
    ])
    cfg = ds.DailySummaryConfig(
        starting_balance=10_000,
        summary_date=date(2026, 5, 30),
        csv_path=csv_path,
    )
    result = ds.generate(cfg)
    # H4_FVG: +150, H1_OB: +100 — H4_FVG wins by pnl
    assert "H4_FVG" in result.telegram_text


# ── to_dict ─────────────────────────────────────────────────────────────────


def test_to_dict_round_trip(tmp_path):
    csv_path = tmp_path / "trades.csv"
    _write_trades_csv(csv_path, [
        {"timestamp": "2026-05-30 09:00", "symbol": "XAUUSD", "pnl": "100",
         "result": "win", "label": "H1_OB", "session": "london"},
    ])
    cfg = ds.DailySummaryConfig(
        starting_balance=10_000,
        summary_date=date(2026, 5, 30),
        csv_path=csv_path,
    )
    result = ds.generate(cfg)
    d = result.to_dict()
    assert d["summary_date"] == "2026-05-30"
    assert d["trade_count"] == 1
    assert d["pnl"] == 100.0
    assert d["ending_balance"] == 10_100.0


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_runs_with_minimal_args(tmp_path, capsys):
    csv_path = tmp_path / "trades.csv"
    _write_trades_csv(csv_path, [
        {"timestamp": "2026-05-30 09:00", "symbol": "XAUUSD", "pnl": "100",
         "result": "win", "label": "H1_OB", "session": "london"},
    ])
    rc = ds.main([
        "--balance", "10000",
        "--date", "2026-05-30",
        "--csv", str(csv_path),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "KUNLIK HISOBOT" in out
    assert "30.05.2026" in out


def test_cli_rejects_invalid_date(tmp_path, capsys):
    csv_path = tmp_path / "trades.csv"
    _write_trades_csv(csv_path, [])
    rc = ds.main([
        "--balance", "10000",
        "--date", "not-a-date",
        "--csv", str(csv_path),
    ])
    err = capsys.readouterr().err
    assert rc == 2
    assert "invalid --date" in err


def test_cli_defaults_to_today(tmp_path, capsys):
    csv_path = tmp_path / "trades.csv"
    _write_trades_csv(csv_path, [])
    rc = ds.main([
        "--balance", "10000",
        "--csv", str(csv_path),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    today_str = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    assert today_str in out
