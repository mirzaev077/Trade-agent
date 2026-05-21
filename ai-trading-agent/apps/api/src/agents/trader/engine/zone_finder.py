"""
F2-3.1 Variant C — Live `_find_all_ict_zones` port for backtest.

Faithful port of TraderAgent's 15-zone-type discovery pipeline from agent.py
(lines 988-1183 + helpers 1187-2825). Live trader is the source of truth for
the math; this module is pure functions over the same ICTSignal output the
live trader consumes (`apps.api.src.agents.trader.analysis.ict.ICTAnalysis`).

All helpers are module-level pure functions — no class, no `self`. They take
constants (PIP, SL_MIN_PIPS, ...) as defaults sourced from CLAUDE.md. This
makes them trivially testable in isolation and decoupled from any agent state.

Zone shape (returned by every helper and `find_all_ict_zones`):

  {
    "direction": "buy" | "sell",
    "entry":     float,         # limit-order entry price
    "sl":        float,         # initial SL price
    "zone_lo":   float,         # bottom of the zone (for debug/UI)
    "zone_hi":   float,         # top of the zone
    "tp1":       float,
    "tp2":       float,
    "tp3":       float,
    "label":     str,           # "{tf}_{kind}" (e.g. "H1_OB", "M15_FVG")
    "weight":    float,         # confluence score (higher = better)
    "tf":        str,           # the timeframe that owns this zone
    "htf_conf":  int,           # set by find_all_ict_zones (multi-TF score)
    "silver_bullet": bool,      # set by find_all_ict_zones
  }
"""

from __future__ import annotations

import numpy as np

# ── Constants (CLAUDE.md — DO NOT change without breaking live trader) ────────

PIP: float = 0.10                # XAUUSD pip in price units
SL_MIN_PIPS: float = 8.0
SL_MAX_PIPS: float = 50.0
TP_MIN_PIPS: float = 12.0
TP_MAX_PIPS: float = 150.0

TF_WEIGHTS: dict[str, float] = {
    "D1": 10.0, "H4": 8.0, "H1": 6.0,
    "M30": 4.0, "M15": 5.0, "M5": 3.0, "M1": 1.0,
}

# Max distance from price for any zone to be considered actionable.
MAX_DIST_DEFAULT: float = 200 * PIP


# ── RR helpers ────────────────────────────────────────────────────────────────


def rr_targets(
    entry: float,
    direction: str,
    sl_pips: float,
    tp_max: float = TP_MAX_PIPS,
    pip: float = PIP,
) -> tuple[float, float, float]:
    """TP1=1.5R  TP2=2.5R  TP3=4R, all capped at `tp_max` pips."""
    sign = 1 if direction == "buy" else -1
    tp1_p = min(sl_pips * 1.5, tp_max)
    tp2_p = min(sl_pips * 2.5, tp_max)
    tp3_p = min(sl_pips * 4.0, tp_max)
    return (
        round(entry + sign * tp1_p * pip, 2),
        round(entry + sign * tp2_p * pip, 2),
        round(entry + sign * tp3_p * pip, 2),
    )


