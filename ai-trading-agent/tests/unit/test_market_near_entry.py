"""Market-near-zone entry routing — narx zonaga yaqin bo'lsa limit o'rniga market.

`_place_zone_limits()` ikkala entry yo'lini boshqaradi:
  • pending limit  → mt5.place_pending_order   (narx zonadan uzoq — kutamiz)
  • market (deal)  → mt5.place_order           (narx entry'ga ≤MARKET_NEAR_PIPS)

Xulq `.env MARKET_NEAR_PIPS` (config.market_near_pips) bilan sozlanadi:
  • >0 → narx entry'ga shu pip-masofa ichida bo'lsa MARKET
  • 0  → o'chiq, doim LIMIT (eski xulq, back-compat)

Bu test uch stsenariyni qamrab oladi: yaqin→market, uzoq→limit, off→limit.
mt5 to'liq mock; brain o'chirilgan (API key yo'q) → sof yo'nalish mantig'i.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apps.api.src.agents.trader.agent import TraderAgent
from apps.api.src.agents.trader.models.config import TradingConfig


def _build_agent(market_near_pips: float):
    """MARKET_NEAR_PIPS bilan TraderAgent + guard/sizing yo'lini bosib o'tishga
    yetadigan mock MT5. get_symbol_info XAUUSD-ga o'xshash lot metrikalarini,
    place_order/place_pending_order esa ticket+price'li natijalarni qaytaradi."""
    config = TradingConfig(_env_file=None, market_near_pips=market_near_pips)
    agent = TraderAgent(config=config)

    mock = MagicMock(name="MT5Connector")
    mock.get_symbol_info.return_value = {
        "point": 0.01, "trade_tick_value": 1.0,
        "volume_min": 0.01, "volume_max": 500.0, "volume_step": 0.01,
    }
    mock.place_order.return_value = MagicMock(ticket=111, price=2000.0)
    mock.place_pending_order.return_value = MagicMock(ticket=222, price=1990.0)
    agent.mt5 = mock
    return agent, mock


def _zone(entry: float, sl: float) -> dict:
    """Bitta ishonchli BUY H1 zona (long-TF → sessiya/htf_conf sharti yo'q).
    zone_lo/zone_hi berilmaydi → learner entry_pct entry'ni surmaydi."""
    return {
        "tf": "H1", "direction": "buy", "htf_conf": 3,
        "entry": entry, "sl": sl, "label": "H1_OB", "quality": 3,
    }


async def test_near_zone_places_market(clean_config_env, tmp_state_dir):
    """Narx entry'ga 3 pip yaqin (≤5 pip threshold) → MARKET, limit EMAS."""
    agent, mock = _build_agent(market_near_pips=5.0)

    await agent._place_zone_limits(
        zones=[_zone(entry=1999.7, sl=1995.7)],  # 2000.0 dan 3 pip past
        want_dir="buy",
        account={"balance": 10000.0},
        current_price=2000.0,
        ict_map={},
    )

    mock.place_order.assert_called_once()          # market yo'li
    mock.place_pending_order.assert_not_called()   # limit yo'li EMAS


async def test_far_zone_places_limit(clean_config_env, tmp_state_dir):
    """Narx entry'dan 100 pip uzoq (>5 pip) → pending LIMIT, market EMAS."""
    agent, mock = _build_agent(market_near_pips=5.0)

    await agent._place_zone_limits(
        zones=[_zone(entry=1990.0, sl=1986.0)],  # 2000.0 dan 100 pip past
        want_dir="buy",
        account={"balance": 10000.0},
        current_price=2000.0,
        ict_map={},
    )

    mock.place_pending_order.assert_called_once()  # limit yo'li
    mock.place_order.assert_not_called()           # market yo'li EMAS


async def test_disabled_always_limit(clean_config_env, tmp_state_dir):
    """MARKET_NEAR_PIPS=0 → narx yaqin bo'lsa ham doim LIMIT (back-compat)."""
    agent, mock = _build_agent(market_near_pips=0.0)

    await agent._place_zone_limits(
        zones=[_zone(entry=1999.7, sl=1995.7)],  # 3 pip yaqin — lekin off
        want_dir="buy",
        account={"balance": 10000.0},
        current_price=2000.0,
        ict_map={},
    )

    mock.place_pending_order.assert_called_once()  # off → limit
    mock.place_order.assert_not_called()
