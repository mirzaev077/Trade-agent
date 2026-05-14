import pandas as pd
import numpy as np
from datetime import datetime, timezone
from ..models.signals import ICTSignal
from ..utils.session_times import get_current_session, is_kill_zone


class ICTAnalysis:
    """
    Full ICT Concepts Engine — all 35+ key levels:
    OB · BB · MB · RB · FVG · IFVG · BISI · SIBI · BPR
    EQH · EQL · BSL · SSL · IRL · ERL · LiqVoid
    BOS · CHoCH · MSS · Displacement · CISD · Inducement
    Premium/Discount · Equilibrium · OTE · Dealing Range
    Judas Swing · Stop Hunt · Power of 3 · MMXM
    PDH/PDL · Asian Range · Kill Zone · Silver Bullet
    Reaccumulation · Redistribution
    """

    def analyze(self, candles: pd.DataFrame, timeframe: str = "M15") -> ICTSignal:
        if candles is None or len(candles) < 50:
            return ICTSignal()

        structure    = self._market_structure(candles)
        obs          = self._order_blocks(candles, structure)
        breakers     = self._breaker_blocks(candles, obs, structure)
        mitigations  = self._mitigation_blocks(candles, obs)
        rejections   = self._rejection_blocks(candles)
        fvgs         = self._fair_value_gaps(candles)
        ifvgs        = self._inverse_fvgs(candles, fvgs)
        bisi_sibi    = self._bisi_sibi(candles)
        bpr          = self._balanced_price_range(fvgs)
        liquidity    = self._liquidity_pools(candles, structure)
        liq_voids    = self._liquidity_voids(candles)
        inducement   = self._inducement(candles, structure)
        eq_zones     = self._equal_highs_lows(candles)
        dealing_rng  = self._dealing_range(candles, structure)
        pd_zone      = self._premium_discount(candles)
        mmxm         = self._mmxm_phase(candles, structure, liquidity)
        displacement = self._displacement(candles)
        cisd         = self._cisd(candles, structure)
        judas        = self._judas_swing(candles)
        stop_hunt    = self._stop_hunt(candles, structure)
        reaccum      = self._reaccumulation(candles, structure)
        pdh_pdl      = self._pdh_pdl(candles)
        asian_range  = self._asian_range(candles)
        ote          = self._ote_zone(candles, structure)
        po3          = self._power_of_3(candles, structure)
        silver       = self._silver_bullet()
        trendlines   = self._trendlines(candles, structure)

        # All zones: OB + BB + MB + RB + FVG + IFVG + BISI/SIBI + BPR + EQH/EQL + LiqVoid
        all_fvgs    = (fvgs + ifvgs + bisi_sibi + bpr)[:12]
        all_liq     = (liquidity + eq_zones + inducement)[:10]

        signal_type = self._build_signal(
            structure, obs, breakers, mitigations, rejections,
            fvgs, ifvgs, pd_zone, mmxm, displacement, cisd,
            judas, pdh_pdl, asian_range, ote, po3, silver, liquidity,
            stop_hunt, reaccum,
        )
        confidence = self._confidence(
            structure, obs, breakers, fvgs, mmxm,
            displacement, cisd, judas, ote, silver, stop_hunt,
        )

        return ICTSignal(
            signal_type=signal_type,
            structure={
                "trend":            structure["trend"],
                "bos":              structure["bos_count"],
                "choch":            structure["choch"],
                "bos_points":       structure["bos_points"],
                "mmxm":             mmxm["phase"],
                "mmxm_dir":         mmxm["direction"],
                "mmxm_swept":       mmxm["swept"],
                "pd_zone":          pd_zone,
                "dealing_range":    dealing_rng,
                "displacement":     displacement["detected"],
                "displacement_dir": displacement.get("direction", "none"),
                "displacement_atr": displacement.get("atr_mult", 0.0),
                "cisd":             cisd["detected"],
                "cisd_dir":         cisd.get("direction", "none"),
                "cisd_level":       cisd.get("level", 0),
                "judas":            judas["detected"],
                "judas_dir":        judas.get("direction", "none"),
                "stop_hunt":        stop_hunt["detected"],
                "stop_hunt_dir":    stop_hunt.get("direction", "none"),
                "reaccum":          reaccum["type"],
                "po3_phase":        po3["phase"],
                "silver_bullet":    silver,
                "pdh":              pdh_pdl.get("pdh", 0),
                "pdl":              pdh_pdl.get("pdl", 0),
                "asian_high":       asian_range.get("high", 0),
                "asian_low":        asian_range.get("low", 0),
                "ote_zone":         ote,
                "trendlines":       trendlines,
                "liq_voids":        liq_voids[:4],
                "mitigation_blocks": mitigations[:4],
                "rejection_blocks": rejections[:4],
            },
            order_blocks=obs[:8],
            breaker_blocks=breakers[:4],
            fvgs=all_fvgs,
            liquidity_zones=all_liq,
            kill_zone=get_current_session(),
            pd_zone=pd_zone,
            confidence=confidence,
        )

    # ── Market Structure (BOS / CHoCH / MSS) ─────────────────────

    def _market_structure(self, candles: pd.DataFrame) -> dict:
        swings = []
        n = len(candles)
        for i in range(3, n - 3):
            h = float(candles.iloc[i]["high"])
            l = float(candles.iloc[i]["low"])
            if all(h >= float(candles.iloc[i+k]["high"]) for k in [-3,-2,-1,1,2,3] if 0 <= i+k < n):
                swings.append({"type": "high", "price": h, "idx": i})
            if all(l <= float(candles.iloc[i+k]["low"]) for k in [-3,-2,-1,1,2,3] if 0 <= i+k < n):
                swings.append({"type": "low",  "price": l, "idx": i})

        trend     = self._ema_trend(candles)
        bos_list  = []
        choch     = False
        bos_count = 0

        highs = [s for s in swings if s["type"] == "high"]
        lows  = [s for s in swings if s["type"] == "low"]

        if len(highs) >= 2:
            if highs[-1]["price"] > highs[-2]["price"]:
                bos_list.append({"dir": "bullish", "price": highs[-1]["price"], "idx": highs[-1]["idx"]})
                bos_count += 1
            elif highs[-1]["price"] < highs[-2]["price"] and trend == "bullish":
                choch = True

        if len(lows) >= 2:
            if lows[-1]["price"] < lows[-2]["price"]:
                bos_list.append({"dir": "bearish", "price": lows[-1]["price"], "idx": lows[-1]["idx"]})
                bos_count += 1
            elif lows[-1]["price"] > lows[-2]["price"] and trend == "bearish":
                choch = True

        return {
            "trend": trend, "swings": swings,
            "bos_points": bos_list, "bos_count": bos_count,
            "choch": choch, "highs": highs, "lows": lows,
        }

    def _ema_trend(self, candles: pd.DataFrame) -> str:
        c     = candles["close"].astype(float)
        n     = len(c)
        e21   = float(c.ewm(span=21,  adjust=False).mean().iloc[-1])
        e50   = float(c.ewm(span=50,  adjust=False).mean().iloc[-1])
        e200  = float(c.ewm(span=200, adjust=False).mean().iloc[-1]) if n >= 100 else e50
        price = float(c.iloc[-1])

        # EMA signals (lagging, 0-4 ball)
        bull_ema = sum([e21 > e50, e50 > e200, price > e21, price > e200])
        bear_ema = sum([e21 < e50, e50 < e200, price < e21, price < e200])

        # Oxirgi 10 sham momentum (tezkor)
        if n >= 12:
            recent    = c.iloc[-10:].values
            up_bars   = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i-1])
            down_bars = 10 - up_bars
            momentum  = "bullish" if up_bars >= 7 else ("bearish" if down_bars >= 7 else "neutral")
        else:
            momentum = "neutral"

        # Swing structure: HH+HL = uptrend, LL+LH = downtrend
        hi = candles["high"].astype(float).values
        lo = candles["low"].astype(float).values
        swing_hi = [hi[i] for i in range(2, n - 1) if hi[i] > hi[i-1] and hi[i] > hi[i+1]][-3:]
        swing_lo = [lo[i] for i in range(2, n - 1) if lo[i] < lo[i-1] and lo[i] < lo[i+1]][-3:]
        hh = len(swing_hi) >= 2 and swing_hi[-1] > swing_hi[-2]
        hl = len(swing_lo) >= 2 and swing_lo[-1] > swing_lo[-2]
        ll = len(swing_lo) >= 2 and swing_lo[-1] < swing_lo[-2]
        lh = len(swing_hi) >= 2 and swing_hi[-1] < swing_hi[-2]
        swing_bull = hh and hl
        swing_bear = ll and lh

        # Yig'indi: EMA(0-2) + momentum(0-1) + swing(0-2) = max 5
        bull_score = (2 if bull_ema >= 3 else (1 if bull_ema == 2 else 0)) + \
                     (1 if momentum == "bullish" else 0) + \
                     (2 if swing_bull else 0)
        bear_score = (2 if bear_ema >= 3 else (1 if bear_ema == 2 else 0)) + \
                     (1 if momentum == "bearish" else 0) + \
                     (2 if swing_bear else 0)

        if bull_score >= 3 and bull_score > bear_score: return "bullish"
        if bear_score >= 3 and bear_score > bull_score: return "bearish"
        return "sideways"

    # ── Order Blocks ──────────────────────────────────────────────

    def _order_blocks(self, candles: pd.DataFrame, structure: dict) -> list:
        obs = []
        atr = self._atr(candles)
        current = float(candles.iloc[-1]["close"])

        for bos in structure["bos_points"]:
            idx   = bos["idx"]
            start = max(0, idx - 20)

            if bos["dir"] == "bullish":
                for i in range(idx - 1, start, -1):
                    c = candles.iloc[i]
                    if float(c["close"]) < float(c["open"]):
                        body = float(c["open"]) - float(c["close"])
                        if body > atr * 0.15:
                            strength = min(body / atr, 3.0)
                            hi = float(c["open"])
                            lo = float(c["low"])
                            obs.append({
                                "type": "bullish_ob", "strength": round(strength, 2),
                                "high": hi, "low": lo, "mid": (hi + lo) / 2,
                                "idx": i, "body": body,
                                "mitigated": current < lo,
                            })
                            break

            elif bos["dir"] == "bearish":
                for i in range(idx - 1, start, -1):
                    c = candles.iloc[i]
                    if float(c["close"]) > float(c["open"]):
                        body = float(c["close"]) - float(c["open"])
                        if body > atr * 0.15:
                            strength = min(body / atr, 3.0)
                            hi = float(c["high"])
                            lo = float(c["open"])
                            obs.append({
                                "type": "bearish_ob", "strength": round(strength, 2),
                                "high": hi, "low": lo, "mid": (hi + lo) / 2,
                                "idx": i, "body": body,
                                "mitigated": current > hi,
                            })
                            break

        # Additional OBs from strong swing points (not just BOS)
        for sw in structure["swings"][-20:]:
            idx = sw["idx"]
            if idx >= len(candles) - 1:
                continue
            c = candles.iloc[idx]
            hi = float(c["high"]); lo = float(c["low"])
            body = abs(float(c["close"]) - float(c["open"]))
            if body < atr * 0.2:
                continue
            if sw["type"] == "low" and float(c["close"]) > float(c["open"]):
                if not any(abs(o["low"] - lo) < atr * 0.3 for o in obs):
                    obs.append({
                        "type": "bullish_ob", "strength": round(min(body/atr, 2.0), 2),
                        "high": float(c["open"]), "low": lo, "mid": (float(c["open"]) + lo) / 2,
                        "idx": idx, "body": body,
                        "mitigated": current < lo,
                    })
            elif sw["type"] == "high" and float(c["close"]) < float(c["open"]):
                if not any(abs(o["high"] - hi) < atr * 0.3 for o in obs):
                    obs.append({
                        "type": "bearish_ob", "strength": round(min(body/atr, 2.0), 2),
                        "high": hi, "low": float(c["open"]), "mid": (hi + float(c["open"])) / 2,
                        "idx": idx, "body": body,
                        "mitigated": current > hi,
                    })

        return sorted(obs, key=lambda o: (o["mitigated"], -o["strength"]))

    # ── Breaker Blocks ────────────────────────────────────────────

    def _breaker_blocks(self, candles: pd.DataFrame, obs: list, structure: dict) -> list:
        breakers = []
        current  = float(candles.iloc[-1]["close"])
        trend    = structure.get("trend", "sideways")
        atr      = self._atr(candles)

        for ob in obs:
            body = ob.get("body", 0)

            if ob["type"] == "bullish_ob" and current < ob["low"]:
                bb_type   = "bearish_breaker"
                direction = "sell"
                htf_align = 1.0 if trend == "bearish" else (0.6 if trend == "sideways" else 0.2)
            elif ob["type"] == "bearish_ob" and current > ob["high"]:
                bb_type   = "bullish_breaker"
                direction = "buy"
                htf_align = 1.0 if trend == "bullish" else (0.6 if trend == "sideways" else 0.2)
            else:
                continue

            structural   = min(ob.get("strength", 1.0) / 3.0, 1.0)
            displacement = min(body / (atr * 2.5), 1.0) if atr > 0 else 0.5
            zone_mid     = (ob["high"] + ob["low"]) / 2
            dist_atr     = abs(current - zone_mid) / atr if atr > 0 else 1.0
            retest       = max(0.0, 1.0 - dist_atr * 0.4)

            rank_score = (htf_align * 0.30 + structural * 0.30 +
                          displacement * 0.25 + retest * 0.15)
            grade = "A" if rank_score >= 0.70 else ("B" if rank_score >= 0.50 else "C")

            breakers.append({
                "type": bb_type, "direction": direction,
                "high": ob["high"], "low": ob["low"], "mid": ob["mid"],
                "idx": ob["idx"], "rank_score": round(rank_score, 2), "grade": grade,
                "mitigated": False,
            })

        return sorted(breakers, key=lambda b: (-{"A":3,"B":2,"C":1}.get(b["grade"],0), -b["rank_score"]))

    # ── Mitigation Blocks ─────────────────────────────────────────

    def _mitigation_blocks(self, candles: pd.DataFrame, obs: list) -> list:
        mit = []
        current = float(candles.iloc[-1]["close"])
        for ob in obs:
            if ob["mitigated"]:
                if ob["type"] == "bullish_ob" and current > ob["low"]:
                    mit.append({"type": "bullish_mitigation",
                                "high": ob["high"], "low": ob["low"], "mid": ob["mid"],
                                "idx": ob["idx"], "mitigated": False})
                elif ob["type"] == "bearish_ob" and current < ob["high"]:
                    mit.append({"type": "bearish_mitigation",
                                "high": ob["high"], "low": ob["low"], "mid": ob["mid"],
                                "idx": ob["idx"], "mitigated": False})
        return mit

    # ── Rejection Blocks ──────────────────────────────────────────

    def _rejection_blocks(self, candles: pd.DataFrame) -> list:
        rbs  = []
        atr  = self._atr(candles)
        data = candles.tail(80)
        for i in range(len(data)):
            c         = data.iloc[i]
            body      = abs(float(c["close"]) - float(c["open"]))
            wick_up   = float(c["high"])  - max(float(c["close"]), float(c["open"]))
            wick_down = min(float(c["close"]), float(c["open"])) - float(c["low"])
            if wick_up > atr * 0.4 and wick_up > body * 2:
                rbs.append({"type": "bearish_rejection",
                            "high": float(c["high"]), "low": float(c["high"]) - wick_up * 0.5,
                            "price": float(c["high"]), "wick": wick_up, "idx": i, "mitigated": False})
            if wick_down > atr * 0.4 and wick_down > body * 2:
                rbs.append({"type": "bullish_rejection",
                            "high": float(c["low"]) + wick_down * 0.5, "low": float(c["low"]),
                            "price": float(c["low"]), "wick": wick_down, "idx": i, "mitigated": False})
        return sorted(rbs, key=lambda r: -r["wick"])[:6]

    # ── Fair Value Gaps ───────────────────────────────────────────

    def _fair_value_gaps(self, candles: pd.DataFrame) -> list:
        fvgs = []
        atr  = self._atr(candles)
        n    = len(candles)
        for i in range(1, n - 1):
            ph = float(candles.iloc[i-1]["high"])
            pl = float(candles.iloc[i-1]["low"])
            nh = float(candles.iloc[i+1]["high"])
            nl = float(candles.iloc[i+1]["low"])
            if ph < nl:
                gap = nl - ph
                if gap >= atr * 0.08:
                    ce = (ph + nl) / 2
                    fvgs.append({
                        "type": "bullish_fvg",
                        "high": nl, "low": ph,          # high/low aliases
                        "top": nl, "bottom": ph, "ce": ce,
                        "gap": gap, "idx": i, "filled": False,
                    })
            if pl > nh:
                gap = pl - nh
                if gap >= atr * 0.08:
                    ce = (nh + pl) / 2
                    fvgs.append({
                        "type": "bearish_fvg",
                        "high": pl, "low": nh,
                        "top": pl, "bottom": nh, "ce": ce,
                        "gap": gap, "idx": i, "filled": False,
                    })

        current = float(candles.iloc[-1]["close"])
        for f in fvgs:
            if f["low"] <= current <= f["high"]:
                f["filled"] = True

        return sorted(fvgs, key=lambda f: (-f["gap"], f["filled"]))[:12]

    # ── Inverse FVG ───────────────────────────────────────────────

    def _inverse_fvgs(self, candles: pd.DataFrame, fvgs: list) -> list:
        current = float(candles.iloc[-1]["close"])
        ifvgs   = []
        for f in fvgs:
            if f["filled"]:
                if "bullish" in f["type"] and current < f["low"]:
                    ifvgs.append({
                        "type": "ifvg_bearish",
                        "high": f["high"], "low": f["low"], "ce": f["ce"],
                        "gap": f["gap"], "filled": False,
                    })
                elif "bearish" in f["type"] and current > f["high"]:
                    ifvgs.append({
                        "type": "ifvg_bullish",
                        "high": f["high"], "low": f["low"], "ce": f["ce"],
                        "gap": f["gap"], "filled": False,
                    })
        return ifvgs[:4]

    # ── BISI / SIBI ───────────────────────────────────────────────

    def _bisi_sibi(self, candles: pd.DataFrame) -> list:
        """
        BISI = Buy Side Imbalance Sell Side Inefficiency (bearish FVG after up move)
        SIBI = Sell Side Imbalance Buy Side Inefficiency (bullish FVG after down move)
        These are FVGs created by strong displacement candles.
        """
        result = []
        atr    = self._atr(candles)
        n      = len(candles)

        for i in range(2, n - 1):
            c    = candles.iloc[i]
            body = abs(float(c["close"]) - float(c["open"]))
            if body < atr * 0.8:   # Must be strong displacement candle
                continue

            if float(c["close"]) > float(c["open"]):  # Bullish displacement
                # SIBI: gap between prev candle high and current candle low
                prev_hi = float(candles.iloc[i-1]["high"])
                curr_lo = float(c["low"])
                if curr_lo > prev_hi:
                    gap = curr_lo - prev_hi
                    result.append({
                        "type": "sibi_bullish",
                        "high": curr_lo, "low": prev_hi,
                        "ce": (prev_hi + curr_lo) / 2,
                        "gap": gap, "idx": i, "filled": False,
                    })
            else:  # Bearish displacement
                # BISI: gap between current candle high and prev candle low
                prev_lo = float(candles.iloc[i-1]["low"])
                curr_hi = float(c["high"])
                if prev_lo > curr_hi:
                    gap = prev_lo - curr_hi
                    result.append({
                        "type": "bisi_bearish",
                        "high": prev_lo, "low": curr_hi,
                        "ce": (curr_hi + prev_lo) / 2,
                        "gap": gap, "idx": i, "filled": False,
                    })

        current = float(candles.iloc[-1]["close"])
        for r in result:
            if r["low"] <= current <= r["high"]:
                r["filled"] = True

        return sorted(result, key=lambda r: -r["gap"])[:6]

    # ── Balanced Price Range (BPR) ────────────────────────────────

    def _balanced_price_range(self, fvgs: list) -> list:
        """BPR = overlap between a bullish FVG and a bearish FVG. Strongest magnet."""
        bprs    = []
        bull_fvgs = [f for f in fvgs if "bullish" in f["type"] and not f["filled"]]
        bear_fvgs = [f for f in fvgs if "bearish" in f["type"] and not f["filled"]]

        for bf in bull_fvgs:
            for sf in bear_fvgs:
                # Find overlap
                overlap_lo = max(bf["low"], sf["low"])
                overlap_hi = min(bf["high"], sf["high"])
                if overlap_lo < overlap_hi:
                    ce = (overlap_lo + overlap_hi) / 2
                    bprs.append({
                        "type": "bpr",
                        "high": overlap_hi, "low": overlap_lo, "ce": ce,
                        "gap": overlap_hi - overlap_lo, "filled": False,
                    })
        return bprs[:3]

    # ── Liquidity Pools (BSL / SSL / EQH / EQL) ──────────────────

    def _liquidity_pools(self, candles: pd.DataFrame, structure: dict) -> list:
        pools  = []
        atr    = self._atr(candles)
        thresh = atr * 0.15
        highs  = candles["high"].astype(float).values
        lows   = candles["low"].astype(float).values
        n      = len(candles)

        for i in range(10, n - 5):
            eq_h = sum(1 for j in range(max(0, i-30), i) if abs(highs[j]-highs[i]) < thresh)
            if eq_h >= 2:
                pools.append({
                    "type": "BSL",   # Buy Side Liquidity (above = resting sells)
                    "price": float(highs[i]), "high": float(highs[i] + thresh),
                    "low": float(highs[i] - thresh),
                    "idx": i, "touches": eq_h, "swept": False,
                })
            eq_l = sum(1 for j in range(max(0, i-30), i) if abs(lows[j]-lows[i]) < thresh)
            if eq_l >= 2:
                pools.append({
                    "type": "SSL",   # Sell Side Liquidity (below = resting buys)
                    "price": float(lows[i]), "high": float(lows[i] + thresh),
                    "low": float(lows[i] - thresh),
                    "idx": i, "touches": eq_l, "swept": False,
                })

        # PDH/PDL as ERL (External Range Liquidity)
        pdh = structure.get("highs", [{}])
        pdl = structure.get("lows",  [{}])
        if len(pdh) >= 2:
            pools.append({
                "type": "ERL_high", "price": pdh[-2]["price"],
                "high": pdh[-2]["price"] + atr * 0.1,
                "low":  pdh[-2]["price"] - atr * 0.1,
                "idx": pdh[-2].get("idx", 0), "touches": 1, "swept": False,
            })
        if len(pdl) >= 2:
            pools.append({
                "type": "ERL_low", "price": pdl[-2]["price"],
                "high": pdl[-2]["price"] + atr * 0.1,
                "low":  pdl[-2]["price"] - atr * 0.1,
                "idx": pdl[-2].get("idx", 0), "touches": 1, "swept": False,
            })

        # Mark swept — so'nggi 10 sham (avval 5 edi, ko'p sweeplar o'tkazib yuborilardi)
        recent_high = float(candles.tail(10)["high"].max())
        recent_low  = float(candles.tail(10)["low"].min())
        for p in pools:
            if p["type"] in ("BSL", "ERL_high") and recent_high > p["price"]:
                p["swept"] = True
            if p["type"] in ("SSL", "ERL_low") and recent_low < p["price"]:
                p["swept"] = True

        return sorted(pools, key=lambda p: (-p["touches"], p["swept"]))[:10]

    # ── Equal Highs / Equal Lows ──────────────────────────────────

    def _equal_highs_lows(self, candles: pd.DataFrame) -> list:
        """EQH/EQL = two or more swing highs/lows at nearly the same price."""
        result = []
        atr    = self._atr(candles)
        thresh = atr * 0.12
        n      = len(candles)

        swing_highs = []
        swing_lows  = []
        for i in range(3, n - 3):
            h = float(candles.iloc[i]["high"])
            l = float(candles.iloc[i]["low"])
            if all(h >= float(candles.iloc[i+k]["high"]) for k in [-3,-2,-1,1,2,3] if 0<=i+k<n):
                swing_highs.append((i, h))
            if all(l <= float(candles.iloc[i+k]["low"])  for k in [-3,-2,-1,1,2,3] if 0<=i+k<n):
                swing_lows.append((i, l))

        # Group equal highs
        for i, (idx1, h1) in enumerate(swing_highs):
            for idx2, h2 in swing_highs[i+1:]:
                if abs(h1 - h2) < thresh and idx2 - idx1 > 5:
                    avg = (h1 + h2) / 2
                    result.append({
                        "type": "EQH",   # Equal Highs = BSL above
                        "price": avg,
                        "high": avg + thresh * 0.5,
                        "low":  avg - thresh * 0.5,
                        "idx": idx2, "touches": 2, "swept": False,
                    })

        # Group equal lows
        for i, (idx1, l1) in enumerate(swing_lows):
            for idx2, l2 in swing_lows[i+1:]:
                if abs(l1 - l2) < thresh and idx2 - idx1 > 5:
                    avg = (l1 + l2) / 2
                    result.append({
                        "type": "EQL",   # Equal Lows = SSL below
                        "price": avg,
                        "high": avg + thresh * 0.5,
                        "low":  avg - thresh * 0.5,
                        "idx": idx2, "touches": 2, "swept": False,
                    })

        # Mark swept
        recent_high = float(candles.tail(10)["high"].max())
        recent_low  = float(candles.tail(10)["low"].min())
        for r in result:
            if r["type"] == "EQH" and recent_high > r["price"]:
                r["swept"] = True
            if r["type"] == "EQL" and recent_low < r["price"]:
                r["swept"] = True

        return result[:8]

    # ── Liquidity Voids ───────────────────────────────────────────

    def _liquidity_voids(self, candles: pd.DataFrame) -> list:
        """Large single-candle moves with minimal overlap = price moves fast here."""
        voids = []
        atr   = self._atr(candles)
        n     = len(candles)

        for i in range(1, n - 1):
            c    = candles.iloc[i]
            body = abs(float(c["close"]) - float(c["open"]))
            hi   = float(c["high"])
            lo   = float(c["low"])

            if body < atr * 2.0:  # Must be at least 2x ATR
                continue

            # Check minimal overlap with prev and next candle
            prev_hi = float(candles.iloc[i-1]["high"])
            prev_lo = float(candles.iloc[i-1]["low"])
            next_hi = float(candles.iloc[i+1]["high"])
            next_lo = float(candles.iloc[i+1]["low"])

            overlap_prev = min(hi, prev_hi) - max(lo, prev_lo)
            overlap_next = min(hi, next_hi) - max(lo, next_lo)

            if overlap_prev < body * 0.2 and overlap_next < body * 0.2:
                typ = "void_bull" if float(c["close"]) > float(c["open"]) else "void_bear"
                voids.append({
                    "type": typ,
                    "high": hi, "low": lo,
                    "body": body, "atr_mult": round(body / atr, 2),
                    "idx": i,
                })

        return sorted(voids, key=lambda v: -v["body"])[:4]

    # ── Inducement ────────────────────────────────────────────────

    def _inducement(self, candles: pd.DataFrame, structure: dict) -> list:
        """
        Inducement (IDM): Small swing high/low that traps retail traders
        before the real institutional move in the opposite direction.
        """
        result = []
        atr    = self._atr(candles)
        highs  = structure.get("highs", [])
        lows   = structure.get("lows",  [])
        trend  = structure.get("trend", "sideways")

        if trend == "bullish" and len(lows) >= 3:
            # In uptrend: small dip (higher low) before continuation = IDM
            for i in range(1, min(len(lows), 5)):
                l1 = lows[-i-1]["price"]
                l2 = lows[-i]["price"]
                if l2 > l1 and (l2 - l1) < atr * 1.5:  # Small pullback
                    result.append({
                        "type": "IDM_buy",
                        "price": l2,
                        "high": l2 + atr * 0.3,
                        "low":  l2 - atr * 0.3,
                        "idx": lows[-i]["idx"], "touches": 1, "swept": False,
                    })

        elif trend == "bearish" and len(highs) >= 3:
            for i in range(1, min(len(highs), 5)):
                h1 = highs[-i-1]["price"]
                h2 = highs[-i]["price"]
                if h2 < h1 and (h1 - h2) < atr * 1.5:
                    result.append({
                        "type": "IDM_sell",
                        "price": h2,
                        "high": h2 + atr * 0.3,
                        "low":  h2 - atr * 0.3,
                        "idx": highs[-i]["idx"], "touches": 1, "swept": False,
                    })

        return result[:4]

    # ── Balanced Price Range ───────────────────────────────────────
    # (already defined above, _balanced_price_range)

    # ── Dealing Range ─────────────────────────────────────────────

    def _dealing_range(self, candles: pd.DataFrame, structure: dict) -> dict:
        """
        Dealing Range = from last significant low to last significant high.
        Used to identify Premium (sell) and Discount (buy) zones.
        """
        highs = structure.get("highs", [])
        lows  = structure.get("lows",  [])
        if not highs or not lows:
            return {}

        dr_high = highs[-1]["price"]
        dr_low  = lows[-1]["price"]
        if dr_high <= dr_low:
            dr_high, dr_low = dr_low, dr_high

        dr_range = dr_high - dr_low
        if dr_range <= 0:
            return {}

        eq = (dr_high + dr_low) / 2   # Equilibrium
        premium_start = dr_low + dr_range * 0.618
        discount_end  = dr_low + dr_range * 0.382

        current = float(candles.iloc[-1]["close"])
        zone = "premium" if current >= premium_start else ("discount" if current <= discount_end else "equilibrium")

        return {
            "high": round(dr_high, 2),
            "low":  round(dr_low, 2),
            "eq":   round(eq, 2),
            "premium_start": round(premium_start, 2),
            "discount_end":  round(discount_end, 2),
            "current_zone":  zone,
        }

    # ── Premium / Discount / Equilibrium ─────────────────────────

    def _premium_discount(self, candles: pd.DataFrame) -> str:
        recent  = candles.tail(100)
        high    = float(recent["high"].max())
        low     = float(recent["low"].min())
        current = float(candles.iloc[-1]["close"])
        if high == low: return "neutral"
        pos = (current - low) / (high - low)
        if pos > 0.618: return "premium"
        if pos < 0.382: return "discount"
        if 0.45 <= pos <= 0.55: return "equilibrium"
        return "neutral"

    # ── MMXM Phase (Market Maker Buy/Sell Model) ──────────────────

    def _mmxm_phase(self, candles: pd.DataFrame, structure: dict, liquidity: list) -> dict:
        trend   = structure["trend"]
        current = float(candles.iloc[-1]["close"])
        recent  = candles.tail(20)
        high20  = float(recent["high"].max())
        low20   = float(recent["low"].min())
        prev    = candles.tail(40).head(20)
        prev_h  = float(prev["high"].max())
        prev_l  = float(prev["low"].min())

        swept_lows  = low20  < prev_l and current > prev_l
        swept_highs = high20 > prev_h and current < prev_h

        bsl_swept = any(p["type"] in ("BSL","ERL_high","EQH") and p["swept"] for p in liquidity)
        ssl_swept = any(p["type"] in ("SSL","ERL_low","EQL")  and p["swept"] for p in liquidity)

        pd_zone = self._premium_discount(candles)

        if swept_lows or ssl_swept:
            if trend in ("bullish", "sideways") or pd_zone == "discount":
                return {"phase": "bullish_distribution", "direction": "buy", "swept": True}

        if swept_highs or bsl_swept:
            if trend in ("bearish", "sideways") or pd_zone == "premium":
                return {"phase": "bearish_distribution", "direction": "sell", "swept": True}

        atr   = self._atr(candles)
        last5 = candles.tail(5)
        bear_body = sum(
            1 for i in range(len(last5))
            if float(last5.iloc[i]["close"]) < float(last5.iloc[i]["open"])
            and abs(float(last5.iloc[i]["close"]) - float(last5.iloc[i]["open"])) > atr * 0.5
        )
        bull_body = sum(
            1 for i in range(len(last5))
            if float(last5.iloc[i]["close"]) > float(last5.iloc[i]["open"])
            and abs(float(last5.iloc[i]["close"]) - float(last5.iloc[i]["open"])) > atr * 0.5
        )

        if bear_body >= 2 and pd_zone == "premium":
            return {"phase": "bearish_markdown", "direction": "sell", "swept": False}
        if bull_body >= 2 and pd_zone == "discount":
            return {"phase": "bullish_markup", "direction": "buy", "swept": False}

        if trend == "bullish":
            return {"phase": "bullish_markup",   "direction": "buy",  "swept": False}
        if trend == "bearish":
            return {"phase": "bearish_markdown", "direction": "sell", "swept": False}
        return {"phase": "accumulation", "direction": "none", "swept": False}

    # ── Displacement ──────────────────────────────────────────────

    def _displacement(self, candles: pd.DataFrame) -> dict:
        atr  = self._atr(candles)
        data = candles.tail(10)
        for i in range(len(data) - 1, 0, -1):
            c    = data.iloc[i]
            body = abs(float(c["close"]) - float(c["open"]))
            if body > atr * 1.5:
                direction = "bullish" if float(c["close"]) > float(c["open"]) else "bearish"
                return {"detected": True, "direction": direction,
                        "body": body, "atr_mult": round(body / atr, 2)}
        return {"detected": False, "direction": "none", "atr_mult": 0.0}

    # ── CISD ──────────────────────────────────────────────────────

    def _cisd(self, candles: pd.DataFrame, structure: dict) -> dict:
        if not structure["highs"] or not structure["lows"]:
            return {"detected": False, "direction": "none"}

        current = float(candles.iloc[-1]["close"])
        prev    = float(candles.iloc[-2]["close"])
        last_high = structure["highs"][-1]["price"] if structure["highs"] else 0
        last_low  = structure["lows"][-1]["price"]  if structure["lows"]  else 0

        if prev < last_high and current > last_high:
            return {"detected": True, "direction": "bullish", "level": last_high}
        if prev > last_low and current < last_low:
            return {"detected": True, "direction": "bearish", "level": last_low}
        return {"detected": False, "direction": "none"}

    # ── Judas Swing ───────────────────────────────────────────────

    def _judas_swing(self, candles: pd.DataFrame) -> dict:
        atr    = self._atr(candles)
        recent = candles.tail(6)
        if len(recent) < 4:
            return {"detected": False, "direction": "none"}

        high_recent = float(recent["high"].max())
        low_recent  = float(recent["low"].min())
        prev        = candles.tail(20).head(14)
        prev_h      = float(prev["high"].max())
        prev_l      = float(prev["low"].min())
        current     = float(candles.iloc[-1]["close"])

        if high_recent > prev_h + atr * 0.3 and current < prev_h:
            return {"detected": True, "direction": "bearish", "swept_level": prev_h}
        if low_recent < prev_l - atr * 0.3 and current > prev_l:
            return {"detected": True, "direction": "bullish", "swept_level": prev_l}
        return {"detected": False, "direction": "none"}

    # ── Stop Hunt ─────────────────────────────────────────────────

    def _stop_hunt(self, candles: pd.DataFrame, structure: dict) -> dict:
        """
        Stop Hunt = spike beyond a key level followed by immediate reversal.
        Similar to Judas but more aggressive — wick > 2x body.
        """
        atr  = self._atr(candles)
        n    = len(candles)
        for i in range(n - 5, n):
            if i < 1: continue
            c    = candles.iloc[i]
            body = abs(float(c["close"]) - float(c["open"]))
            wick_up   = float(c["high"]) - max(float(c["close"]), float(c["open"]))
            wick_down = min(float(c["close"]), float(c["open"])) - float(c["low"])

            if wick_up > body * 2.5 and wick_up > atr * 0.5:
                return {"detected": True, "direction": "bearish",
                        "spike_price": float(c["high"]), "wick": wick_up}
            if wick_down > body * 2.5 and wick_down > atr * 0.5:
                return {"detected": True, "direction": "bullish",
                        "spike_price": float(c["low"]), "wick": wick_down}
        return {"detected": False, "direction": "none"}

    # ── Reaccumulation / Redistribution ──────────────────────────

    def _reaccumulation(self, candles: pd.DataFrame, structure: dict) -> dict:
        """
        Reaccumulation = tight range after bullish move (pause before continuation up).
        Redistribution = tight range after bearish move (pause before continuation down).
        """
        atr   = self._atr(candles)
        trend = structure.get("trend", "sideways")
        recent = candles.tail(20)
        rng = float(recent["high"].max()) - float(recent["low"].min())

        if rng < atr * 2.0:   # Tight range = consolidation
            if trend == "bullish":
                return {"type": "reaccumulation", "direction": "buy",
                        "range_high": float(recent["high"].max()),
                        "range_low": float(recent["low"].min())}
            if trend == "bearish":
                return {"type": "redistribution", "direction": "sell",
                        "range_high": float(recent["high"].max()),
                        "range_low": float(recent["low"].min())}
        return {"type": "none", "direction": "none"}

    # ── PDH / PDL ─────────────────────────────────────────────────

    def _pdh_pdl(self, candles: pd.DataFrame) -> dict:
        if len(candles) < 2:
            return {}
        try:
            if "time" in candles.columns:
                c = candles.copy()
                c["time"] = pd.to_datetime(c["time"], unit="s", utc=True)
                c["date"] = c["time"].dt.date
                days = sorted(c["date"].unique())
                if len(days) < 2:
                    return {}
                prev_day = c[c["date"] == days[-2]]
                return {
                    "pdh": round(float(prev_day["high"].max()), 2),
                    "pdl": round(float(prev_day["low"].min()),  2),
                }
        except Exception:
            pass
        day_candles = candles.head(min(288, len(candles) // 2))
        return {
            "pdh": round(float(day_candles["high"].max()), 2),
            "pdl": round(float(day_candles["low"].min()),  2),
        }

    # ── Asian Range ───────────────────────────────────────────────

    def _asian_range(self, candles: pd.DataFrame) -> dict:
        try:
            if "time" not in candles.columns:
                return {}
            c = candles.copy()
            c["time"] = pd.to_datetime(c["time"], unit="s", utc=True)
            # Asia session: 00:00-06:00 UTC (Tokyo + Singapore)
            asian = c[c["time"].dt.hour.between(0, 5)]
            if len(asian) < 3:
                asian = c.tail(48)
            return {
                "high": round(float(asian["high"].max()), 2),
                "low":  round(float(asian["low"].min()),  2),
            }
        except Exception:
            return {}

    # ── OTE Zone (62%–79% Fib retracement) ───────────────────────

    def _ote_zone(self, candles: pd.DataFrame, structure: dict) -> dict:
        highs = structure["highs"]
        lows  = structure["lows"]
        if not highs or not lows:
            return {"active": False}

        trend   = structure["trend"]
        current = float(candles.iloc[-1]["close"])

        if trend == "bullish" and len(lows) >= 2:
            swing_low  = lows[-2]["price"]
            swing_high = highs[-1]["price"] if highs else swing_low + 1
            r          = swing_high - swing_low
            ote_high   = round(swing_high - r * 0.62, 2)
            ote_low    = round(swing_high - r * 0.79, 2)
            return {"active": ote_low <= current <= ote_high, "direction": "buy",
                    "high": ote_high, "low": ote_low,
                    "swing_high": swing_high, "swing_low": swing_low}

        if trend == "bearish" and len(highs) >= 2:
            swing_high = highs[-2]["price"]
            swing_low  = lows[-1]["price"] if lows else swing_high - 1
            r          = swing_high - swing_low
            ote_low    = round(swing_low + r * 0.62, 2)
            ote_high   = round(swing_low + r * 0.79, 2)
            return {"active": ote_low <= current <= ote_high, "direction": "sell",
                    "high": ote_high, "low": ote_low,
                    "swing_high": swing_high, "swing_low": swing_low}

        return {"active": False}

    # ── Power of 3 ────────────────────────────────────────────────

    def _power_of_3(self, candles: pd.DataFrame, structure: dict) -> dict:
        recent  = candles.tail(30)
        atr     = self._atr(candles)
        h_vals  = recent["high"].astype(float).values
        l_vals  = recent["low"].astype(float).values
        c_vals  = recent["close"].astype(float).values

        acc_range = h_vals[:10].max() - l_vals[:10].min()
        mid_high  = h_vals[10:20].max()
        mid_low   = l_vals[10:20].min()
        dist_close = c_vals[-1]

        tight = acc_range < atr * 1.5
        spike = (mid_high > h_vals[:10].max() + atr * 0.5 or
                 mid_low  < l_vals[:10].min() - atr * 0.5)

        if tight and spike:
            direction = "buy" if dist_close > mid_low else "sell"
            return {"phase": "distribution", "direction": direction, "detected": True}
        if tight:
            return {"phase": "manipulation", "direction": "none", "detected": False}
        return {"phase": "accumulation", "direction": "none", "detected": False}

    # ── Silver Bullet ─────────────────────────────────────────────

    def _silver_bullet(self) -> bool:
        t = datetime.now(timezone.utc)
        cur = t.hour * 60 + t.minute
        # London SB: 10:00-11:00 UTC | NY SB: 15:00-16:00 UTC
        return (600 <= cur <= 660) or (900 <= cur <= 960)

    # ── Trendlines ────────────────────────────────────────────────

    def _trendlines(self, candles: pd.DataFrame, structure: dict) -> dict:
        highs   = structure["highs"][-6:] if len(structure["highs"]) >= 2 else []
        lows    = structure["lows"][-6:]  if len(structure["lows"])  >= 2 else []
        current = float(candles.iloc[-1]["close"])
        result  = {"bull_tl": False, "bear_tl": False,
                   "price_at_support": False, "price_at_resist": False}

        if len(lows) >= 2:
            l1, l2 = lows[-2], lows[-1]
            if l2["price"] > l1["price"]:
                slope     = (l2["price"] - l1["price"]) / max(l2["idx"] - l1["idx"], 1)
                projected = l2["price"] + slope * (len(candles) - 1 - l2["idx"])
                result["bull_tl"]          = True
                result["bull_tl_level"]    = round(projected, 2)
                result["price_at_support"] = abs(current - projected) < self._atr(candles) * 0.4

        if len(highs) >= 2:
            h1, h2 = highs[-2], highs[-1]
            if h2["price"] < h1["price"]:
                slope     = (h2["price"] - h1["price"]) / max(h2["idx"] - h1["idx"], 1)
                projected = h2["price"] + slope * (len(candles) - 1 - h2["idx"])
                result["bear_tl"]         = True
                result["bear_tl_level"]   = round(projected, 2)
                result["price_at_resist"] = abs(current - projected) < self._atr(candles) * 0.4

        return result

    # ── Signal Builder ────────────────────────────────────────────

    def _build_signal(
        self, structure, obs, breakers, mitigations, rejections,
        fvgs, ifvgs, pd_zone, mmxm, displacement, cisd,
        judas, pdh_pdl, asian_range, ote, po3, silver, liquidity,
        stop_hunt, reaccum,
    ) -> str:
        direction = mmxm["direction"]
        if direction == "none":
            # Try trend as fallback
            trend = structure["trend"]
            if trend == "bullish": direction = "buy"
            elif trend == "bearish": direction = "sell"
            else: return "none"

        trend = structure["trend"]
        score = 0

        if trend == ("bullish" if direction=="buy" else "bearish"): score += 2
        if structure["bos_count"] >= 1:                             score += 1
        if structure["choch"]:                                      score += 1

        align_ob = [o for o in obs if not o.get("mitigated") and
                    (("bullish" in o["type"] and direction=="buy") or
                     ("bearish" in o["type"] and direction=="sell"))]
        if align_ob:    score += 2

        align_br = [b for b in breakers if
                    ("bullish" in b["type"] and direction=="buy") or
                    ("bearish" in b["type"] and direction=="sell")]
        if align_br:    score += 1

        align_fvg = [f for f in fvgs if not f.get("filled") and
                     (("bullish" in f["type"] and direction=="buy") or
                      ("bearish" in f["type"] and direction=="sell"))]
        if align_fvg:   score += 1
        if ifvgs:       score += 1
        if mmxm["swept"]:    score += 2
        if stop_hunt["detected"] and stop_hunt["direction"] == direction: score += 2

        if displacement["detected"] and displacement["direction"] == (
            "bullish" if direction=="buy" else "bearish"): score += 1

        if cisd["detected"] and cisd["direction"] == (
            "bullish" if direction=="buy" else "bearish"): score += 1

        if judas["detected"] and judas["direction"] == direction: score += 2
        if ote.get("active") and ote.get("direction") == direction: score += 2
        if po3["detected"] and po3["direction"] == direction:       score += 1
        if silver:      score += 1
        if reaccum.get("direction") == direction: score += 1

        if direction == "buy"  and pd_zone in ("discount","equilibrium"): score += 1
        if direction == "sell" and pd_zone in ("premium","equilibrium"):  score += 1
        if is_kill_zone(): score += 1

        if score >= 10: return "strong_buy"  if direction=="buy" else "strong_sell"
        if score >= 5:  return "buy"         if direction=="buy" else "sell"
        if score >= 2:  return "buy"         if direction=="buy" else "sell"
        return "none"

    def _confidence(
        self, structure, obs, breakers, fvgs, mmxm,
        displacement, cisd, judas, ote, silver, stop_hunt,
    ) -> float:
        s = 0.0
        if structure["trend"] != "sideways":  s += 0.15
        if structure["bos_count"] >= 1:       s += 0.10
        if obs:                               s += 0.15
        if breakers:                          s += 0.10
        if fvgs:                              s += 0.10
        if mmxm["swept"]:                     s += 0.15
        if displacement["detected"]:          s += 0.10
        if stop_hunt["detected"]:             s += 0.05
        if cisd["detected"]:                  s += 0.05
        if judas["detected"]:                 s += 0.05
        if ote.get("active"):                 s += 0.05
        return round(min(s, 1.0), 2)

    def _atr(self, candles: pd.DataFrame, period: int = 14) -> float:
        h  = candles["high"].astype(float)
        l  = candles["low"].astype(float)
        c  = candles["close"].astype(float)
        tr = pd.concat([h-l, (h-c.shift(1)).abs(), (l-c.shift(1)).abs()], axis=1).max(axis=1)
        v  = tr.rolling(period, min_periods=1).mean().iloc[-1]
        return float(v) if not np.isnan(v) else 1.0
