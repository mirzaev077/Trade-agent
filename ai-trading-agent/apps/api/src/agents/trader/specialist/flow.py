"""
FlowAgent — Range / manipulation zone continuation trades.

Activates when FLOW mode (D1/H4 diverged or ranging).
Score system: 0–4, minimum 3 required.
Risk: 0.5% | SL: 10–25 pip | TP: 30 / 60 / 120 pip
Target: 15–20 trades/day
"""
from dataclasses import dataclass


@dataclass
class FlowDecision:
    should_trade: bool
    score: int              # 0–4 (need ≥3)
    score_breakdown: dict
    risk_pct: float         # 0.005 (0.5%)
    sl_min: int
    sl_max: int
    tp1_pips: int
    tp2_pips: int
    tp3_pips: int
    reason: str = ""


class FlowAgent:
    """
    Step 5b of pipeline: scores setup for FLOW mode.

    Scoring (max 4):
    +1 — Liquidity sweep confirmed (mandatory prerequisite)
    +1 — Micro displacement (M5/M15 impulse)
    +1 — OB / FVG / BB zone aligned with direction
    +1 — Structure break (BOS on H1 or M15)

    Minimum score: 3 (all 4 conditions OR any 3 of 4)
    Note: sweep=0 → automatically fails (score cannot reach 3)
    """

    RISK_PCT  = 0.005  # 0.5% per trade
    SL_MIN    = 40     # pips
    SL_MAX    = 50
    TP1       = 40
    TP2       = 100
    TP3       = 150
    MIN_SCORE = 3

    def decide(
        self,
        liq_swept: bool,
        micro_disp: bool,     # displacement on M5/M15
        zone_ok: bool,        # OB/FVG/BB found for this direction
        struct_break: bool,   # BOS on H1 or M15
    ) -> FlowDecision:
        b: dict = {}

        b["liq_sweep"]       = 1 if liq_swept    else 0
        b["displacement"]    = 1 if micro_disp   else 0
        b["zone"]            = 1 if zone_ok      else 0
        b["struct_break"]    = 1 if struct_break else 0

        score = sum(b.values())

        reason = (
            f"sweep={int(liq_swept)} disp={int(micro_disp)} "
            f"zone={int(zone_ok)} struct={int(struct_break)}"
        )

        return FlowDecision(
            should_trade=(score >= self.MIN_SCORE),
            score=score,
            score_breakdown=b,
            risk_pct=self.RISK_PCT,
            sl_min=self.SL_MIN,
            sl_max=self.SL_MAX,
            tp1_pips=self.TP1,
            tp2_pips=self.TP2,
            tp3_pips=self.TP3,
            reason=reason,
        )
