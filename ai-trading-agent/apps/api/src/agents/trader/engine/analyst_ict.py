"""
F2-2.E: MVP ICT analyst for backtest engine.

Bridges ``ICTAnalysis`` (production analyzer in ``analysis/ict.py``) to the
BacktestEngine's ``analyze_market(symbol, timeframes) -> Signal | None``
protocol.

Strategy (MVP — intentionally simpler than production trader):

  * Fetch H4 + H1 + M15 candles ending at ``clock.now()``
  * Run ``ICTAnalysis.analyze`` on H4 and H1
  * HTF bias: if ``H4.structure["trend"]`` is ``"bullish"``/``"bearish"``,
    pick that as ``want_direction``; otherwise skip.
  * Find an unmitigated H1 order block in the wanted direction.
  * If current price is inside the OB zone AND the H1 ``pd_zone`` aligns
    (buy → discount/equilibrium, sell → premium/equilibrium), emit a
    ``Signal``:

      - ``entry``: OB midpoint
      - ``sl``:    OB other side + ``sl_buffer_pips`` buffer
      - ``tp``:    entry ± SL-distance × ``rr_ratio`` (default 1:2 RR)

  * Otherwise: return ``None``.

Notes:
  * No Claude calls, no learner dependency, no multi-TF zone weighting.
    The production ``TraderAgent._find_all_ict_zones`` is ~200 LOC across
    9 helpers — porting it is F2-3 scope.
  * ICT order-block types are ``"bullish_ob"`` / ``"bearish_ob"`` (not
    ``"buy"``/``"sell"``) — we map them explicitly here.
  * VirtualClock returns whatever was passed to its constructor. The
    BacktestConfig stores tz-aware UTC datetimes, so ``clock.now()`` is
    tz-aware. If a caller wires a naive clock, we coerce to UTC before
    constructing the Signal (Signal validates tz-awareness).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from loguru import logger

from apps.api.src.agents.trader.analysis.ict import ICTAnalysis
from apps.api.src.agents.trader.core.data import HistoricalDataManager
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


def _ob_direction(ob_type: str) -> str | None:
    """Map ICT OB ``type`` string (``bullish_ob``/``bearish_ob``) to ``buy``/``sell``."""
    if "bullish" in ob_type:
        return "buy"
    if "bearish" in ob_type:
        return "sell"
    return None


def _ensure_tz_aware(dt: datetime) -> datetime:
    """Coerce naive datetimes to UTC. Signal validates tz-awareness."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ── Analyst ──────────────────────────────────────────────────────────────────


