"""
F2-3.1 Variant C — ICTAnalyst with live `_find_all_ict_zones` paradigm.

Strategy:
  * Fetch D1 + H4 + H1 + M30 + M15 candles ending at ``clock.now()``
  * Run ``ICTAnalysis.analyze`` per TF + compute ATR per TF
  * HTF bias: D1 trend → want_dir, fallback to H4 trend, else None
  * HTF confluence: count of D1/H4/H1/M30 confirming want_dir (0-4 score)
  * Silver Bullet flag: in London or NY kill-zone hour
  * Call ``zone_finder.find_all_ict_zones`` → list of zone dicts
  * Convert each zone → ``Signal(is_limit=True)``
  * Dedup by (direction, label, round(entry, 1)) — same zone emitted at most
    once per analyst lifetime

The analyst returns ``list[Signal]`` — empty list means no signals this cycle.
Engine iterates and routes each to broker as a pending limit.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from loguru import logger

from apps.api.src.agents.trader.analysis.ict import ICTAnalysis
from apps.api.src.agents.trader.core.data import HistoricalDataManager
from apps.api.src.agents.trader.engine import zone_finder
from apps.api.src.agents.trader.engine.regime import RegimeGate
from apps.api.src.agents.trader.engine.signals import Signal


# ── Helpers (pure functions; testable in isolation) ──────────────────────────


def _session_from_hour(hour: int) -> str:
    """Map UTC hour to coarse session label used by Signal validation."""
    if 0 <= hour < 6:
        return "asia"
    if 6 <= hour < 13:
        return "london"
    if 13 <= hour < 22:
        return "ny"
    return "off"


def _day_name(weekday: int) -> str:
    """Map ``datetime.weekday()`` (Mon=0..Sun=6) to the 3-letter Signal code."""
    return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][weekday]


def _ensure_tz_aware(dt: datetime) -> datetime:
    """Coerce naive datetimes to UTC. Signal validates tz-awareness."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _is_silver_bullet(hour: int) -> bool:
    """Silver Bullet kill-zone hour: London 10:00-11:00 UTC, NY 14:00-15:00 UTC."""
    return hour in (10, 14)


def _htf_confluence(want_dir: str, ict_map: dict) -> int:
    """Live `_htf_confluence` port: count of D1/H4/H1/M30 confirming want_dir (0-4)."""
    count = 0
    want_trend = "bullish" if want_dir == "buy" else "bearish"
    for tf in ("D1", "H4", "H1", "M30"):
        ict = ict_map.get(tf)
        if ict is None:
            continue
        structure = getattr(ict, "structure", {}) or {}
        trend = structure.get("trend", "sideways")
        bos = structure.get("bos", 0)
        choch = structure.get("choch", False)
        if trend == want_trend:
            count += 1
        elif bos > 0 and choch:
            count += 1
    return count


def _bias_from_trends(d1_trend: str, h4_trend: str) -> str | None:
    """D1 primary, H4 fallback. Returns 'buy' / 'sell' / None."""
    for trend in (d1_trend, h4_trend):
        if trend == "bullish":
            return "buy"
        if trend == "bearish":
            return "sell"
    return None


# ── Analyst ──────────────────────────────────────────────────────────────────


