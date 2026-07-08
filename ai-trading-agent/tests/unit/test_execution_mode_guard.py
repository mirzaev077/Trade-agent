"""T-02: EXECUTION_MODE hard safety guard — connect-time REAL-account block.

Load-bearing safety regression. Before this guard, demo vs. live was implied
solely by the MT5_SERVER string; nothing stopped the bot from trading a REAL
(live-money) account while operator intent was still "demo".

`TraderAgent.connect()` now reads `config.execution_mode` and inspects the
broker's account class. `MT5Connector.connect()` returns the raw account
`trade_mode` mapped to the STRING "Demo" (trade_mode==0) or "Real" (CONTEST /
REAL both map to "Real" — conservative). The guard:

  * sets `self._account_is_real = (info.get("trade_mode") == "Real")`
  * if the account is REAL and `execution_mode != "live"` → raises
    `MT5ConnectorError` BEFORE any trading state is initialised, so startup
    (main.py wraps connect() in except) is blocked.

Back-compat: default execution_mode="demo" is inert on Demo/sim accounts
(_account_is_real stays False), so existing behaviour is unchanged.

This test fails on any tree where the guard is absent (REAL + demo would
connect silently, and `_account_is_real` would not exist / stay False).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apps.api.src.agents.trader.agent import TraderAgent
from apps.api.src.agents.trader.models.config import TradingConfig
from apps.api.src.agents.trader.mt5_connector import MT5ConnectorError


def _build_agent(execution_mode: str, trade_mode: str):
    """A TraderAgent whose MT5 layer is a MagicMock reporting `trade_mode`.

    `_env_file=None` + the clean_config_env fixture keep the config purely
    default-driven (no stray .env / OS env leakage); only execution_mode is
    varied. The mocked connector's connect() hands back the documented dict
    shape and find_symbol resolves the trade symbol so the non-raising paths
    run to completion.
    """
    config = TradingConfig(_env_file=None, execution_mode=execution_mode)
    agent = TraderAgent(config=config)

    mock = MagicMock(name="MT5Connector")
    mock.connect.return_value = {
        "login": 1,
        "balance": 1000.0,
        "leverage": 100,
        "server": "S",
        "currency": "USD",
        "trade_mode": trade_mode,
    }
    mock.find_symbol.return_value = "XAUUSD"
    # Defensive: connect()'s post-guard recovery path may touch the connector;
    # an empty book keeps it deterministic (and it is try/except-wrapped anyway).
    mock.get_open_positions.return_value = []
    agent.mt5 = mock
    return agent, mock


def test_connect_rejects_real_account_unless_live(clean_config_env, tmp_state_dir):
    # ── REAL account + execution_mode='demo' → hard reject ────────────────
    # The guard must fire, and _account_is_real must be set True *before* the
    # raise (it is read again by the order-time defense-in-depth guard).
    agent, mock = _build_agent(execution_mode="demo", trade_mode="Real")
    with pytest.raises(MT5ConnectorError):
        agent.connect(1, "p", "S")
    assert agent._account_is_real is True

    # ── REAL account + execution_mode='live' → explicitly allowed ─────────
    agent, mock = _build_agent(execution_mode="live", trade_mode="Real")
    info = agent.connect(1, "p", "S")          # must NOT raise
    assert info["trade_mode"] == "Real"
    assert agent._account_is_real is True

    # ── DEMO account + execution_mode='demo' → guard inert (back-compat) ──
    agent, mock = _build_agent(execution_mode="demo", trade_mode="Demo")
    info = agent.connect(1, "p", "S")          # must NOT raise
    assert info["trade_mode"] == "Demo"
    assert agent._account_is_real is False


# ── T-02 order-time defense-in-depth guard (_place_zone_limits) ───────────────
# Second, independent line of defence. Even if a REAL account slipped past the
# connect-time guard above, the SOLE entry-order method must place nothing while
# in demo mode. _place_zone_limits() routes both market orders (mt5.place_order)
# and pending limit orders (mt5.place_pending_order); the guard sits at the very
# top and returns before any sizing/placement.


def _one_valid_zone() -> dict:
    """A single plausible BUY zone. The guard returns before the zone is ever
    inspected, but passing a realistic dict keeps the call faithful to a real
    placement attempt (tf / direction / htf_conf / entry / sl)."""
    return {
        "tf": "H1",
        "direction": "buy",
        "htf_conf": 3,
        "entry": 1999.5,
        "sl": 1996.0,
        "label": "H1_OB",
        "zone_low": 1999.0,
        "zone_high": 2000.0,
    }


async def test_order_guard_blocks_real_account_in_demo_mode(clean_config_env, tmp_state_dir):
    """REAL account + execution_mode='demo' → _place_zone_limits early-returns at
    the top, placing no order of any kind (market OR pending)."""
    agent, mock = _build_agent(execution_mode="demo", trade_mode="Demo")
    agent._account_is_real = True  # pretend connect() detected a REAL account

    await agent._place_zone_limits(
        zones=[_one_valid_zone()],
        want_dir="buy",
        account={"balance": 10000.0},
        current_price=2000.0,
        ict_map={},
    )

    # The load-bearing assertion: neither entry-order path was reached.
    mock.place_order.assert_not_called()
    mock.place_pending_order.assert_not_called()


async def test_order_guard_inert_when_account_not_real(clean_config_env, tmp_state_dir):
    """Control (back-compat): demo/sim account (_account_is_real=False) → the
    guard is inert and execution proceeds past it into sizing. We stop the method
    right after the guard via a sentinel on the first post-guard mt5 call
    (get_symbol_info), proving the guard did NOT early-return without exercising
    the full placement path."""
    agent, mock = _build_agent(execution_mode="demo", trade_mode="Demo")
    agent._account_is_real = False

    class _PastGuard(Exception):
        pass

    # get_symbol_info is the first self.mt5 call after the guard; reaching it
    # means the guard let execution through.
    mock.get_symbol_info.side_effect = _PastGuard

    with pytest.raises(_PastGuard):
        await agent._place_zone_limits(
            zones=[_one_valid_zone()],
            want_dir="buy",
            account={"balance": 10000.0},
            current_price=2000.0,
            ict_map={},
        )

    mock.get_symbol_info.assert_called_once()
