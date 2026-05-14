"""
LiquidityHunterAgent — Finds all EQH/EQL, PDH/PDL, BSL/SSL pools.

Predicts where price will hunt liquidity next.
Output feeds into ManipulationAgent to confirm actual sweep.
"""
from dataclasses import dataclass, field


@dataclass
class LiquidityTarget:
    price: float
    level_type: str      # "BSL" | "SSL" | "EQH" | "EQL" | "PDH" | "PDL"
    timeframe: str
    sweep_dir: str       # "up" (BSL above) | "down" (SSL below)
    distance_pips: float
    swept: bool
    weight: float        # higher = more significant


@dataclass
class LiquidityHuntResult:
    targets: list                  # List[LiquidityTarget], sorted by weight
    swept_targets: list            # targets already swept
    pending_targets: list          # targets NOT yet swept (upcoming)
    any_swept: bool                # has any sweep happened recently?
    likely_next_sweep: str         # "up" | "down" | "none"
    nearest_pending: LiquidityTarget = None
    summary: str = ""


class LiquidityHunterAgent:
    """
    Step 2 of pipeline: maps out all liquidity pools.

    Priority:
    - H4/H1 swept pools → confirmed manipulation zones
    - EQH/EQL → equal highs/lows (resting liquidity)
    - PDH/PDL → previous day levels (algorithmic targets)
    - BSL/SSL → buy-side and sell-side liquidity
    """

    PIP = 0.10
    TF_WEIGHT = {
        "D1": 3.5, "H4": 3.0, "H1": 2.5,
        "M30": 2.0, "M15": 1.5, "M5": 1.0, "M1": 0.7,
    }

    def hunt(self, ict_map: dict, current_price: float) -> LiquidityHuntResult:
        targets: list[LiquidityTarget] = []

        for tf, ict in ict_map.items():
            if ict is None:
                continue
            tf_wt = self.TF_WEIGHT.get(tf, 1.0)

            # Liquidity pools from ICT liquidity_zones
            for lz in ict.liquidity_zones:
                lz_type = lz.get("type", "")
                raw_price = float(
                    lz.get("price", lz.get("high", lz.get("low", 0)))
                )
                if raw_price <= 0:
                    continue

                is_bsl = lz_type in ("BSL", "ERL_high", "EQH", "IRL_high")
                is_ssl = lz_type in ("SSL", "ERL_low",  "EQL", "IRL_low")
                if not (is_bsl or is_ssl):
                    continue

                swept     = lz.get("swept", False)
                sweep_dir = "up" if is_bsl else "down"
                dist      = abs(current_price - raw_price) / self.PIP
                wt        = tf_wt * (2.2 if swept else 1.0)

                targets.append(LiquidityTarget(
                    price=raw_price, level_type=lz_type, timeframe=tf,
                    sweep_dir=sweep_dir, distance_pips=round(dist, 1),
                    swept=swept, weight=round(wt, 2),
                ))

            # PDH / PDL from structure dict
            pdh = float(ict.structure.get("pdh", 0))
            pdl = float(ict.structure.get("pdl", 0))
            if pdh > 0:
                dist = abs(current_price - pdh) / self.PIP
                targets.append(LiquidityTarget(
                    price=pdh, level_type="PDH", timeframe=tf, sweep_dir="up",
                    distance_pips=round(dist, 1), swept=False, weight=tf_wt * 1.8,
                ))
            if pdl > 0:
                dist = abs(current_price - pdl) / self.PIP
                targets.append(LiquidityTarget(
                    price=pdl, level_type="PDL", timeframe=tf, sweep_dir="down",
                    distance_pips=round(dist, 1), swept=False, weight=tf_wt * 1.8,
                ))

            # Asian Range high/low
            asian_hi = float(ict.structure.get("asian_high", 0))
            asian_lo = float(ict.structure.get("asian_low",  0))
            if asian_hi > 0 and tf in ("H1", "M30", "M15"):
                dist = abs(current_price - asian_hi) / self.PIP
                targets.append(LiquidityTarget(
                    price=asian_hi, level_type="AR_HI", timeframe=tf, sweep_dir="up",
                    distance_pips=round(dist, 1), swept=False, weight=tf_wt * 1.4,
                ))
            if asian_lo > 0 and tf in ("H1", "M30", "M15"):
                dist = abs(current_price - asian_lo) / self.PIP
                targets.append(LiquidityTarget(
                    price=asian_lo, level_type="AR_LO", timeframe=tf, sweep_dir="down",
                    distance_pips=round(dist, 1), swept=False, weight=tf_wt * 1.4,
                ))

        # Sort all targets by weight
        targets.sort(key=lambda t: -t.weight)

        swept   = [t for t in targets if t.swept]
        pending = [t for t in targets if not t.swept]

        any_swept = len(swept) > 0

        # Predict next likely sweep: vote by weight
        if any_swept:
            up_w   = sum(t.weight for t in swept if t.sweep_dir == "up")
            down_w = sum(t.weight for t in swept if t.sweep_dir == "down")
            likely_next = "up" if up_w >= down_w else "down"
        elif pending:
            # No sweeps yet → nearest pending by distance
            nearest = min(pending, key=lambda t: t.distance_pips)
            likely_next = nearest.sweep_dir
        else:
            likely_next = "none"

        nearest_pending = pending[0] if pending else None

        counts = f"{len(swept)} swept / {len(pending)} pending"
        summary = f"Liquidity: {counts} | next_sweep={likely_next}"

        return LiquidityHuntResult(
            targets=targets,
            swept_targets=swept,
            pending_targets=pending,
            any_swept=any_swept,
            likely_next_sweep=likely_next,
            nearest_pending=nearest_pending,
            summary=summary,
        )
