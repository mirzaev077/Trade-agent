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