class ICTAnalyst:
    """Variant C ICT analyst — multi-TF zone discovery via live paradigm port.

    :param clock: Clock with ``.now() -> datetime`` (VirtualClock in backtest).
    :param data: ``HistoricalDataManager`` (already preloaded by the engine).
    :param params: Reserved for future ``AnalystParams`` injection; unused.
    :param min_candles_per_tf: Skip TF if fewer than this many candles returned.
    :param confluence_max_weight: Upper bound used to normalize zone weight
        into ``Signal.confluence_score`` ([0, 1]). Live trader's strongest
        weights typically land in the 30-80 range — 100.0 gives headroom.
    :param max_signals_per_cycle: Cap on how many Signals we emit per call.
        Live trader places up to 20; backtest can be tighter to reduce broker
        margin churn.
    """

    # Per-TF candle counts (mirror live agent.py `_tick`)
    TF_CANDLE_COUNTS: dict[str, int] = {
        "D1": 150,
        "H4": 300,
        "H1": 500,
        "M30": 300,
        "M15": 500,
    }

    # Setup-type structural losers — blocked by name in every regime. Reserved
    # for setups whose edge is broken by construction; directional / regime-
    # dependent under-performance is NOT blocked here (kept CLI-only via
    # --block-setup or handled by RegimeGate, to avoid overfitting to one
    # period). The live trader also down-weights losers via SelfLearner; the
    # backtest lacks that loop, so we block them by name.
    #
    # F2-3.1 Variant C smoke (2024-01, 1 month):
    # - M15_OTE: 69 trades, PF 0.39 — dynamic fib on M15 = noise
    # - M15_DR_Eq: 22 trades, PF 0.04 — DR equilibrium needs HTF context
    #
    # Per-trade diagnosis over 24 months / 8 quarters (2026-06, --dump-trades):
    # - H1_OB: net -$106.55, WR < 50% in 6 of 8 quarters, no quarter profitable
    #   beyond noise (+$9/+$21/+$23 best). The loss is CHRONIC, not volatility-
    #   driven (the v4 RegimeGate does not catch it), so it belongs here rather
    #   than the gate. Blocking it lifts the v4 24-month aggregate +$351 -> ~+$457
    #   and turns no quarter negative (it only trims 3 marginally-positive ones).
    DEFAULT_BLOCKED_SETUPS: frozenset[str] = frozenset({
        "M15_OTE",
        "M15_DR_Eq",
        "H1_OB",
    })

    def __init__(
        self,
        clock: Any,
        data: HistoricalDataManager,
        params: Any = None,
        *,
        min_candles_per_tf: int = 50,
        confluence_max_weight: float = 100.0,
        max_signals_per_cycle: int = 8,
        blocked_setups: set[str] | frozenset[str] | None = None,
        regime: RegimeGate | None = None,
        min_rr: float = 0.0,
    ) -> None:
        self.clock = clock
        self.data = data
        self.params = params
        self.min_candles_per_tf = min_candles_per_tf
        self.confluence_max_weight = confluence_max_weight
        self.max_signals_per_cycle = max_signals_per_cycle
        # Structural reward:risk floor. A zone whose |tp1-entry|/|entry-sl| falls
        # below this is dropped before routing. 0.0 (default) disables the floor,
        # reproducing prior behaviour exactly. Motivation: the book's edge in
        # benign regimes is high-WR / low-RR mean-reversion zones (e.g. planned
        # RR ~0.6) that need ~60% WR to break even; when WR mean-reverts ~15pts
        # in adverse regimes they bleed. A floor culls exactly those fragile
        # zones — structurally, not by regime-timing.
        self.min_rr = float(min_rr)
        self.blocked_setups = (
            frozenset(blocked_setups) if blocked_setups is not None
            else self.DEFAULT_BLOCKED_SETUPS
        )
        # v4: high-volatility regime gate. None / disabled gate = no-op, so the
        # analyst reproduces v3 unless a gate is explicitly passed.
        self.regime = regime
        self.ict = ICTAnalysis()
        self._emitted_zone_keys: set[tuple] = set()
        # Perf: memoize per-TF ICT analysis keyed by the last-closed bar
        # timestamp. HTF windows (H4/H1/D1) only advance when a new bar on that
        # TF closes, so most M15 steps would otherwise re-run identical analysis
        # (measured: H4 ~95% redundant, H1 ~82%). tf -> (last_ts, ICTSignal, atr).
        self._analysis_cache: dict[str, tuple[Any, Any, float]] = {}

    # ── Public API expected by BacktestEngine._trading_cycle ─────────────────

    def analyze_market(
        self,
        symbol: str,
        timeframes: Iterable[str],
    ) -> list[Signal]:
        """Return zero or more limit-order ``Signal``s for this cycle.

        Always returns a list. Empty list = no actionable zones this bar.

        TF availability is dynamic: D1/M30 may be missing in some datasets
        (live trader fetches from MT5; backtest may only have M15/H1/H4 parquets).
        We skip unavailable TFs and degrade gracefully — bias falls back from
        D1 → H4, confluence/zones use whatever TFs we have.
        """
        now = _ensure_tz_aware(self.clock.now())

        # 1. Fetch candles per TF — skip missing TFs gracefully
        candles_per_tf: dict[str, Any] = {}
        for tf, count in self.TF_CANDLE_COUNTS.items():
            try:
                df = self.data.get_candles_at(symbol, tf, now, count=count)
            except KeyError:
                # TF not loaded — degrade gracefully
                continue
            except Exception as exc:
                logger.debug("ICTAnalyst: {} data error — {}", tf, exc)
                continue
            if df is None or len(df) < self.min_candles_per_tf:
                continue
            candles_per_tf[tf] = df

        # Need at least one HTF (H4 or D1) and the primary LTF (M15) to operate
        if "M15" not in candles_per_tf:
            return []
        if "D1" not in candles_per_tf and "H4" not in candles_per_tf:
            return []

        # 2. Run ICT.analyze per TF + ATR — memoized by last-closed bar timestamp.
        #    When a TF's window has not advanced since the previous cycle, the
        #    candles are identical and analyze/_atr would return the same result,
        #    so we reuse the cached value (byte-identical, just skips the work).
        ict_map: dict[str, Any] = {}
        atr_map: dict[str, float] = {}
        for tf, df in candles_per_tf.items():
            last_ts = df.index[-1]
            cached = self._analysis_cache.get(tf)
            if cached is not None and cached[0] == last_ts:
                ict_map[tf], atr_map[tf] = cached[1], cached[2]
            else:
                sig = self.ict.analyze(df, tf)
                atr = self.ict._atr(df)
                self._analysis_cache[tf] = (last_ts, sig, atr)
                ict_map[tf], atr_map[tf] = sig, atr

        # 3. HTF bias (D1 primary, H4 fallback — whichever is loaded)
        d1_trend = ict_map.get("D1").structure.get("trend", "sideways") if "D1" in ict_map else "sideways"
        h4_trend = ict_map.get("H4").structure.get("trend", "sideways") if "H4" in ict_map else "sideways"
        want_dir = _bias_from_trends(d1_trend, h4_trend)
        if want_dir is None:
            return []

        # 4. HTF confluence (0-4 across whatever HTF TFs are loaded)
        htf_conf = _htf_confluence(want_dir, ict_map)

        # 5. Current price (last M15 close)
        current_price = float(candles_per_tf["M15"].iloc[-1]["close"])

        # 5b. Regime gate — realised M15 volatility for this cycle. Computed once
        #     (0.0 when the gate is absent/disabled) and reused per zone below.
        regime_atr_pct = (
            self.regime.atr_pct(candles_per_tf["M15"])
            if self.regime is not None and self.regime.enabled
            else 0.0
        )

        # 6. Silver Bullet flag
        silver_bullet = _is_silver_bullet(now.hour)

        # 7. Build tf_ict_data tuples — only loaded TFs
        tf_ict_data = [
            (tf, ict_map[tf], atr_map[tf], candles_per_tf[tf])
            for tf in self.TF_CANDLE_COUNTS if tf in ict_map
        ]

        # 8. Find all zones
        zones = zone_finder.find_all_ict_zones(
            want_dir=want_dir,
            price=current_price,
            tf_ict_data=tf_ict_data,
            want_trend=want_dir,
            htf_conf=htf_conf,
            silver_bullet=silver_bullet,
        )
        if not zones:
            return []

        # 9. Convert zones → Signals (dedup + max cap)
        signals: list[Signal] = []
        session = _session_from_hour(now.hour)
        day = _day_name(now.weekday())

        for zone in zones:
            label = zone.get("label", "UNKNOWN")
            if label in self.blocked_setups:
                continue
            # v4 regime gate: skip core reversion setups in high-vol regime.
            if self.regime is not None and self.regime.should_block(label, regime_atr_pct):
                continue
            entry = float(zone["entry"])
            direction = zone["direction"]
            tp = float(zone.get("tp1", zone.get("tp2", entry)))
            sl = float(zone["sl"])
            # Structural reward:risk floor (== Signal.rr()). Drop low-RR zones
            # before dedup so a culled zone never reserves its dedup key.
            if self.min_rr > 0.0:
                risk = abs(entry - sl)
                if risk <= 0.0 or (abs(tp - entry) / risk) < self.min_rr:
                    continue
            zone_key = (direction, label, round(entry, 1))
            if zone_key in self._emitted_zone_keys:
                continue

            weight = float(zone.get("weight", 0.0))
            confluence_score = max(0.0, min(1.0, weight / self.confluence_max_weight))

            self._emitted_zone_keys.add(zone_key)

            signals.append(Signal(
                symbol=symbol,
                direction=direction,
                entry_price=entry,
                sl=sl,
                tp=tp,
                confluence_score=confluence_score,
                setup_type=label,
                session=session,
                day_of_week=day,
                mode="sniper",
                timestamp=now,
                is_limit=True,
                metadata={
                    "tf": zone.get("tf"),
                    "zone_lo": zone.get("zone_lo"),
                    "zone_hi": zone.get("zone_hi"),
                    "tp1": zone.get("tp1"),
                    "tp2": zone.get("tp2"),
                    "tp3": zone.get("tp3"),
                    "weight": weight,
                    "htf_conf": zone.get("htf_conf", htf_conf),
                    "silver_bullet": zone.get("silver_bullet", silver_bullet),
                    "d1_trend": d1_trend,
                    "h4_trend": h4_trend,
                },
            ))

            if len(signals) >= self.max_signals_per_cycle:
                break

        return signals
