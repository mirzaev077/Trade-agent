"""
EntryAgent — Selects best entry zone from OB / FVG / OTE / Breaker Block.

Entry must be 30–50% retracement into the zone (configurable via SelfLearner).
Uses LIMIT orders only — never chases price.
"""
from dataclasses import dataclass, field


@dataclass
class EntryResult:
    zones: list             # refined zone dicts, sorted by quality
    best_zone: dict = None  # top-ranked zone
    entry_type: str = "OB"  # dominant zone type (OB / FVG / OTE / BB)
    entry_pct: float = 0.30 # retracement used (from SelfLearner)
    count: int = 0
    htf_zones: list = field(default_factory=list)   # D1/H4 zones
    ltf_zones: list = field(default_factory=list)   # H1/M30/M15/M5 zones


class EntryAgent:
    """
    Step 4 of pipeline: refines and ranks entry zones.

    Takes raw zones from _find_all_ict_zones and applies:
    - SelfLearner entry precision (entry_pct: 30–50% of zone range)
    - Zone quality filtering (SelfLearner disabled setups removed upstream)
    - HTF/LTF zone separation for display/logging
    - Best zone selection
    """

    PRIORITY_ORDER = ("OB", "BB", "OTE", "FVG", "IFVG", "BPR", "BISI", "SIBI")
    HTF_SET = {"D1", "H4"}
    LTF_SET = {"H1", "M30", "M15", "M5", "M1"}

    def find_zones(
        self,
        raw_zones: list,
        entry_pct: float = 0.30,
    ) -> EntryResult:
        if not raw_zones:
            return EntryResult(zones=[], best_zone=None, count=0)

        refined = []
        htf_zones = []
        ltf_zones = []

        for z in raw_zones:
            z2 = dict(z)
            zone_lo = z2.get("zone_lo", z2.get("entry", 0))
            zone_hi = z2.get("zone_hi", z2.get("entry", 0))
            rng = zone_hi - zone_lo

            # Apply entry precision retracement
            if rng > 0.01:
                direction = z2.get("direction", "buy")
                if direction == "buy":
                    z2["entry"] = round(zone_lo + rng * entry_pct, 2)
                else:
                    z2["entry"] = round(zone_hi - rng * entry_pct, 2)

            refined.append(z2)

            tf = z2.get("tf", "H1")
            if tf in self.HTF_SET:
                htf_zones.append(z2)
            else:
                ltf_zones.append(z2)

        # Sort by weight (SelfLearner-adjusted) desc
        refined.sort(key=lambda z: -z.get("weight", 0))
        htf_zones.sort(key=lambda z: -z.get("weight", 0))
        ltf_zones.sort(key=lambda z: -z.get("weight", 0))

        best_zone = refined[0] if refined else None

        # Determine dominant entry type from top-5 zones
        type_counts: dict = {}
        for z in refined[:5]:
            label = z.get("label", "")
            parts = label.split("_")
            for p in parts[1:]:
                pu = p.upper()
                if pu in self.PRIORITY_ORDER:
                    type_counts[pu] = type_counts.get(pu, 0) + 1
                    break

        dominant_type = "OB"
        if type_counts:
            dominant_type = max(type_counts, key=lambda k: (type_counts[k], -self.PRIORITY_ORDER.index(k) if k in self.PRIORITY_ORDER else 99))

        return EntryResult(
            zones=refined,
            best_zone=best_zone,
            entry_type=dominant_type,
            entry_pct=entry_pct,
            count=len(refined),
            htf_zones=htf_zones,
            ltf_zones=ltf_zones,
        )