class ICTAnalyst:
    """MVP ICT-based analyst — bridges ``ICTAnalysis`` to the backtest engine.

    :param clock: A clock object exposing ``.now() -> datetime`` (VirtualClock
                  in backtest, RealClock in live). Used to anchor candle reads
                  and stamp the emitted Signal.
    :param data: ``HistoricalDataManager`` (already preloaded by the engine).
    :param params: Reserved for future ``AnalystParams`` injection — accepted
                   for engine-construction compat; unused in MVP.
    :param rr_ratio: Reward:Risk multiple for TP. 2.0 = fixed 1:2.
    :param sl_buffer_pips: Extra pips added beyond OB opposite side for SL.
    :param pip_size: Quote unit per pip. 0.10 = XAUUSD; 0.0001 = EURUSD; etc.
    :param min_candles_per_tf: Minimum candles each TF must return; below this
                               we return None (insufficient data).
    """

    def __init__(
        self,
        clock: Any,
        data: HistoricalDataManager,
        params: Any = None,
        *,
        rr_ratio: float = 2.0,
        sl_buffer_pips: float = 5.0,
        pip_size: float = 0.10,
        min_candles_per_tf: int = 50,
    ) -> None:
        self.clock = clock
        self.data = data
        self.params = params
        self.rr_ratio = rr_ratio
        self.sl_buffer_pips = sl_buffer_pips
        self.pip_size = pip_size
        self.min_candles_per_tf = min_candles_per_tf
        self.ict = ICTAnalysis()

    # ── Public API expected by BacktestEngine._trading_cycle ─────────────────

    def analyze_market(
        self,
        symbol: str,
        timeframes: Iterable[str],
    ) -> Signal | None:
        """Return ONE ``Signal`` if all MVP conditions are met, else ``None``.

        Reads candles via ``self.data``; does not depend on any TraderAgent
        state. The ``timeframes`` argument is accepted for protocol parity
        with the engine but ignored — this MVP always reads H4/H1/M15.
        """
        now = _ensure_tz_aware(self.clock.now())

        # 1. Fetch candles for H4, H1, M15
        try:
            h4 = self.data.get_candles_at(symbol, "H4", now, count=200)
            h1 = self.data.get_candles_at(symbol, "H1", now, count=300)
            m15 = self.data.get_candles_at(symbol, "M15", now, count=500)
        except KeyError as exc:
            logger.debug("ICTAnalyst: data not loaded — {}", exc)
            return None

        for tf_name, df in (("H4", h4), ("H1", h1), ("M15", m15)):
            if df is None or len(df) < self.min_candles_per_tf:
                logger.debug(
                    "ICTAnalyst: insufficient {} candles ({}) — skip",
                    tf_name,
                    0 if df is None else len(df),
                )
                return None

        # 2. Run ICT on H4 + H1 (M15 only used for current price)
        ict_h4 = self.ict.analyze(h4, "H4")
        ict_h1 = self.ict.analyze(h1, "H1")

        # 3. HTF bias from H4 trend
        h4_trend = ict_h4.structure.get("trend", "sideways")
        if h4_trend == "bullish":
            want_dir = "buy"
        elif h4_trend == "bearish":
            want_dir = "sell"
        else:
            return None  # no clear HTF bias

        # 4. Find unmitigated H1 OB in want direction
        obs = ict_h1.order_blocks or []
        matching = [
            ob
            for ob in obs
            if not ob.get("mitigated", False)
            and _ob_direction(ob.get("type", "")) == want_dir
        ]
        if not matching:
            return None

        # Pick strongest unmitigated OB
        ob = max(matching, key=lambda o: o.get("strength", 0.0))

        # 5. Current price must be INSIDE the OB zone
        current_close = float(m15.iloc[-1]["close"])
        ob_top = float(ob.get("high", 0.0))
        ob_bot = float(ob.get("low", 0.0))
        if ob_top <= ob_bot:
            return None  # malformed OB
        if not (ob_bot <= current_close <= ob_top):
            return None  # price not in the zone yet

        # 6. PD-zone alignment (H1)
        pd_zone = ict_h1.pd_zone
        if want_dir == "buy" and pd_zone not in ("discount", "equilibrium"):
            return None
        if want_dir == "sell" and pd_zone not in ("premium", "equilibrium"):
            return None

        # 7. Compute entry / SL / TP
        entry = (ob_top + ob_bot) / 2.0
        sl_buffer = self.sl_buffer_pips * self.pip_size
        if want_dir == "buy":
            sl = ob_bot - sl_buffer
            tp = entry + abs(entry - sl) * self.rr_ratio
        else:
            sl = ob_top + sl_buffer
            tp = entry - abs(entry - sl) * self.rr_ratio

        # 8. Confluence score: OB strength normalised to [0, 1].
        # ICTAnalysis caps strength at 3.0 (see _order_blocks); divide for
        # range stability. Fall back to 0.5 if absent.
        raw_strength = float(ob.get("strength", 0.5))
        confluence_score = max(0.0, min(1.0, raw_strength / 3.0))

        return Signal(
            symbol=symbol,
            direction=want_dir,
            entry_price=entry,
            sl=sl,
            tp=tp,
            confluence_score=confluence_score,
            setup_type="H1_OB",
            session=_session_from_hour(now.hour),
            day_of_week=_day_name(now.weekday()),
            mode="sniper",
            timestamp=now,
            metadata={
                "h4_trend": h4_trend,
                "h1_pd_zone": pd_zone,
                "ob_strength": raw_strength,
                "ob_high": ob_top,
                "ob_low": ob_bot,
            },
        )
