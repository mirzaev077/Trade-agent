"""F2-2: Minimal pass-through stubs for Reflector + Risk agents.

The TMAS engine expects these protocols. For the MVP backtest, we don't
want to port the full TMAS agents (DB-dependent, async-heavy). These stubs
satisfy the interface with the simplest possible behavior:

  * ``StubReflector``: always passes signal through (no blocking).
  * ``StubRisk``: always approves with a fixed lot size; records trade
    outcomes as a no-op.

Real risk management will be wired in a follow-up. For now, the goal is
proving the engine end-to-end works with our ICT adapter. The real
``RiskManagement`` in ``apps/api/src/agents/trader/risk/manager.py`` is
async-friendly and trader-loop-scoped — adapting it for the engine is its
own task.

API SURFACE (what engine.py + backtest_journal.py actually use):

  reflector.evaluate_signal_sync(signal, market_context)
      → object with .block_reason: str | None
                    .adjustments:  list (iterable; items expose
                                   id/agent_id/param_name/param_path/
                                   old_value/new_value/reason/evidence)
                    .confidence:   float
                    .notes:        str

  risk.approve_trade(signal, broker)
      → dataclass with .approved   : bool
                       .lot_size   : float (> 0 if approved)
                       .reason     : str
                       (asdict()-friendly — used by journal)

  risk.record_trade_outcome(pnl, exit_time)
      → None (side effects only)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# ── Reflector stub ────────────────────────────────────────────────────────────


@dataclass
class StubReflectorResult:
    """Mirror of TMAS ReflectorResult — exposes attributes journal expects."""

    block_reason: str | None = None
    adjustments: list = field(default_factory=list)
    confidence: float = 1.0
    notes: str = ""


class StubReflector:
    """Always-approve reflector. No Claude calls, no DB writes."""

    def __init__(self, shadow_mode: bool = True) -> None:
        # Engine reads ``reflector.shadow_mode`` only via tests; keep it for
        # API parity with the full ReflectorAgent.
        self.shadow_mode = shadow_mode

    def evaluate_signal_sync(
        self,
        signal: Any,
        market_context: dict,
    ) -> StubReflectorResult:
        """Pass the signal through unconditionally."""
        return StubReflectorResult()


# ── Risk stub ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StubRiskCheckResult:
    """Mirror of TMAS RiskCheckResult — frozen so ``asdict`` is stable.

    Note: fields match the journal's expectations (lot_size, reason,
    risk_amount_usd, risk_pct). approved=True requires lot_size > 0.
    """

    approved: bool
    reason: str
    lot_size: float = 0.0
    risk_amount_usd: float = 0.0
    risk_pct: float = 0.0


class StubRisk:
    """Always-approve risk agent. Fixed lot size, no streak tracking."""

    def __init__(self, lot_size: float = 0.01) -> None:
        self._lot_size = lot_size

    def approve_trade(self, signal: Any, broker: Any) -> StubRiskCheckResult:
        """Always approve at the configured lot size."""
        return StubRiskCheckResult(
            approved=True,
            reason="stub-approve",
            lot_size=self._lot_size,
            risk_amount_usd=0.0,
            risk_pct=0.0,
        )

    def record_trade_outcome(self, pnl: float, exit_time: datetime) -> None:
        """No-op — engine calls this on every closed trade and at end-of-test."""
        return None
