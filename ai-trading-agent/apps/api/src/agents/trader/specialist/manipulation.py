"""
ManipulationAgent — Confirms liquidity grab before allowing entries.

RULE: NO SWEEP = NO TRADE

Detects:
- Stop hunt (SSL/BSL swept with reversal)
- Judas swing (fake move before real direction)
- CHoCH after MMXM sweep
- Displacement after sweep (M1/M5 impulse)
"""
from dataclasses import dataclass


@dataclass
class ManipulationResult:
    confirmed: bool
    sweep_type: str       # "stop_hunt" | "judas_swing" | "choch_sweep" | "mmxm_sweep" | "none"
    direction_after: str  # "buy" | "sell" | "unknown" (trade direction AFTER sweep)
    strength: float       # 0.0–1.0 (how strong/clear the manipulation is)
    tf_found: str         # timeframe where manipulation was detected
    detail: str           # human-readable description


class ManipulationAgent:
    """
    Step 3 of pipeline: confirms stop hunt / fake move before any entry.

    Search order: M5 → M15 → M30 → H1 → H4 (LTF first — most recent action)

    Strength tiers:
    - stop_hunt: 0.90 (clear spike + reversal)
    - judas_swing: 0.80 (fake move in session open)
    - choch_sweep: 0.75 (structure flip after pool grab)
    - mmxm_sweep: 0.65 (MMXM phase completed)
    """

    SEARCH_TFS = ["M5", "M15", "M30", "H1", "H4"]

    def confirm(self, ict_map: dict) -> ManipulationResult:
        for tf in self.SEARCH_TFS:
            ict = ict_map.get(tf)
            if ict is None:
                continue
            s = ict.structure

            # ── Stop Hunt ─────────────────────────────────────────
            if s.get("stop_hunt"):
                sh_dir = s.get("stop_hunt_dir", "none")
                # bearish stop hunt (SSL swept) → price now reverses UP
                # bullish stop hunt (BSL swept) → price now reverses DOWN
                dir_after = (
                    "buy"  if sh_dir == "bearish" else
                    "sell" if sh_dir == "bullish" else
                    "unknown"
                )
                return ManipulationResult(
                    confirmed=True, sweep_type="stop_hunt",
                    direction_after=dir_after, strength=0.90, tf_found=tf,
                    detail=f"{tf} stop_hunt({sh_dir}) → trade {dir_after}",
                )

            # ── Judas Swing ───────────────────────────────────────
            if s.get("judas"):
                judas_dir = s.get("judas_dir", "none")
                # Judas moves bullish (fake up) → real direction is DOWN
                dir_after = (
                    "sell" if judas_dir == "bullish" else
                    "buy"  if judas_dir == "bearish" else
                    "unknown"
                )
                return ManipulationResult(
                    confirmed=True, sweep_type="judas_swing",
                    direction_after=dir_after, strength=0.80, tf_found=tf,
                    detail=f"{tf} judas({judas_dir}) → trade {dir_after}",
                )

            # ── CHoCH + MMXM sweep ────────────────────────────────
            if s.get("choch") and s.get("mmxm_swept"):
                mmxm_dir  = s.get("mmxm_dir", "none")
                dir_after = (
                    "buy"  if mmxm_dir == "bearish" else
                    "sell" if mmxm_dir == "bullish" else
                    "unknown"
                )
                strength = 0.85 if tf in ("H4", "H1") else 0.72
                return ManipulationResult(
                    confirmed=True, sweep_type="choch_sweep",
                    direction_after=dir_after, strength=strength, tf_found=tf,
                    detail=f"{tf} choch+mmxm_sweep({mmxm_dir}) → {dir_after}",
                )

        # ── MMXM sweep without CHoCH ──────────────────────────────
        for tf in ("H4", "H1", "M30"):
            ict = ict_map.get(tf)
            if ict is None:
                continue
            s = ict.structure
            if s.get("mmxm_swept"):
                mmxm_dir  = s.get("mmxm_dir", "none")
                dir_after = (
                    "buy"  if mmxm_dir == "bearish" else
                    "sell" if mmxm_dir == "bullish" else
                    "unknown"
                )
                return ManipulationResult(
                    confirmed=True, sweep_type="mmxm_sweep",
                    direction_after=dir_after, strength=0.65, tf_found=tf,
                    detail=f"{tf} mmxm_swept({mmxm_dir}) → {dir_after}",
                )

        # ── Swept liquidity pool (any TF) ─────────────────────────
        for tf in self.SEARCH_TFS:
            ict = ict_map.get(tf)
            if ict is None:
                continue
            for lz in ict.liquidity_zones:
                if lz.get("swept"):
                    lz_type = lz.get("type", "")
                    dir_after = (
                        "sell" if lz_type in ("BSL", "EQH", "ERL_high") else
                        "buy"  if lz_type in ("SSL", "EQL", "ERL_low")  else
                        "unknown"
                    )
                    if dir_after != "unknown":
                        return ManipulationResult(
                            confirmed=True, sweep_type="liq_sweep",
                            direction_after=dir_after, strength=0.60, tf_found=tf,
                            detail=f"{tf} {lz_type} swept → {dir_after}",
                        )

        return ManipulationResult(
            confirmed=False, sweep_type="none",
            direction_after="unknown", strength=0.0, tf_found="none",
            detail="No manipulation detected — waiting for sweep",
        )
