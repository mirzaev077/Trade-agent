"""
F2-3: Standalone HTML report renderer for backtest results.

Self-contained: no jinja2, no plotly, no matplotlib. Equity curve rendered
as inline SVG hand-built from data points. CSS lives in a <style> block in
the same file.

Public API:
    render(report, equity_curve, closed_trades, config_summary) -> str   # HTML string
    save(html_str, path) -> None                                          # atomic write
"""
from __future__ import annotations

import math
import os
import tempfile
from collections import defaultdict
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any


# --- CSS - minimal but presentable -------------------------------------
_CSS = """
body { font: 14px -apple-system, Segoe UI, Roboto, sans-serif; max-width: 1100px; margin: 24px auto; color: #1f2937; padding: 0 16px; }
h1 { font-size: 24px; margin: 0 0 8px; }
.sub { color: #6b7280; margin-bottom: 24px; }
.verdict { display: inline-block; padding: 4px 12px; border-radius: 4px; font-weight: 600; color: white; }
.verdict-ACCEPT { background: #10b981; }
.verdict-WARN   { background: #f59e0b; }
.verdict-REJECT { background: #ef4444; }
.verdict-PENDING { background: #6b7280; }
.metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin: 16px 0 32px; }
.metric { border: 1px solid #e5e7eb; padding: 12px; border-radius: 6px; background: #f9fafb; }
.metric .label { color: #6b7280; font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; }
.metric .value { font-size: 20px; font-weight: 600; margin-top: 4px; }
.metric .value.pos { color: #10b981; }
.metric .value.neg { color: #ef4444; }
table { width: 100%; border-collapse: collapse; margin: 16px 0; }
th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid #e5e7eb; }
th { background: #f3f4f6; font-weight: 600; }
tr:hover { background: #f9fafb; }
.right { text-align: right; }
h2 { font-size: 18px; margin-top: 32px; }
svg.chart { display: block; margin: 8px 0; max-width: 100%; }
.footer { color: #9ca3af; font-size: 12px; margin-top: 32px; text-align: center; }
"""


# --- Safety helpers ------------------------------------------------------

def _safe_float(v: Any, default: float = 0.0) -> float:
    """Coerce to float; treat None/NaN/Inf/non-numeric as default."""
    if v is None:
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f


def _safe_int(v: Any, default: int = 0) -> int:
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def render(
    report: Any,                     # PerformanceReport or dict with same fields
    equity_curve: list[dict],
    closed_trades: list[dict] | None = None,
    config_summary: dict | None = None,
) -> str:
    """Return a complete HTML document string."""
    # Normalize report -> dict
    if isinstance(report, dict):
        rd = dict(report)
    elif hasattr(report, "to_dict") and callable(report.to_dict):
        rd = report.to_dict()
    elif is_dataclass(report):
        rd = asdict(report)
    elif hasattr(report, "__dict__"):
        rd = dict(report.__dict__)
    else:
        rd = dict(report)

    verdict = str(rd.get("verdict", "PENDING") or "PENDING")
    run_id = str(rd.get("run_id", "unknown") or "unknown")
    config_summary = config_summary or {}
    closed_trades = closed_trades or []
    equity_curve = equity_curve or []

    head = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Backtest Report - {escape(run_id[:8])}</title>
