"""F2-3 diagnostic: which ICTAnalyst gate eats all signals on real XAUUSD data?

Samples N timestamps across the parquet range, invokes the same MVP analyst
the backtest uses, and counts which exit condition fires. Read-only.
"""
from __future__ import annotations

import sys
from pathlib import Path
from datetime import timezone

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.api.src.agents.trader.analysis.ict import ICTAnalysis
from apps.api.src.agents.trader.core.clock import VirtualClock
from apps.api.src.agents.trader.core.data import HistoricalDataManager
from apps.api.src.agents.trader.engine.analyst_ict import ICTAnalyst, _ob_direction


def main() -> int:
    data_dir = ROOT / "data" / "historical"
    m15 = pd.read_parquet(data_dir / "XAUUSD_M15.parquet")
    print(f"M15 bars: {len(m15):,}  range: {m15['timestamp'].iloc[0]} .. {m15['timestamp'].iloc[-1]}")

    # Sample every 250th M15 bar (~188 samples across 2 years) starting after
    # the H4 warmup (need >=50 H4 candles = 200 hours = 9 days from start).
    warmup_skip = 1000  # ~250 hours = ~10 days
    sample_idx = range(warmup_skip, len(m15), 250)
    timestamps = [m15["timestamp"].iloc[i] for i in sample_idx]
    print(f"Sampling {len(timestamps)} timestamps (every 250th M15 bar after warmup)")

    data = HistoricalDataManager(data_dir)
    data.preload("XAUUSD", timeframes=("M15", "H1", "H4"))

    clock = VirtualClock(timestamps[0].to_pydatetime(), timestamps[-1].to_pydatetime())
    analyst = ICTAnalyst(clock=clock, data=data)
    ict = ICTAnalysis()
    _ = analyst  # silence unused

    gates = {
        "insufficient_data": 0,
        "h4_sideways": 0,
        "no_unmitigated_ob": 0,
        "price_outside_ob": 0,
        "pd_zone_mismatch": 0,
        "malformed_ob": 0,
        "signal_emitted": 0,
    }
    trend_counts = {"bullish": 0, "bearish": 0, "sideways": 0}
    pd_zone_counts = {}
    ob_counts = []
    sample_ob_examples = []

    for ts in timestamps:
        py_ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        clock.current = py_ts  # direct set; VirtualClock has no setter
        now = clock.now()

        # Replicate gate-by-gate
        h4 = data.get_candles_at("XAUUSD", "H4", now, count=200)
        h1 = data.get_candles_at("XAUUSD", "H1", now, count=300)
        m15c = data.get_candles_at("XAUUSD", "M15", now, count=500)

        if any(df is None or len(df) < 50 for df in (h4, h1, m15c)):
            gates["insufficient_data"] += 1
            continue

        ict_h4 = ict.analyze(h4, "H4")
        ict_h1 = ict.analyze(h1, "H1")

        trend = ict_h4.structure.get("trend", "sideways")
        trend_counts[trend] = trend_counts.get(trend, 0) + 1

        if trend == "bullish":
            want_dir = "buy"
        elif trend == "bearish":
            want_dir = "sell"
        else:
            gates["h4_sideways"] += 1
            continue

        obs = ict_h1.order_blocks or []
        ob_counts.append(len(obs))
        matching = [
            ob for ob in obs
            if not ob.get("mitigated", False)
            and _ob_direction(ob.get("type", "")) == want_dir
        ]
        if not matching:
            gates["no_unmitigated_ob"] += 1
            continue

        ob = max(matching, key=lambda o: o.get("strength", 0.0))
        if len(sample_ob_examples) < 3:
            sample_ob_examples.append({
                "ts": str(now),
                "trend": trend,
                "want_dir": want_dir,
                "ob_top": ob.get("high"),
                "ob_bot": ob.get("low"),
                "ob_strength": ob.get("strength"),
                "current_close": float(m15c.iloc[-1]["close"]),
                "pd_zone": ict_h1.pd_zone,
            })

        ob_top = float(ob.get("high", 0.0))
        ob_bot = float(ob.get("low", 0.0))
        if ob_top <= ob_bot:
            gates["malformed_ob"] += 1
            continue

        current_close = float(m15c.iloc[-1]["close"])
        if not (ob_bot <= current_close <= ob_top):
            gates["price_outside_ob"] += 1
            continue

        pd_zone = ict_h1.pd_zone
        pd_zone_counts[pd_zone] = pd_zone_counts.get(pd_zone, 0) + 1
        if want_dir == "buy" and pd_zone not in ("discount", "equilibrium"):
            gates["pd_zone_mismatch"] += 1
            continue
        if want_dir == "sell" and pd_zone not in ("premium", "equilibrium"):
            gates["pd_zone_mismatch"] += 1
            continue

        gates["signal_emitted"] += 1

    print("\n=== GATE BREAKDOWN ===")
    total = len(timestamps)
    for gate, count in gates.items():
        pct = 100.0 * count / total
        print(f"  {gate:>22s}: {count:>4d} ({pct:5.1f}%)")
    print(f"  {'TOTAL':>22s}: {total:>4d}")

    print("\n=== H4 TREND DISTRIBUTION ===")
    for t, c in trend_counts.items():
        print(f"  {t:>10s}: {c:>4d}")

    print("\n=== H1 OB COUNT STATS (when trend != sideways) ===")
    if ob_counts:
        s = pd.Series(ob_counts)
        print(f"  samples: {len(ob_counts)}")
        print(f"  mean: {s.mean():.1f}  median: {s.median():.0f}  max: {s.max()}  min: {s.min()}")
        print(f"  bars w/ 0 OBs: {(s == 0).sum()}  ({100.0*(s==0).sum()/len(s):.1f}%)")

    print("\n=== PD ZONE DISTRIBUTION (after OB match) ===")
    for z, c in pd_zone_counts.items():
        print(f"  {z!r:>15s}: {c}")

    print("\n=== SAMPLE OB STATES (first 3 matches with OBs) ===")
    for ex in sample_ob_examples:
        spread = ex["ob_top"] - ex["ob_bot"]
        dist = ex["current_close"] - (ex["ob_top"] + ex["ob_bot"]) / 2.0
        print(
            f"  {ex['ts']}  trend={ex['trend']}  want={ex['want_dir']}  "
            f"OB=[{ex['ob_bot']:.2f}..{ex['ob_top']:.2f}] (w={spread:.2f}, s={ex['ob_strength']})  "
            f"price={ex['current_close']:.2f}  dist={dist:+.2f}  pd={ex['pd_zone']}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
