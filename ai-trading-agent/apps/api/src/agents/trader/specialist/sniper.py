"""
SniperAgent — High-confidence trend continuation trades.

Activates when D1 + H4 aligned (SNIPER mode).
Score system: 0–10, minimum 7 required.
Risk: 1% | SL: 15–40 pip | TP: 50 / 100 / 200 pip
Target: 5–10 trades/day
"""
from dataclasses import dataclass


@dataclass
class SniperDecision:
    should_trade: bool
    score: int              # 0–10 (need ≥7)
    score_breakdown: dict   # per-factor scores
    risk_pct: float         # 0.01 (1%)
    sl_min: int             # pips
    sl_max: int             # pips
    tp1_pips: int
    tp2_pips: int
    tp3_pips: int
    reason: str = ""


class SniperAgent:
    """
    Step 5a of pipeline: scores setup for SNIPER mode.

    Scoring (max 10):
    +4 — HTF alignment (2 points per aligned TF, max 4)
    +2 — Liquidity sweep confirmed
    +2 — Impulse/displacement in trade direction
    +1 — OB/FVG/BB zone present
    +1 — Inside Kill Zone OR Silver Bullet window
    +0 bonus from struct_ok (captured via htf_conf)

    Minimum score: 7
    """

    RISK_PCT  = 0.01   # 1% per trade
    SL_MIN    = 40     # pips
    SL_MAX    = 50
    TP1       = 40
    TP2       = 100
    TP3       = 150
    MIN_SCORE = 7

    def decide(
        self,
        htf_conf: int,          # 0–4 (how many of D1/H4/H1/M30 agree)
        liq_swept: bool,
        impulse_ok: bool,       # M1/M5 impulse in trade direction
        ob_fvg_ok: bool,        # OB / FVG / BB zone found
        in_kz: bool,            # in Kill Zone or Silver Bullet
        struct_ok: bool,        # BOS or CHoCH at H4/H1
        manip_strength: float = 0.0,
    ) -> SniperDecision:
        b: dict = {}

        # HTF alignment: 2 pts per confirming TF, max 4
        b["htf_alignment"] = min(htf_conf * 2, 4)
        b["liq_sweep"]     = 2 if liq_swept    else 0
        b["impulse"]       = 2 if impulse_ok   else 0
        b["ob_fvg_zone"]   = 1 if ob_fvg_ok    else 0
        b["kill_zone"]     = 1 if in_kz        else 0

        score = min(sum(b.values()), 10)

        reason = (
            f"HTF={b['htf_alignment']}/4 sweep={int(liq_swept)} "
            f"impulse={int(impulse_ok)} OB/FVG={int(ob_fvg_ok)} "
            f"KZ={int(in_kz)} struct={int(struct_ok)}"
        )

        return SniperDecision(
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