def swing_tp_targets(
    entry: float,
    direction: str,
    sl_pips: float,
    candles,
    tp_max: float = TP_MAX_PIPS,
    pip: float = PIP,
) -> tuple[float, float, float]:
    """Fractal-swing-based TPs. Falls back to fixed-R targets if no swings found."""
    if candles is None or len(candles) < 10:
        return rr_targets(entry, direction, sl_pips, tp_max, pip)

    hi = candles["high"].astype(float).values
    lo = candles["low"].astype(float).values
    n = min(150, len(hi))
    start = max(2, len(hi) - n)

    swings: list[float] = []
    if direction == "buy":
        for i in range(start + 2, len(hi) - 1):
            v = hi[i]
            if v > hi[i - 1] and v > hi[i - 2] and v > hi[i + 1]:
                if v > entry + 5 * pip:
                    dist = (v - entry) / pip
                    if dist <= tp_max:
                        swings.append(v)
        swings = sorted({round(s, 2) for s in swings})  # nearest first
    else:
        for i in range(start + 2, len(lo) - 1):
            v = lo[i]
            if v < lo[i - 1] and v < lo[i - 2] and v < lo[i + 1]:
                if v < entry - 5 * pip:
                    dist = (entry - v) / pip
                    if dist <= tp_max:
                        swings.append(v)
        swings = sorted({round(s, 2) for s in swings}, reverse=True)

    if len(swings) >= 3:
        return swings[0], swings[len(swings) // 2], swings[-1]
    fb = rr_targets(entry, direction, sl_pips, tp_max, pip)
    if len(swings) == 2:
        return swings[0], swings[1], fb[2]
    if len(swings) == 1:
        return swings[0], fb[1], fb[2]
    return fb


def structural_sl(
    direction: str,
    entry: float,
    candles,
    zone_sl: float,
    pip: float = PIP,
    sl_min_pips: float = SL_MIN_PIPS,
    sl_max_pips: float = SL_MAX_PIPS,
) -> float | None:
    """Returns nearest swing-low (buy) or swing-high (sell) outside SL constraints,
    else None. Caller falls back to `zone_sl`."""
    if candles is None or len(candles) < 10:
        return None
    hi = candles["high"].astype(float).values
    lo = candles["low"].astype(float).values
    n = min(80, len(hi))
    start = max(2, len(hi) - n)
    buf = 3 * pip

    if direction == "buy":
        swings: list[float] = []
        for i in range(start + 2, len(lo) - 1):
            v = lo[i]
            if v < lo[i - 1] and v < lo[i - 2] and v < lo[i + 1]:
                if v < entry:
                    swings.append(v)
        swings.sort(reverse=True)
        for sw in swings:
            sl_cand = round(sw - buf, 2)
            sl_p = (entry - sl_cand) / pip
            if sl_min_pips <= sl_p <= sl_max_pips:
                return sl_cand
    else:
        swings = []
        for i in range(start + 2, len(hi) - 1):
            v = hi[i]
            if v > hi[i - 1] and v > hi[i - 2] and v > hi[i + 1]:
                if v > entry:
                    swings.append(v)
        swings.sort()
        for sw in swings:
            sl_cand = round(sw + buf, 2)
            sl_p = (sl_cand - entry) / pip
            if sl_min_pips <= sl_p <= sl_max_pips:
                return sl_cand
    return None


# ── Volume boost (zone weight multiplier) ─────────────────────────────────────


def volume_boost(candles) -> float:
    """1.4 if last bar volume >= 1.5× recent avg (20 bars), else 1.0."""
    if candles is None or len(candles) < 20:
        return 1.0
    vol = candles["tick_volume"].astype(float).values
    avg = float(np.mean(vol[-20:]))
    last = float(vol[-1])
    if avg > 0 and last >= avg * 1.5:
        return 1.4
    return 1.0


# ── Zone helpers ──────────────────────────────────────────────────────────────


def ob_zone(
    ob: dict,
    want_dir: str,
    price: float,
    buf: float,
    wt: float,
    tf: str,
    label_suffix: str,
    pip: float = PIP,
    sl_min_pips: float = SL_MIN_PIPS,
    sl_max_pips: float = SL_MAX_PIPS,
    tp_max_pips: float = TP_MAX_PIPS,
) -> dict | None:
    """OB / BB / MB / RB / OTE-as-OB → zone dict (sniper: entry at 30% of OB)."""
    hi = float(ob.get("high", 0))
    lo = float(ob.get("low", 0))
    if hi <= 0 or lo <= 0 or hi <= lo:
        return None

    typ = ob.get("type", "")
    rng = hi - lo
    MAX_DIST = 200 * pip

    if want_dir == "buy" and ("bullish" in typ or "buy" in typ or "mitigation" in typ):
        if price > hi + MAX_DIST or price < lo - MAX_DIST:
            return None
        entry_p = round(lo + rng * 0.30, 2)
        sl = round(lo - buf, 2)
        sl_p = (entry_p - sl) / pip
        if sl_p < sl_min_pips:
            sl = round(entry_p - sl_min_pips * pip, 2)
            sl_p = sl_min_pips
        if sl_p > sl_max_pips:
            sl = round(entry_p - sl_max_pips * pip, 2)
            sl_p = sl_max_pips
        if not (sl_min_pips <= sl_p <= sl_max_pips):
            return None
        bonus = 1 if lo <= price <= hi else 0
        tp1, tp2, tp3 = rr_targets(entry_p, "buy", sl_p, tp_max_pips, pip)
        return {
            "direction": "buy", "entry": entry_p, "sl": sl,
            "zone_lo": lo, "zone_hi": hi,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "label": f"{tf}_{label_suffix}", "weight": wt * 3 + bonus, "tf": tf,
        }

    if want_dir == "sell" and ("bearish" in typ or "sell" in typ or "mitigation" in typ):
        if price < lo - MAX_DIST or price > hi + MAX_DIST:
            return None
        entry_p = round(hi - rng * 0.30, 2)
        sl = round(hi + buf, 2)
        sl_p = (sl - entry_p) / pip
        if sl_p < sl_min_pips:
            sl = round(entry_p + sl_min_pips * pip, 2)
            sl_p = sl_min_pips
        if sl_p > sl_max_pips:
            sl = round(entry_p + sl_max_pips * pip, 2)
            sl_p = sl_max_pips
        if not (sl_min_pips <= sl_p <= sl_max_pips):
            return None
        bonus = 1 if lo <= price <= hi else 0
        tp1, tp2, tp3 = rr_targets(entry_p, "sell", sl_p, tp_max_pips, pip)
        return {
            "direction": "sell", "entry": entry_p, "sl": sl,
            "zone_lo": lo, "zone_hi": hi,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "label": f"{tf}_{label_suffix}", "weight": wt * 3 + bonus, "tf": tf,
        }
    return None


def fvg_zone(
    fvg: dict,
    want_dir: str,
    price: float,
    buf: float,
    wt: float,
    tf: str,
    pip: float = PIP,
    sl_min_pips: float = SL_MIN_PIPS,
    sl_max_pips: float = SL_MAX_PIPS,
    tp_max_pips: float = TP_MAX_PIPS,
) -> dict | None:
    """FVG / IFVG / BISI / SIBI / BPR → zone dict (sniper: 10% inside gap)."""
    hi = float(fvg.get("high", 0))
    lo = float(fvg.get("low", 0))
    typ = fvg.get("type", "")
    if hi <= 0 or lo <= 0 or hi <= lo:
        return None

    is_bull = ("bullish" in typ or "sibi" in typ or "ifvg_bullish" in typ or "bpr" in typ)
    is_bear = ("bearish" in typ or "bisi" in typ or "ifvg_bearish" in typ or "bpr" in typ)

    rng = hi - lo
    MAX_DIST = 200 * pip

    if want_dir == "buy" and is_bull:
        if price > hi + MAX_DIST or price < lo - MAX_DIST:
            return None
        entry_p = round(lo + rng * 0.10, 2)
        sl = round(lo - buf, 2)
        sl_p = (entry_p - sl) / pip
        if sl_p < sl_min_pips:
            sl = round(entry_p - sl_min_pips * pip, 2)
            sl_p = sl_min_pips
        if sl_p > sl_max_pips:
            sl = round(entry_p - sl_max_pips * pip, 2)
            sl_p = sl_max_pips
        if not (sl_min_pips <= sl_p <= sl_max_pips):
            return None
        tp1, tp2, tp3 = rr_targets(entry_p, "buy", sl_p, tp_max_pips, pip)
        return {
            "direction": "buy", "entry": entry_p, "sl": sl,
            "zone_lo": lo, "zone_hi": hi,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "label": f"{tf}_{typ.upper()[:4]}", "weight": wt * 2, "tf": tf,
        }

    if want_dir == "sell" and is_bear:
        if price < lo - MAX_DIST or price > hi + MAX_DIST:
            return None
        entry_p = round(hi - rng * 0.10, 2)
        sl = round(hi + buf, 2)
        sl_p = (sl - entry_p) / pip
        if sl_p < sl_min_pips:
            sl = round(entry_p + sl_min_pips * pip, 2)
            sl_p = sl_min_pips
        if sl_p > sl_max_pips:
            sl = round(entry_p + sl_max_pips * pip, 2)
            sl_p = sl_max_pips
        if not (sl_min_pips <= sl_p <= sl_max_pips):
            return None
        tp1, tp2, tp3 = rr_targets(entry_p, "sell", sl_p, tp_max_pips, pip)
        return {
            "direction": "sell", "entry": entry_p, "sl": sl,
            "zone_lo": lo, "zone_hi": hi,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "label": f"{tf}_{typ.upper()[:4]}", "weight": wt * 2, "tf": tf,
        }
    return None


def liq_zone(
    lz: dict,
    want_dir: str,
    price: float,
    atr: float,
    wt: float,
    tf: str,
    pip: float = PIP,
    sl_min_pips: float = SL_MIN_PIPS,
    sl_max_pips: float = SL_MAX_PIPS,
) -> dict | None:
    """After liquidity sweep → reversal entry zone."""
    if not lz.get("swept"):
        return None
    lz_type = lz.get("type", "")
    lz_p = float(lz.get("price", 0))
    if lz_p <= 0:
        return None

    if want_dir == "buy" and lz_type in ("SSL", "ERL_low", "EQL"):
        entry_p = round(lz_p + atr * 0.1, 2)
        sl = round(lz_p - atr * 0.3, 2)
        sl_p = (entry_p - sl) / pip
        if not (sl_min_pips <= sl_p <= sl_max_pips):
            return None
        if price < sl:
            return None
        tp1, tp2, tp3 = rr_targets(entry_p, "buy", sl_p, pip=pip)
        return {
            "direction": "buy", "entry": entry_p, "sl": sl,
            "zone_lo": sl, "zone_hi": entry_p + atr * 0.2,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "label": f"{tf}_{lz_type}_sweep", "weight": wt * 2.5, "tf": tf,
        }

    if want_dir == "sell" and lz_type in ("BSL", "ERL_high", "EQH"):
        entry_p = round(lz_p - atr * 0.1, 2)
        sl = round(lz_p + atr * 0.3, 2)
        sl_p = (sl - entry_p) / pip
        if not (sl_min_pips <= sl_p <= sl_max_pips):
            return None
        if price > sl:
            return None
        tp1, tp2, tp3 = rr_targets(entry_p, "sell", sl_p, pip=pip)
        return {
            "direction": "sell", "entry": entry_p, "sl": sl,
            "zone_lo": entry_p - atr * 0.2, "zone_hi": sl,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "label": f"{tf}_{lz_type}_sweep", "weight": wt * 2.5, "tf": tf,
        }
    return None


def liq_void_zone(
    lv: dict,
    want_dir: str,
    price: float,
    buf: float,
    wt: float,
    tf: str,
    pip: float = PIP,
    sl_min_pips: float = SL_MIN_PIPS,
    sl_max_pips: float = SL_MAX_PIPS,
) -> dict | None:
    """Liquidity Void — large-impulse gap that price tends to refill."""
    hi = float(lv.get("high", 0))
    lo = float(lv.get("low", 0))
    typ = lv.get("type", "")
    if hi <= 0 or lo <= 0 or hi <= lo:
        return None
    MAX_DIST = 200 * pip

    if want_dir == "buy" and "bull" in typ:
        if not (lo - MAX_DIST <= price <= hi + MAX_DIST):
            return None
        entry_p = round(lo + (hi - lo) * 0.2, 2)
        sl = round(lo - buf * 1.5, 2)
        sl_p = (entry_p - sl) / pip
        if sl_p < sl_min_pips:
            sl = round(entry_p - sl_min_pips * pip, 2)
            sl_p = sl_min_pips
        if sl_p > sl_max_pips:
            return None
        tp1, tp2, tp3 = rr_targets(entry_p, "buy", sl_p, pip=pip)
        return {
            "direction": "buy", "entry": entry_p, "sl": sl,
            "zone_lo": lo, "zone_hi": hi,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "label": f"{tf}_LiqVoid", "weight": wt * 2.2, "tf": tf,
        }

    if want_dir == "sell" and "bear" in typ:
        if not (lo - MAX_DIST <= price <= hi + MAX_DIST):
            return None
        entry_p = round(hi - (hi - lo) * 0.2, 2)
        sl = round(hi + buf * 1.5, 2)
        sl_p = (sl - entry_p) / pip
        if sl_p < sl_min_pips:
            sl = round(entry_p + sl_min_pips * pip, 2)
            sl_p = sl_min_pips
        if sl_p > sl_max_pips:
            return None
        tp1, tp2, tp3 = rr_targets(entry_p, "sell", sl_p, pip=pip)
        return {
            "direction": "sell", "entry": entry_p, "sl": sl,
            "zone_lo": lo, "zone_hi": hi,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "label": f"{tf}_LiqVoid", "weight": wt * 2.2, "tf": tf,
        }
    return None


def unswept_liq_zone(
    lz: dict,
    want_dir: str,
    price: float,
    atr: float,
    wt: float,
    tf: str,
    pip: float = PIP,
    sl_min_pips: float = SL_MIN_PIPS,
    sl_max_pips: float = SL_MAX_PIPS,
) -> dict | None:
    """EQL/EQH/IDM unswept — limit entry near the unswept liquidity level."""
    lz_type = lz.get("type", "")
    lz_p = float(lz.get("price", lz.get("high", lz.get("low", 0))))
    if lz_p <= 0:
        return None
    MAX_DIST = 150 * pip

    if want_dir == "buy" and lz_type in ("EQL", "IDM_buy"):
        if not (lz_p <= price <= lz_p + MAX_DIST):
            return None
        entry_p = round(lz_p + atr * 0.06, 2)
        sl = round(lz_p - atr * 0.40, 2)
        sl_p = (entry_p - sl) / pip
        if sl_p < sl_min_pips:
            sl = round(entry_p - sl_min_pips * pip, 2)
            sl_p = sl_min_pips
        if sl_p > sl_max_pips:
            return None
        tp1, tp2, tp3 = rr_targets(entry_p, "buy", sl_p, pip=pip)
        return {
            "direction": "buy", "entry": entry_p, "sl": sl,
            "zone_lo": lz_p - atr * 0.1, "zone_hi": lz_p + atr * 0.1,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "label": f"{tf}_{lz_type}", "weight": wt * 1.6, "tf": tf,
        }

    if want_dir == "sell" and lz_type in ("EQH", "IDM_sell"):
        if not (lz_p - MAX_DIST <= price <= lz_p):
            return None
        entry_p = round(lz_p - atr * 0.06, 2)
        sl = round(lz_p + atr * 0.40, 2)
        sl_p = (sl - entry_p) / pip
        if sl_p < sl_min_pips:
            sl = round(entry_p + sl_min_pips * pip, 2)
            sl_p = sl_min_pips
        if sl_p > sl_max_pips:
            return None
        tp1, tp2, tp3 = rr_targets(entry_p, "sell", sl_p, pip=pip)
        return {
            "direction": "sell", "entry": entry_p, "sl": sl,
            "zone_lo": lz_p - atr * 0.1, "zone_hi": lz_p + atr * 0.1,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "label": f"{tf}_{lz_type}", "weight": wt * 1.6, "tf": tf,
        }
    return None


def dealing_eq_zone(
    dr: dict,
    want_dir: str,
    price: float,
    atr: float,
    wt: float,
    tf: str,
    pip: float = PIP,
    sl_min_pips: float = SL_MIN_PIPS,
    sl_max_pips: float = SL_MAX_PIPS,
) -> dict | None:
    """Dealing-Range Equilibrium (50%) — MM accumulation/distribution zone."""
    eq = float(dr.get("eq", 0))
    zone_curr = dr.get("current_zone", "")
    if eq <= 0:
        return None
    dist = abs(price - eq) / pip
    if dist > 60 or dist < 1:
        return None

    if want_dir == "buy" and price < eq and zone_curr == "discount":
        entry_p = round(eq - atr * 0.06, 2)
        sl = round(eq - atr * 0.50, 2)
        sl_p = (entry_p - sl) / pip
        if sl_p < sl_min_pips:
            sl = round(entry_p - sl_min_pips * pip, 2)
            sl_p = sl_min_pips
        if sl_p > sl_max_pips:
            return None
        tp1, tp2, tp3 = rr_targets(entry_p, "buy", sl_p, pip=pip)
        return {
            "direction": "buy", "entry": entry_p, "sl": sl,
            "zone_lo": eq - atr * 0.1, "zone_hi": eq + atr * 0.05,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "label": f"{tf}_DR_Eq", "weight": wt * 1.9, "tf": tf,
        }

    if want_dir == "sell" and price > eq and zone_curr == "premium":
        entry_p = round(eq + atr * 0.06, 2)
        sl = round(eq + atr * 0.50, 2)
        sl_p = (sl - entry_p) / pip
        if sl_p < sl_min_pips:
            sl = round(entry_p + sl_min_pips * pip, 2)
            sl_p = sl_min_pips
        if sl_p > sl_max_pips:
            return None
        tp1, tp2, tp3 = rr_targets(entry_p, "sell", sl_p, pip=pip)
        return {
            "direction": "sell", "entry": entry_p, "sl": sl,
            "zone_lo": eq - atr * 0.05, "zone_hi": eq + atr * 0.1,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "label": f"{tf}_DR_Eq", "weight": wt * 1.9, "tf": tf,
        }
    return None


def bos_sr_zone(
    bp: dict,
    want_dir: str,
    price: float,
    atr: float,
    wt: float,
    tf: str,
    pip: float = PIP,
    sl_min_pips: float = SL_MIN_PIPS,
    sl_max_pips: float = SL_MAX_PIPS,
) -> dict | None:
    """BOS / CISD level — broken structure flips to S/R for the retest."""
    bos_p = float(bp.get("price", 0))
    bos_dir = bp.get("dir", "none")
    if bos_p <= 0:
        return None
    dist = abs(price - bos_p) / pip
    if dist > 80 or dist < 0.5:
        return None

    if want_dir == "buy" and bos_dir == "bullish" and price >= bos_p:
        entry_p = round(bos_p + atr * 0.05, 2)
        sl = round(bos_p - atr * 0.40, 2)
        sl_p = (entry_p - sl) / pip
        if sl_p < sl_min_pips:
            sl = round(entry_p - sl_min_pips * pip, 2)
            sl_p = sl_min_pips
        if sl_p > sl_max_pips:
            return None
        tp1, tp2, tp3 = rr_targets(entry_p, "buy", sl_p, pip=pip)
        return {
            "direction": "buy", "entry": entry_p, "sl": sl,
            "zone_lo": bos_p - atr * 0.1, "zone_hi": bos_p + atr * 0.1,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "label": f"{tf}_BOS_S", "weight": wt * 2.4, "tf": tf,
        }

    if want_dir == "sell" and bos_dir == "bearish" and price <= bos_p:
        entry_p = round(bos_p - atr * 0.05, 2)
        sl = round(bos_p + atr * 0.40, 2)
        sl_p = (sl - entry_p) / pip
        if sl_p < sl_min_pips:
            sl = round(entry_p + sl_min_pips * pip, 2)
            sl_p = sl_min_pips
        if sl_p > sl_max_pips:
            return None
        tp1, tp2, tp3 = rr_targets(entry_p, "sell", sl_p, pip=pip)
        return {
            "direction": "sell", "entry": entry_p, "sl": sl,
            "zone_lo": bos_p - atr * 0.1, "zone_hi": bos_p + atr * 0.1,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "label": f"{tf}_BOS_R", "weight": wt * 2.4, "tf": tf,
        }
    return None


def ote_fib_zones(
    want_dir: str,
    candles,
    tf: str,
    atr: float,
    wt: float,
    price: float,
    pip: float = PIP,
    sl_min_pips: float = SL_MIN_PIPS,
    sl_max_pips: float = SL_MAX_PIPS,
    tp_max_pips: float = TP_MAX_PIPS,
) -> list[dict]:
    """Dynamic OTE: 0.705 fib retracement of the most recent swing (60 bars)."""
    if candles is None or len(candles) < 30:
        return []
    hi = candles["high"].astype(float).values
    lo = candles["low"].astype(float).values
    n = min(60, len(hi))

    swing_hi = float(np.max(hi[-n:]))
    swing_lo = float(np.min(lo[-n:]))
    swing = swing_hi - swing_lo
    if swing < 8 * pip:
        return []

    MAX_DIST = 200 * pip
    zones: list[dict] = []

    if want_dir == "buy":
        ote_hi = round(swing_hi - swing * 0.618, 2)
        ote_lo = round(swing_hi - swing * 0.786, 2)
        entry_p = round(swing_hi - swing * 0.705, 2)
        if price < lo[-1] - MAX_DIST or price > hi[-1] + MAX_DIST:
            return []
        sl = round(ote_lo - atr * 0.15, 2)
        sl_p = (entry_p - sl) / pip
        if sl_p < sl_min_pips:
            sl = round(entry_p - sl_min_pips * pip, 2)
            sl_p = sl_min_pips
        if sl_p > sl_max_pips:
            return []
        tp1, tp2, tp3 = rr_targets(entry_p, "buy", sl_p, tp_max_pips, pip)
        zones.append({
            "direction": "buy", "entry": entry_p, "sl": sl,
            "zone_lo": ote_lo, "zone_hi": ote_hi,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "label": f"{tf}_OTE", "weight": wt * 4.5, "tf": tf,
        })

    elif want_dir == "sell":
        ote_lo = round(swing_lo + swing * 0.618, 2)
        ote_hi = round(swing_lo + swing * 0.786, 2)
        entry_p = round(swing_lo + swing * 0.705, 2)
        if price < lo[-1] - MAX_DIST or price > hi[-1] + MAX_DIST:
            return []
        sl = round(ote_hi + atr * 0.15, 2)
        sl_p = (sl - entry_p) / pip
        if sl_p < sl_min_pips:
            sl = round(entry_p + sl_min_pips * pip, 2)
            sl_p = sl_min_pips
        if sl_p > sl_max_pips:
            return []
        tp1, tp2, tp3 = rr_targets(entry_p, "sell", sl_p, tp_max_pips, pip)
        zones.append({
            "direction": "sell", "entry": entry_p, "sl": sl,
            "zone_lo": ote_lo, "zone_hi": ote_hi,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "label": f"{tf}_OTE", "weight": wt * 4.5, "tf": tf,
        })

    return zones


# ── Main dispatcher ───────────────────────────────────────────────────────────


def find_all_ict_zones(
    want_dir: str,
    price: float,
    tf_ict_data: list[tuple[str, object, float, object]],
    want_trend: str = "none",
    htf_conf: int = 0,
    silver_bullet: bool = False,
    pip: float = PIP,
    sl_min_pips: float = SL_MIN_PIPS,
    sl_max_pips: float = SL_MAX_PIPS,
    tp_max_pips: float = TP_MAX_PIPS,
    tf_weights: dict | None = None,
    dedup_buffer_price: float = 1.5,
    max_zones: int = 20,
) -> list[dict]:
    """Faithful port of TraderAgent._find_all_ict_zones.

    :param tf_ict_data: ``[(tf, ICTSignal, atr, candles), ...]`` — already-computed
        ICT output per timeframe. The analyst runs `ICTAnalysis.analyze` once per
        TF and assembles this list.
    :param want_dir: 'buy' | 'sell'
    :param price: current close
    :param want_trend: HTF trend label for trend-bonus multiplier
    :param htf_conf: number of higher-TFs confirming `want_dir` (0-4)
    :param silver_bullet: True if we're in the Silver Bullet kill-zone hour
    :return: list of zone dicts, sorted by weight (highest first), capped at
             ``max_zones`` and deduped within ``dedup_buffer_price``.
    """
    weights = tf_weights or TF_WEIGHTS

    trend_bonus = 1.3 if want_trend == want_dir else 1.0
    sb_bonus = 1.5 if silver_bullet else 1.0
    htf_bonus = 1.0 + htf_conf * 0.15

    zones: list[dict] = []

    for tf, ict, atr, candles in tf_ict_data:
        if ict is None:
            continue
        wt = weights.get(tf, 1.0)
        buf = atr * 0.10
        vol_mul = volume_boost(candles)

        # 1. Order Blocks
        for ob in getattr(ict, "order_blocks", []) or []:
            if ob.get("mitigated"):
                continue
            z = ob_zone(ob, want_dir, price, buf, wt * vol_mul, tf, "OB",
                       pip, sl_min_pips, sl_max_pips, tp_max_pips)
            if z:
                zones.append(z)

        # 2. Breaker Blocks
        for bb in getattr(ict, "breaker_blocks", []) or []:
            z = ob_zone(bb, want_dir, price, buf, wt * vol_mul, tf, "BB",
                       pip, sl_min_pips, sl_max_pips, tp_max_pips)
            if z:
                zones.append(z)

        structure = getattr(ict, "structure", {}) or {}

        # 3. Mitigation Blocks
        for mb in structure.get("mitigation_blocks", []):
            z = ob_zone(mb, want_dir, price, buf, wt * 0.7, tf, "MB",
                       pip, sl_min_pips, sl_max_pips, tp_max_pips)
            if z:
                zones.append(z)

        # 4. Rejection Blocks (direction-filtered)
        for rb in structure.get("rejection_blocks", []):
            rb_type = rb.get("type", "")
            if (("bullish" in rb_type and want_dir == "buy")
                    or ("bearish" in rb_type and want_dir == "sell")):
                z = ob_zone(rb, want_dir, price, buf, wt * 0.6, tf, "RB",
                           pip, sl_min_pips, sl_max_pips, tp_max_pips)
                if z:
                    zones.append(z)

        # 5. FVG / IFVG / BISI / SIBI / BPR
        for fvg in getattr(ict, "fvgs", []) or []:
            if fvg.get("filled"):
                continue
            z = fvg_zone(fvg, want_dir, price, buf, wt * vol_mul, tf,
                        pip, sl_min_pips, sl_max_pips, tp_max_pips)
            if z:
                zones.append(z)

        # 6. OTE zone (static — ICT-detected)
        ote = structure.get("ote_zone", {}) or {}
        if ote.get("active") and ote.get("direction") == want_dir:
            ote_hi = float(ote.get("high", 0))
            ote_lo = float(ote.get("low", 0))
            if ote_hi > 0 and ote_lo > 0:
                z = ob_zone(
                    {"type": f"{'bullish' if want_dir == 'buy' else 'bearish'}_ob",
                     "high": ote_hi, "low": ote_lo, "mitigated": False},
                    want_dir, price, buf, wt * 1.5, tf, "OTE",
                    pip, sl_min_pips, sl_max_pips, tp_max_pips,
                )
                if z:
                    zones.append(z)

        # 7. Liquidity sweeps → reversal zones
        for lz in getattr(ict, "liquidity_zones", []) or []:
            z = liq_zone(lz, want_dir, price, atr, wt, tf,
                        pip, sl_min_pips, sl_max_pips)
            if z:
                zones.append(z)

        # 8. PDH / PDL as key levels
        pdh = float(structure.get("pdh", 0))
        pdl = float(structure.get("pdl", 0))
        if want_dir == "sell" and pdh > 0 and price <= pdh * 1.002:
            sl = round(pdh + buf * 2, 2)
            sl_p = (sl - pdh) / pip
            if sl_min_pips <= sl_p <= sl_max_pips:
                tp1, tp2, tp3 = rr_targets(pdh, "sell", sl_p, pip=pip)
                zones.append({
                    "direction": "sell", "entry": round(pdh, 2), "sl": sl,
                    "zone_lo": pdh - atr * 0.5, "zone_hi": pdh + atr * 0.5,
                    "tp1": tp1, "tp2": tp2, "tp3": tp3,
                    "label": f"{tf}_PDH", "weight": wt * 2.5, "tf": tf,
                })
        if want_dir == "buy" and pdl > 0 and price >= pdl * 0.998:
            sl = round(pdl - buf * 2, 2)
            sl_p = (pdl - sl) / pip
            if sl_min_pips <= sl_p <= sl_max_pips:
                tp1, tp2, tp3 = rr_targets(pdl, "buy", sl_p, pip=pip)
                zones.append({
                    "direction": "buy", "entry": round(pdl, 2), "sl": sl,
                    "zone_lo": pdl - atr * 0.5, "zone_hi": pdl + atr * 0.5,
                    "tp1": tp1, "tp2": tp2, "tp3": tp3,
                    "label": f"{tf}_PDL", "weight": wt * 2.5, "tf": tf,
                })

        # 9. Asian Range high/low
        ar_hi = float(structure.get("asian_high", 0))
        ar_lo = float(structure.get("asian_low", 0))
        if want_dir == "sell" and ar_hi > 0 and price <= ar_hi * 1.001:
            sl = round(ar_hi + buf * 2, 2)
            sl_p = (sl - ar_hi) / pip
            if sl_min_pips <= sl_p <= sl_max_pips:
                tp1, tp2, tp3 = rr_targets(ar_hi, "sell", sl_p, pip=pip)
                zones.append({
                    "direction": "sell", "entry": round(ar_hi, 2), "sl": sl,
                    "zone_lo": ar_hi - atr * 0.3, "zone_hi": ar_hi + atr * 0.3,
                    "tp1": tp1, "tp2": tp2, "tp3": tp3,
                    "label": f"{tf}_AR_HI", "weight": wt * 2, "tf": tf,
                })
        if want_dir == "buy" and ar_lo > 0 and price >= ar_lo * 0.999:
            sl = round(ar_lo - buf * 2, 2)
            sl_p = (ar_lo - sl) / pip
            if sl_min_pips <= sl_p <= sl_max_pips:
                tp1, tp2, tp3 = rr_targets(ar_lo, "buy", sl_p, pip=pip)
                zones.append({
                    "direction": "buy", "entry": round(ar_lo, 2), "sl": sl,
                    "zone_lo": ar_lo - atr * 0.3, "zone_hi": ar_lo + atr * 0.3,
                    "tp1": tp1, "tp2": tp2, "tp3": tp3,
                    "label": f"{tf}_AR_LO", "weight": wt * 2, "tf": tf,
                })

        # 10. Dynamic OTE (Fibonacci 0.62-0.786)
        for z in ote_fib_zones(want_dir, candles, tf, atr, wt, price,
                               pip, sl_min_pips, sl_max_pips, tp_max_pips):
            zones.append(z)

        # 11. Liquidity Voids
        for lv in structure.get("liq_voids", []):
            z = liq_void_zone(lv, want_dir, price, buf, wt * 0.9, tf,
                             pip, sl_min_pips, sl_max_pips)
            if z:
                zones.append(z)

        # 12. EQL / EQH / IDM unswept
        for lz in getattr(ict, "liquidity_zones", []) or []:
            if lz.get("swept"):
                continue
            if lz.get("type", "") in ("EQH", "EQL", "IDM_buy", "IDM_sell"):
                z = unswept_liq_zone(lz, want_dir, price, atr, wt, tf,
                                    pip, sl_min_pips, sl_max_pips)
                if z:
                    zones.append(z)

        # 13. Dealing Range Equilibrium
        dr = structure.get("dealing_range", {})
        if dr and float(dr.get("eq", 0)) > 0:
            z = dealing_eq_zone(dr, want_dir, price, atr, wt, tf,
                               pip, sl_min_pips, sl_max_pips)
            if z:
                zones.append(z)

        # 14. BOS points → S/R retest
        for bp in structure.get("bos_points", []):
            z = bos_sr_zone(bp, want_dir, price, atr, wt * 0.8, tf,
                           pip, sl_min_pips, sl_max_pips)
            if z:
                zones.append(z)

        # 15. CISD level
        cisd_on = structure.get("cisd", False)
        cisd_lv = float(structure.get("cisd_level", 0))
        cisd_dir = structure.get("cisd_dir", "none")
        if cisd_on and cisd_lv > 0:
            want = "bullish" if want_dir == "buy" else "bearish"
            if cisd_dir == want:
                z = bos_sr_zone(
                    {"dir": cisd_dir, "price": cisd_lv},
                    want_dir, price, atr, wt * 1.2, tf,
                    pip, sl_min_pips, sl_max_pips,
                )
                if z:
                    z["label"] = f"{tf}_CISD"
                    zones.append(z)

    # Apply multipliers + structural refinement
    composite = trend_bonus * sb_bonus * htf_bonus
    tf_candles = {row[0]: row[3] for row in tf_ict_data}
    for z in zones:
        z["weight"] = round(z["weight"] * composite, 2)
        z["htf_conf"] = htf_conf
        z["silver_bullet"] = silver_bullet
        z_tf = z.get("tf", "")
        if z_tf != "M1":
            cdl = tf_candles.get(z_tf)
            if cdl is None:
                cdl = tf_candles.get("H1")
            sl_p = abs(z["entry"] - z["sl"]) / pip
            z["tp1"], z["tp2"], z["tp3"] = swing_tp_targets(
                z["entry"], z["direction"], sl_p, cdl, tp_max_pips, pip,
            )
            refined = structural_sl(z["direction"], z["entry"], cdl, z["sl"],
                                    pip, sl_min_pips, sl_max_pips)
            if refined is not None:
                z["sl"] = refined

    # Dedup within `dedup_buffer_price` price units; keep highest-weight zone
    deduped: list[dict] = []
    for z in sorted(zones, key=lambda x: -x["weight"]):
        if not any(abs(d["entry"] - z["entry"]) < dedup_buffer_price for d in deduped):
            deduped.append(z)

    return deduped[:max_zones]
