"""High-volatility regime gate (v4).

Diagnosis (v3 24-month acceptance run, 2026-06-04)
--------------------------------------------------
The two REJECT quarters (Q2 2025, Q4 2025) ran at ~1.7x the volatility of the
six WARN quarters — mean M15 ATR%% 0.20 vs 0.12 (``reports/acceptance_24m/
atr_diagnosis.py``). In that high-vol regime the core mean-reversion setups
(``M15_OB``, ``M15_BB``, ``M15_CISD``) collapsed from ~47%% to ~30%% win rate,
while they stayed profitable in normal volatility. They are the best setups in
the normal regime, so they cannot be blocked outright (that was tried in v3 and
rejected as overfit). Instead we *gate* them: skip these setups ONLY when
realised volatility is high.

Design
------
``RegimeGate`` is a pure, self-contained filter — no engine/analyst coupling, so
it is trivially unit-testable and reusable by the live trader later. It computes
a rolling ATR%% on the M15 candle window (SMA of true range / last close * 100,
matching :meth:`ICTAnalysis._atr`) and blocks the configured setups when ATR%%
exceeds ``atr_threshold``.

The gate is **disabled by default** (``atr_threshold <= 0``) so existing
backtests reproduce v3 exactly. Enable it from the CLI with
``--regime-atr-threshold`` (calibrated band 0.14–0.20; 0.18 blocks ~50%% of
high-vol bars and <15%% of normal-vol bars).

Threshold calibration (M15 ATR%%, SMA-14, fraction of bars above T)::

    quarter   verdict   T=0.16  T=0.18  T=0.20
    q1_2024   WARN        0.12    0.09    0.06
    q2_2024   WARN        0.30    0.23    0.17   <- volatile WARN quarter
    q1_2025   WARN        0.17    0.11    0.06
    q2_2025   REJECT      0.63    0.50    0.39
    q4_2025   REJECT      0.65    0.55    0.45
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

# Core mean-reversion / order-block setups that decay in high volatility.
# Labels are TF-prefixed ("{TF}_{KIND}") — see zone_finder. v3 per-setup
# breakdown: M15_OB/BB/CISD win rate fell 47%->30% across 2025 H2.
DEFAULT_REGIME_BLOCKED_SETUPS: frozenset[str] = frozenset({
    "M15_OB",
    "M15_BB",
    "M15_CISD",
})

HIGH_VOL = "HIGH_VOL"
NORMAL = "NORMAL"


@dataclass(frozen=True)
class RegimeGate:
    """Volatility regime gate — block ``blocked_setups`` when M15 ATR%% > threshold.

    :param atr_threshold: ATR%% (ATR / price * 100) above which the regime is
        "high volatility". ``<= 0`` disables the gate entirely (no-op), which is
        the default so the gate never changes behaviour unless turned on.
    :param atr_period: Lookback for the ATR SMA. Defaults to 14, matching
        :meth:`ICTAnalysis._atr` so the gate and the analyst agree on ATR.
    :param blocked_setups: Setup labels (``"{TF}_{KIND}"``) to skip while the
        regime is high-vol. Defaults to the core M15 reversion setups. Any
        iterable is accepted and normalised to a frozenset.
    """

    atr_threshold: float = 0.0
    atr_period: int = 14
    blocked_setups: frozenset[str] = DEFAULT_REGIME_BLOCKED_SETUPS

    def __post_init__(self) -> None:
        if self.atr_period < 1:
            raise ValueError(f"atr_period must be >= 1, got {self.atr_period}")
        # Accept any iterable; store as frozenset (gate is frozen → object.__setattr__).
        object.__setattr__(self, "blocked_setups", frozenset(self.blocked_setups))

    @property
    def enabled(self) -> bool:
        """True only when the gate can actually block something."""
        return self.atr_threshold > 0.0 and bool(self.blocked_setups)

    def atr_pct(self, candles: pd.DataFrame) -> float:
        """Rolling ATR%% at the last bar: ``SMA(TR, period) / last_close * 100``.

        Mirrors :meth:`ICTAnalysis._atr` (SMA true range, ``min_periods=1``)
        then normalises by price so the threshold is asset-independent (gold ran
        ~2065 → ~4340 over the backtest window; a raw-ATR threshold would not
        transfer). Fails *open* — returns ``0.0`` (→ NORMAL → no block) on empty
        / degenerate input so a data glitch never silently blocks every setup.

        The first bar's shifted-close terms are NaN; ``concat(...).max(axis=1)``
        relies on pandas' default ``skipna=True`` so true range falls back to
        ``high-low`` there (do not switch to ``skipna=False``).
        """
        if candles is None or len(candles) == 0:
            return 0.0
        h = candles["high"].astype(float)
        l = candles["low"].astype(float)
        c = candles["close"].astype(float)
        tr = pd.concat(
            [h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1
        ).max(axis=1)
        atr = tr.rolling(self.atr_period, min_periods=1).mean().iloc[-1]
        last_close = float(c.iloc[-1])
        if last_close <= 0 or pd.isna(atr):
            return 0.0
        return float(atr) / last_close * 100.0

    def regime(self, atr_pct_value: float) -> str:
        """Classify a precomputed ATR%% value into ``HIGH_VOL`` / ``NORMAL``."""
        if self.enabled and atr_pct_value > self.atr_threshold:
            return HIGH_VOL
        return NORMAL

    def should_block(self, setup_label: str, atr_pct_value: float) -> bool:
        """True if ``setup_label`` must be skipped given the current ATR%%.

        Blocks only when the gate is enabled, the regime is high-vol
        (``atr_pct_value > atr_threshold``), AND the label is in
        ``blocked_setups``. The caller computes ATR%% once per cycle (via
        :meth:`atr_pct`) and passes it in, so this is a cheap per-zone check.
        """
        if not self.enabled:
            return False
        if atr_pct_value <= self.atr_threshold:
            return False
        return setup_label in self.blocked_setups
