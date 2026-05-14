"""
ConfluenceAgent — Institutional-grade final gate before order placement.

Scoring (0–20):
  HTF Bias       0–5  (D1=2, H4=2, H1=1)
  Liquidity Sweep 0–5  (confirmed=3, strength>0.75=1, type_bonus=1)
  Zone Quality   0–5  (zone=2, kill_zone=1, silver_bullet=1, depth=1)
  Structure      0–4  (BOS=2, CHoCH=1, displacement=1)
  DXY Correlation 0–1  (EURUSD trend inversely confirms direction)

Thresholds:
  SNIPER ≥ 13
  FLOW   ≥ 10
  Otherwise → REJECT

4 CORE PILLARS (ALL must score > 0):
  1. HTF Bias    — at least H4 aligned
  2. Sweep       — liquidity manipulation confirmed
  3. Zone        — OB / FVG / BB / OTE present
  4. Structure   — BOS or CHoCH present
"""
from dataclasses import dataclass


@dataclass
class ConfluenceResult:
    approved: bool
    total_score: int
    max_score: int                # 20
    minimum_required: int
    pillars_ok: bool
    htf_score: int                # 0–5
    sweep_score: int              # 0–5
    zone_score: int               # 0–5
    structure_score: int          # 0–4
    dxy_score: int                # 0–1
    breakdown: dict
    grade: str                    # "A+" | "A" | "B" | "C" | "FAIL"
    reason: str