<style>{_CSS}</style>
</head>
<body>
<h1>OpenClaw Backtest Report</h1>
<div class="sub">
  Run <code>{escape(run_id)}</code> &middot;
  Generated {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")} &middot;
  <span class="verdict verdict-{escape(verdict)}">{escape(verdict)}</span>
</div>
"""

    # Config summary table
    if config_summary:
        config_rows = "\n".join(
            f"<tr><td>{escape(str(k))}</td><td class='right'>{escape(str(v))}</td></tr>"
            for k, v in config_summary.items()
        )
        config_html = f"<h2>Configuration</h2><table>{config_rows}</table>"
    else:
        config_html = ""

    metrics_html = _render_metrics_grid(rd)
    equity_chart = _render_equity_chart_svg(equity_curve)
    setup_html = _render_setup_breakdown(rd.get("setup_breakdown", {}) or {})
    monthly_html = _render_monthly_table(closed_trades) if closed_trades else ""

    footer = """
<div class="footer">OpenClaw Trading Agent - Backtest Report - F2-3</div>
</body>
</html>
"""

    return (
        head
        + config_html
        + metrics_html
        + "<h2>Equity Curve</h2>"
        + equity_chart
        + setup_html
        + monthly_html
        + footer
    )


def save(html_str: str, path: str | Path) -> None:
    """Atomic write to path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".report_", suffix=".html", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(html_str)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- Helpers -------------------------------------------------------------

def _render_metrics_grid(rd: dict) -> str:
    """Render the top-level metrics tiles."""

    def fmt_pct(v: Any) -> str:
        return f"{_safe_float(v):+.2f}%"

    def fmt_num(v: Any) -> str:
        return f"{_safe_float(v):.2f}"

    def fmt_int(v: Any) -> str:
        return str(_safe_int(v))

    wr = _safe_float(rd.get("win_rate"))
    wr_lo = _safe_float(rd.get("win_rate_ci_low"))
    wr_hi = _safe_float(rd.get("win_rate_ci_high"))
    pf = _safe_float(rd.get("profit_factor"))
    dd = _safe_float(rd.get("max_dd_pct"))
    sharpe = _safe_float(rd.get("sharpe_ratio"))
    total_return = _safe_float(rd.get("total_return_pct"))
    total_pnl = _safe_float(rd.get("total_pnl"))

    tiles = [
        ("Total Trades", fmt_int(rd.get("total_trades", 0)), "neutral"),
        ("Win Rate", f"{wr * 100:.1f}%", _color_for_wr(wr)),
        ("WR CI (95%)", f"[{wr_lo * 100:.1f}, {wr_hi * 100:.1f}]%", "neutral"),
        ("Profit Factor", fmt_num(pf), _color_for_pf(pf)),
        ("Avg RR", fmt_num(rd.get("avg_rr", 0)), "neutral"),
        ("Max DD", f"{dd:.2f}%", _color_for_dd(dd)),
        ("Sharpe", fmt_num(sharpe), _color_for_sharpe(sharpe)),
        ("Total Return", fmt_pct(total_return), _color_for_pct(total_return)),
        ("Total PnL", f"${total_pnl:,.2f}", _color_for_pct(total_pnl)),
    ]
    cells = "\n".join(
        f'<div class="metric"><div class="label">{escape(label)}</div>'
        f'<div class="value {cls}">{escape(value)}</div></div>'
        for label, value, cls in tiles
    )
    return f'<div class="metrics">{cells}</div>'


def _color_for_wr(wr: float) -> str:
    return "pos" if wr > 0.5 else "neg"


def _color_for_pf(pf: float) -> str:
    return "pos" if pf > 1.5 else ("neg" if pf < 1.0 else "neutral")


def _color_for_dd(dd: float) -> str:
    return "neg" if dd > 15 else ("neutral" if dd > 5 else "pos")


def _color_for_sharpe(s: float) -> str:
    return "pos" if s > 1.0 else "neutral"


def _color_for_pct(p: float) -> str:
    return "pos" if p > 0 else ("neg" if p < 0 else "neutral")


def _render_equity_chart_svg(equity_curve: list[dict]) -> str:
    """Inline SVG line chart of equity over time. ~900x240 viewport."""
    if not equity_curve or len(equity_curve) < 2:
        return "<p><em>Insufficient equity data for chart.</em></p>"

    width, height = 900, 240
    margin_l, margin_b = 60, 30
    margin_r, margin_t = 20, 20
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b

    equities = [_safe_float(p.get("equity")) for p in equity_curve]
    n = len(equities)
    e_min = min(equities)
    e_max = max(equities)
    if e_max == e_min:
        e_max = e_min + 1  # avoid div by zero

    def x(i: int) -> float:
        return margin_l + (i / max(1, n - 1)) * plot_w

    def y(eq: float) -> float:
        return margin_t + (1 - (eq - e_min) / (e_max - e_min)) * plot_h

    points = " ".join(f"{x(i):.1f},{y(equities[i]):.1f}" for i in range(n))

    # Y-axis labels (5 ticks)
    y_ticks_parts = []
    for k in range(5):
        eq = e_min + (e_max - e_min) * (k / 4)
        py = margin_t + (1 - k / 4) * plot_h
        y_ticks_parts.append(
            f'<text x="{margin_l - 6}" y="{py + 4:.1f}" text-anchor="end" font-size="10" '
            f'fill="#6b7280">${eq:,.0f}</text>'
            f'<line x1="{margin_l}" y1="{py:.1f}" x2="{width - margin_r}" y2="{py:.1f}" '
            f'stroke="#e5e7eb" stroke-width="0.5"/>'
        )

    first_t = equity_curve[0].get("timestamp")
    last_t = equity_curve[-1].get("timestamp")

    def fmt_t(t: Any) -> str:
        if isinstance(t, datetime):
            return t.strftime("%Y-%m-%d")
        if t is None:
            return ""
        return str(t)[:10]

    return f"""<svg class="chart" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">
{"".join(y_ticks_parts)}
<polyline fill="none" stroke="#3b82f6" stroke-width="1.5" points="{points}"/>
<line x1="{margin_l}" y1="{height - margin_b}" x2="{width - margin_r}" y2="{height - margin_b}" stroke="#9ca3af" stroke-width="0.5"/>
<text x="{margin_l}" y="{height - margin_b + 18}" font-size="10" fill="#6b7280">{escape(fmt_t(first_t))}</text>
<text x="{width - margin_r}" y="{height - margin_b + 18}" text-anchor="end" font-size="10" fill="#6b7280">{escape(fmt_t(last_t))}</text>
</svg>"""


def _render_setup_breakdown(breakdown: dict) -> str:
    if not breakdown:
        return ""
    rows = "\n".join(
        f"<tr><td>{escape(str(setup))}</td>"
        f"<td class='right'>{_safe_int(stats.get('count', 0))}</td>"
        f"<td class='right'>{_safe_float(stats.get('win_rate', 0)) * 100:.1f}%</td>"
        f"<td class='right'>{_safe_float(stats.get('profit_factor', 0)):.2f}</td>"
        f"<td class='right'>${_safe_float(stats.get('total_pnl', 0)):,.2f}</td></tr>"
        for setup, stats in breakdown.items()
    )
    return f"""<h2>Setup Breakdown</h2>
<table>
<thead><tr><th>Setup</th><th class="right">Trades</th><th class="right">Win Rate</th><th class="right">PF</th><th class="right">P&amp;L</th></tr></thead>
<tbody>{rows}</tbody>
</table>"""


def _render_monthly_table(closed_trades: list[dict]) -> str:
    """Aggregate PnL by year-month."""
    if not closed_trades:
        return ""
    by_month: dict[str, dict] = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    for t in closed_trades:
        if isinstance(t, dict):
            exit_t = t.get("exit_time")
            pnl_raw = t.get("pnl", 0)
        else:
            exit_t = getattr(t, "exit_time", None)
            pnl_raw = getattr(t, "pnl", 0)
        if exit_t is None:
            continue
        if isinstance(exit_t, str):
            try:
                exit_t = datetime.fromisoformat(exit_t)
            except ValueError:
                continue
        if not isinstance(exit_t, datetime):
            continue
        key = exit_t.strftime("%Y-%m")
        pnl = _safe_float(pnl_raw)
        by_month[key]["trades"] += 1
        by_month[key]["pnl"] += pnl
        if pnl > 0:
            by_month[key]["wins"] += 1

    if not by_month:
        return ""

    rows = "\n".join(
        f"<tr><td>{escape(month)}</td><td class='right'>{m['trades']}</td>"
        f"<td class='right'>"
        f"{(m['wins'] / m['trades'] * 100) if m['trades'] else 0:.1f}%</td>"
        f"<td class='right'>${m['pnl']:,.2f}</td></tr>"
        for month, m in sorted(by_month.items())
    )
    return f"""<h2>Monthly P&amp;L</h2>
<table>
<thead><tr><th>Month</th><th class="right">Trades</th><th class="right">Win Rate</th><th class="right">P&amp;L</th></tr></thead>
<tbody>{rows}</tbody>
</table>"""
