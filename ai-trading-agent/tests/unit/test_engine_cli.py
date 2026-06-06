"""F2-3.1 diagnostic: --block-setup CLI flag parsing + union with defaults.

The engine's ``main()`` unions ``ICTAnalyst.DEFAULT_BLOCKED_SETUPS`` with any
``--block-setup LABEL`` flags so a loser setup can be isolated from the command
line without code changes. These tests pin the parsing contract (repeatable
append, empty default) and the union semantics main() relies on, without
running a full backtest.
"""

from __future__ import annotations

from apps.api.src.agents.trader.engine.__main__ import _parse_args
from apps.api.src.agents.trader.engine.analyst_ict import ICTAnalyst
from apps.api.src.agents.trader.engine.regime import (
    DEFAULT_REGIME_BLOCKED_SETUPS,
    RegimeGate,
)

_BASE = ["--symbol", "XAUUSD", "--start", "2024-01-01", "--end", "2024-02-01"]


def test_block_setup_defaults_to_empty_list() -> None:
    """Omitting --block-setup yields an empty list (not None)."""
    ns = _parse_args(_BASE)
    assert ns.block_setup == []


def test_block_setup_is_repeatable() -> None:
    """Each --block-setup appends; order preserved."""
    ns = _parse_args(_BASE + ["--block-setup", "H1_DR_Eq", "--block-setup", "H4_RB"])
    assert ns.block_setup == ["H1_DR_Eq", "H4_RB"]


def test_block_setup_unions_with_analyst_defaults() -> None:
    """main()'s union: defaults ∪ CLI, fed to ICTAnalyst.blocked_setups.

    Mirrors the exact expression in main() so the wiring is covered without a
    backtest. Confirms defaults survive AND the CLI labels are added.
    """
    ns = _parse_args(_BASE + ["--block-setup", "H1_DR_Eq", "--block-setup", "H1_BEAR"])
    blocked = set(ICTAnalyst.DEFAULT_BLOCKED_SETUPS) | set(ns.block_setup)

    # CLI labels present.
    assert {"H1_DR_Eq", "H1_BEAR"} <= blocked
    # Defaults preserved (not clobbered by the CLI set).
    assert set(ICTAnalyst.DEFAULT_BLOCKED_SETUPS) <= blocked

    # The union is what an analyst actually filters on.
    analyst = ICTAnalyst(clock=None, data=None, blocked_setups=blocked)
    assert "H1_DR_Eq" in analyst.blocked_setups
    assert "M15_OTE" in analyst.blocked_setups  # a default


# ── F4 regime gate CLI ────────────────────────────────────────────────────────


def test_regime_flags_default_to_off() -> None:
    """Omitting regime flags → gate disabled (default run reproduces v3)."""
    ns = _parse_args(_BASE)
    assert ns.regime_atr_threshold == 0.0
    assert ns.regime_atr_period == 14
    assert ns.regime_block_setup == []


def test_regime_threshold_and_period_parse() -> None:
    ns = _parse_args(_BASE + ["--regime-atr-threshold", "0.18", "--regime-atr-period", "20"])
    assert ns.regime_atr_threshold == 0.18
    assert ns.regime_atr_period == 20


def test_regime_block_setup_is_repeatable() -> None:
    ns = _parse_args(_BASE + ["--regime-block-setup", "M15_OB", "--regime-block-setup", "M15_BB"])
    assert ns.regime_block_setup == ["M15_OB", "M15_BB"]


def test_regime_gate_construction_mirrors_main() -> None:
    """Mirror main()'s wiring: threshold>0 with no --regime-block-setup → the
    default core blocklist; the resulting gate is enabled."""
    ns = _parse_args(_BASE + ["--regime-atr-threshold", "0.18"])
    regime_blocked = (
        frozenset(ns.regime_block_setup) if ns.regime_block_setup
        else DEFAULT_REGIME_BLOCKED_SETUPS
    )
    gate = RegimeGate(
        atr_threshold=ns.regime_atr_threshold,
        atr_period=ns.regime_atr_period,
        blocked_setups=regime_blocked,
    )
    assert gate.enabled is True
    assert {"M15_OB", "M15_BB", "M15_CISD"} <= gate.blocked_setups


def test_regime_disabled_when_threshold_zero() -> None:
    """threshold defaults to 0 → main() leaves regime=None (gate off)."""
    ns = _parse_args(_BASE)
    assert not (ns.regime_atr_threshold and ns.regime_atr_threshold > 0)