class ConfluenceAgent:
    """
    Step 6 (final gate) of pipeline.

    SCORING BREAKDOWN
    ─────────────────
    HTF Bias (max 5):
      D1 aligned  → +2
      H4 aligned  → +2
      H1 aligned  → +1

    Liquidity Sweep (max 5):
      Sweep confirmed   → +3
      Strength > 0.75   → +1
      Type = stop_hunt or judas_swing → +1

    Zone Quality (max 5):
      OB/FVG/BB/OTE present → +2
      In Kill Zone          → +1
      In Silver Bullet      → +1
      HTF depth (D1+H4+H1)  → +1

    Structure (max 4):
      BOS confirmed     → +2
      CHoCH confirmed   → +1
      Displacement M5+  → +1

    4 PILLARS — any at 0 = FAIL:
      HTF ≥ 2 (at least H4)
      Sweep > 0
      Zone > 0
      Structure > 0
    """

    MAX_SCORE  = 20
    MIN_SNIPER = 13
    MIN_FLOW   = 10

    def score(
        self,
        # HTF Bias
        d1_bias: str,              # "bullish" | "bearish" | "sideways"
        h4_trend: str,
        h1_trend: str,
        direction: str,            # "buy" | "sell"
        # Sweep
        sweep_confirmed: bool,
        sweep_strength: float,     # 0.0–1.0
        sweep_type: str = "none",  # "stop_hunt" | "judas_swing" | "mmxm_sweep" | ...
        # Zone
        has_zone: bool = False,    # OB/FVG/BB/OTE
        in_kz: bool = False,
        in_sb: bool = False,
        # Structure
        has_bos: bool = False,
        has_choch: bool = False,
        has_displacement: bool = False,
        # DXY correlation (EURUSD as DXY proxy — inversely correlated with gold)
        dxy_correlated: bool = False,
        # Mode
        mode: str = "FLOW",
    ) -> ConfluenceResult:

        b: dict = {}

        # ── HTF Bias (0–5) ──────────────────────────────────────────
        d1_ok = (d1_bias == "bullish" and direction == "buy") or \
                (d1_bias == "bearish" and direction == "sell")
        h4_ok = (h4_trend == "bullish" and direction == "buy") or \
                (h4_trend == "bearish" and direction == "sell")
        h1_ok = (h1_trend == "bullish" and direction == "buy") or \
                (h1_trend == "bearish" and direction == "sell")

        b["htf_d1"] = 2 if d1_ok else 0
        b["htf_h4"] = 2 if h4_ok else 0
        b["htf_h1"] = 1 if h1_ok else 0
        htf_score   = b["htf_d1"] + b["htf_h4"] + b["htf_h1"]

        # ── Liquidity Sweep (0–5) ───────────────────────────────────
        b["sweep_base"]     = 3 if sweep_confirmed else 0
        b["sweep_strength"] = 1 if (sweep_confirmed and sweep_strength > 0.75) else 0
        b["sweep_type"]     = 1 if (sweep_confirmed and sweep_type in ("stop_hunt", "judas_swing")) else 0
        sweep_score = b["sweep_base"] + b["sweep_strength"] + b["sweep_type"]

        # ── Zone Quality (0–5) ──────────────────────────────────────
        all_htf = d1_ok and h4_ok and h1_ok
        b["zone_present"]   = 2 if has_zone else 0
        b["zone_kz"]        = 1 if in_kz   else 0
        b["zone_sb"]        = 1 if in_sb   else 0
        b["zone_htf_depth"] = 1 if all_htf else 0
        zone_score = b["zone_present"] + b["zone_kz"] + b["zone_sb"] + b["zone_htf_depth"]

        # ── Structure (0–4) ─────────────────────────────────────────
        b["struct_bos"]   = 2 if has_bos         else 0
        b["struct_choch"] = 1 if has_choch        else 0
        b["struct_disp"]  = 1 if has_displacement else 0
        structure_score = b["struct_bos"] + b["struct_choch"] + b["struct_disp"]

        # ── DXY Correlation (0–1) ────────────────────────────────────
        # Gold-USD teskari korrelyatsiya: EURUSD H4 bullish → USD zaif → GOLD buy qo'llaydi
        b["dxy_confirm"] = 1 if dxy_correlated else 0
        dxy_score = b["dxy_confirm"]

        total = htf_score + sweep_score + zone_score + structure_score + dxy_score

        # ── 4 Pillar check ──────────────────────────────────────────
        # zone_present must specifically be > 0 (KZ/SB bonus alone not enough)
        pillars_ok = (
            htf_score >= 2 and           # at least H4 aligned
            sweep_score > 0 and          # sweep confirmed
            b["zone_present"] > 0 and    # OB/FVG/BB/OTE must exist
            structure_score > 0          # BOS or CHoCH or displacement
        )

        minimum = self.MIN_SNIPER if mode == "SNIPER" else self.MIN_FLOW
        approved = pillars_ok and total >= minimum

        # Grade (A+ ≥18, A ≥15, B ≥12, C ≥10)
        if not pillars_ok or total < self.MIN_FLOW:
            grade = "FAIL"
        elif total >= 18:
            grade = "A+"
        elif total >= 15:
            grade = "A"
        elif total >= 12:
            grade = "B"
        else:
            grade = "C"

        # Reason
        missing = []
        if htf_score < 2:
            missing.append(f"HTF({htf_score}/5)")
        if sweep_score == 0:
            missing.append("SWEEP")
        if zone_score == 0:
            missing.append("ZONE")
        if structure_score == 0:
            missing.append("STRUCT")
        if not missing and total < minimum:
            missing.append(f"score {total}<{minimum}")

        dxy_tag = " DXY✓" if dxy_score else ""
        if not approved:
            reason = "REJECTED — " + ", ".join(missing)
        else:
            reason = (
                f"APPROVED [{grade}] {total}/{self.MAX_SCORE} | "
                f"HTF={htf_score}/5 Sweep={sweep_score}/5 "
                f"Zone={zone_score}/5 Struct={structure_score}/4{dxy_tag}"
            )

        return ConfluenceResult(
            approved=approved,
            total_score=total,
            max_score=self.MAX_SCORE,
            minimum_required=minimum,
            pillars_ok=pillars_ok,
            htf_score=htf_score,
            sweep_score=sweep_score,
            zone_score=zone_score,
            structure_score=structure_score,
            dxy_score=dxy_score,
            breakdown=b,
            grade=grade,
            reason=reason,
        )
