"""
TradeAnalytics — CSV export, profit factor, session win rate, haftalik hisobot.
"""
import csv
import os
from datetime import datetime, timedelta, timezone
from loguru import logger


_CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "brain", "trades.csv")
_CSV_FIELDS = [
    "timestamp", "direction", "symbol", "entry", "exit", "sl", "tp1",
    "lot", "pnl", "result", "label", "tf", "mode", "session", "regime",
    "sl_pips", "tp_pips", "rr",
]


def get_csv_path() -> str:
    """Savdo jurnali yo'li. `OPENCLAW_TRADES_CSV` env uni override qiladi.

    A2 himoyasi (2026-08-01): yo'l qattiq yozilgani uchun har qanday test yoki
    ad-hoc skript jonli `brain/trades.csv` ga yozib yuborishi mumkin edi.
    `OPENCLAW_STATE_DIR` (db.py, persistence.py) bilan bir xil konvensiya —
    env CHAQIRUV paytida o'qiladi, shuning uchun `monkeypatch.setenv` ishlaydi.
    """
    return os.path.abspath(os.getenv("OPENCLAW_TRADES_CSV") or _CSV_PATH)


def log_trade_csv(
    direction: str, symbol: str, entry: float, exit_price: float,
    sl: float, tp1: float, lot: float, pnl: float, result: str,
    label: str, tf: str, mode: str, session: str, regime: str,
    sl_pips: float, tp_pips: float,
):
    """Har bir yopilgan tradeни CSV ga yozadi."""
    rr = round(tp_pips / sl_pips, 2) if sl_pips > 0 else 0
    row = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "direction": direction, "symbol": symbol,
        "entry": entry, "exit": exit_price, "sl": sl, "tp1": tp1,
        "lot": lot, "pnl": round(pnl, 2), "result": result,
        "label": label, "tf": tf, "mode": mode,
        "session": session, "regime": regime,
        "sl_pips": round(sl_pips, 1), "tp_pips": round(tp_pips, 1), "rr": rr,
    }
    try:
        path = get_csv_path()
        write_header = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
            if write_header:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        logger.debug(f"CSV write error: {e}")


def get_profit_factor(trades: list, last_n: int = 50) -> float:
    """Profit Factor = gross_profit / gross_loss. Ideal: > 1.5"""
    recent = trades[-last_n:]
    gross_profit = sum(t.get("pnl", 0) for t in recent if t.get("pnl", 0) > 0)
    gross_loss   = abs(sum(t.get("pnl", 0) for t in recent if t.get("pnl", 0) < 0))
    if gross_loss == 0:
        return 999.0 if gross_profit > 0 else 1.0
    return round(gross_profit / gross_loss, 2)


def get_session_stats(trades: list, last_n: int = 100) -> dict:
    """Har sessiya uchun W/L/WR statistikasi."""
    recent = trades[-last_n:]
    stats: dict = {}
    for t in recent:
        s = t.get("session", "unknown")
        stats.setdefault(s, {"wins": 0, "losses": 0, "pnl": 0.0})
        if t.get("result") == "win":
            stats[s]["wins"] += 1
        elif t.get("result") == "loss":
            stats[s]["losses"] += 1
        stats[s]["pnl"] = round(stats[s]["pnl"] + t.get("pnl", 0), 2)
    result = {}
    for s, c in stats.items():
        total = c["wins"] + c["losses"]
        result[s] = {
            "wins": c["wins"], "losses": c["losses"],
            "wr": round(c["wins"] / total * 100, 1) if total > 0 else 0,
            "pnl": c["pnl"], "total": total,
        }
    return result


def build_weekly_report(trades: list, balance: float) -> str:
    """Haftalik Telegram hisobot matni."""
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    weekly = [
        t for t in trades
        if _parse_ts(t.get("ts", "")) >= week_ago
    ]
    if not weekly:
        return "📊 <b>Haftalik hisobot</b>\nBu hafta trade yo'q."

    wins   = sum(1 for t in weekly if t.get("result") == "win")
    losses = sum(1 for t in weekly if t.get("result") == "loss")
    total  = wins + losses
    pnl    = sum(t.get("pnl", 0) for t in weekly)
    wr     = round(wins / total * 100, 1) if total > 0 else 0
    pf     = get_profit_factor(weekly, last_n=len(weekly))

    # Best/worst session
    s_stats = get_session_stats(weekly, last_n=len(weekly))
    best_s  = max(s_stats, key=lambda k: s_stats[k]["pnl"], default="N/A")
    worst_s = min(s_stats, key=lambda k: s_stats[k]["pnl"], default="N/A")

    # Best/worst setup
    by_label: dict = {}
    for t in weekly:
        lbl = t.get("entry_type", t.get("label", "OB"))
        by_label.setdefault(lbl, {"w": 0, "l": 0, "pnl": 0})
        if t.get("result") == "win":
            by_label[lbl]["w"] += 1
        elif t.get("result") == "loss":
            by_label[lbl]["l"] += 1
        by_label[lbl]["pnl"] = round(by_label[lbl]["pnl"] + t.get("pnl", 0), 2)

    pnl_sign = "+" if pnl >= 0 else ""
    pct = round(pnl / balance * 100, 2) if balance > 0 else 0

    lines = [
        "📊 <b>HAFTALIK HISOBOT</b>",
        f"📅 {week_ago.strftime('%d.%m')} – {datetime.now(timezone.utc).strftime('%d.%m.%Y')}",
        "",
        f"Trades : {total} ({wins}W / {losses}L)",
        f"Win Rate: <b>{wr}%</b>",
        f"PnL    : <b>{pnl_sign}{pnl:.2f}$ ({pnl_sign}{pct}%)</b>",
        f"PF     : {pf}",
        "",
        f"🏆 Eng yaxshi sessiya: {best_s} (+{s_stats.get(best_s,{}).get('pnl',0):.1f}$)",
        f"❌ Eng yomon sessiya : {worst_s} ({s_stats.get(worst_s,{}).get('pnl',0):.1f}$)",
    ]
    if by_label:
        best_lbl = max(by_label, key=lambda k: by_label[k]["pnl"])
        lines.append(f"🎯 Eng yaxshi setup  : {best_lbl} ({by_label[best_lbl]['pnl']:+.1f}$)")

    return "\n".join(lines)


def _parse_ts(ts_str: str) -> datetime:
    try:
        dt = datetime.fromisoformat(ts_str)
        # tz-aware bo'lishni majburlash (week_ago bilan taqqoslash uchun)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)
