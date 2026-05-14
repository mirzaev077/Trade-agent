"""
HTFBiasAgent — D1 + H4 directional bias detector.

Determines: BUY bias / SELL bias / NEUTRAL
Assigns: SNIPER mode (D1+H4 aligned) or FLOW mode (diverged/ranging)
"""
from dataclasses import dataclass


@dataclass
class HTFBiasResult:
    bias: str           # "buy" | "sell" | "neutral"
    d1_trend: str       # "bullish" | "bearish" | "sideways"
    h4_trend: str
    h1_trend: str
    w1_trend: str       # "bullish" | "bearish" | "sideways" (Weekly)
    effective_bias: str # after W1→D1→H4→H1 fallback
    aligned: bool       # D1 + H4 same non-sideways direction
    w1_confirmed: bool  # W1 aligns with effective bias
    confidence: float   # 0.0–1.0
    mode: str           # "SNIPER" | "FLOW"
    bos_count: int      # total BOS across D1+H4
    choch: bool         # CHoCH at H4 level


class HTFBiasAgent:
    """
    Step 1 of pipeline: determines high-timeframe directional bias.

    Rules:
    - Primary: W1 → D1 (institutional weekly bias)
    - Fallback 1: H4 trend (if D1 sideways)
    - Fallback 2: H1 trend (if both D1+H4 sideways)
    - SNIPER: D1 + H4 both trend in same direction → high-confidence
    - FLOW: one or both sideways → range/manipulation zone
    - W1 confirmation adds +0.12 confidence bonus
    """

    def analyze(self, ict_d1, ict_h4, ict_h1, ict_w1=None) -> HTFBiasResult:
        w1_trend = ict_w1.structure.get("trend", "sideways") if ict_w1 else "sideways"
        d1_trend = ict_d1.structure.get("trend", "sideways") if ict_d1 else "sideways"
        h4_trend = ict_h4.structure.get("trend", "sideways")
        h1_trend = ict_h1.structure.get("trend", "sideways")

        d1_bos   = int(ict_d1.structure.get("bos", 0)) if ict_d1 else 0
        h4_bos   = int(ict_h4.structure.get("bos", 0))
        h4_choch = ict_h4.structure.get("choch", False)
        bos_total = d1_bos + h4_bos

        # Effective bias: W1 → D1 → H4 → H1 fallback chain
        effective = w1_trend if w1_trend != "sideways" else d1_trend
        if effective == "sideways" and h4_trend != "sideways":
            effective = h4_trend
        elif effective == "sideways" and h4_trend == "sideways" and h1_trend != "sideways":
            effective = h1_trend

        # H4 CHoCH + BOS + Displacement = confirmed structural flip (fast path)
        # D1 EMA lags by many candles — trust H4 structure break over D1 trend
        h4_disp = ict_h4.structure.get("displacement", False)
        if (h4_choch and h4_bos >= 1 and h4_disp
                and h4_trend not in ("sideways", "neutral")
                and h4_trend != effective):
            effective = h4_trend   # H4 CHoCH+BOS+Displacement → confirmed flip

        # H4 + H1 ikkalasi ham D1 ga qarshi bo'lsa → H4 ustun (D1 EMA lag)
        # Misol: D1=bearish lekin H4=bullish + H1=bullish → bu kechagi trendni o'zgartirdi
        elif (effective != "sideways"
                and h4_trend not in ("sideways", "neutral")
                and h1_trend not in ("sideways", "neutral")
                and h4_trend != effective
                and h1_trend == h4_trend):
            effective = h4_trend   # H4+H1 D1 ga qarshi → H4 yo'nalishga o'tish

        bias = (
            "buy"  if effective == "bullish" else
            "sell" if effective == "bearish" else
            "neutral"
        )

        # Alignment: D1 va H4 bir xil yo'nalishda
        aligned = (
            d1_trend not in ("sideways", "neutral") and
            h4_trend not in ("sideways", "neutral") and
            d1_trend == h4_trend
        )

        # W1 confirms when Weekly trend matches effective bias
        w1_confirmed = (
            w1_trend not in ("sideways", "neutral") and
            w1_trend == effective
        )

        # Confidence: W1 > D1 > H4 > H1, BOS bonus
        d1_w = 1.0 if d1_trend != "sideways" else 0.25
        h4_w = 1.0 if h4_trend != "sideways" else 0.25
        h1_w = 0.6 if h1_trend != "sideways" else 0.15
        bos_bonus = min(0.20, bos_total * 0.04)
        w1_bonus  = 0.12 if w1_confirmed else 0.0

        if aligned:
            confidence = d1_w * 0.45 + h4_w * 0.30 + h1_w * 0.13 + bos_bonus + w1_bonus
        else:
            confidence = max(d1_w, h4_w) * 0.50 + h1_w * 0.13 + w1_bonus

        confidence = round(min(confidence, 1.0), 2)
        mode = "SNIPER" if aligned else "FLOW"

        return HTFBiasResult(
            bias=bias,
            d1_trend=d1_trend,
            h4_trend=h4_trend,
            h1_trend=h1_trend,
            w1_trend=w1_trend,
            effective_bias=effective,
            aligned=aligned,
            w1_confirmed=w1_confirmed,
            confidence=confidence,
            mode=mode,
            bos_count=bos_total,
            choch=h4_choch,
        )
