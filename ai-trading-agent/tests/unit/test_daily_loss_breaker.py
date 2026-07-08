"""
T-01 regression: kunlik-zarar breaker'ning callee kontrakti.

Bug (T-01): `risk/manager.py:142 add_closed_trade()` BUTUN repo'da 0 marta
chaqiriladi → `RiskManagement._today_trades` doim bo'sh → `check_trade_allowed`
dagi `daily_pnl` doim 0 → agent.py'dagi "kunlik zarar → faqat manage" darvozasi
HECH QACHON ishlamaydi. Wiring tuzatishi (agent.py'da har savdo yopilganda
`self.risk.add_closed_trade(...)`) shu callee kontraktiga tayanadi.

Bu test callee'ning O'ZI (RiskManagement.add_closed_trade) kutilgan shartnomaga
amal qilishini qotiradi, shunda wiring xavfsiz tayana oladi:
  • bugungi sana bilan yopilgan savdo QO'SHILADI (win + loss — pnl belgisidan
    qat'i nazar; kunlik PROFIT target'i ham shu ro'yxatdan o'qiydi);
  • o'tgan kun (today-filter tashqarisidagi) yopilgan savdo QO'SHILMAYDI.

Determinizm: `frozen_now` (freezegun, 2026-05-14 12:00 UTC) + RealClock. Global
soatni `set_clock(None)` orqali RealClock'ga majburlaymiz, shunda oldingi
testdan qolib ketishi mumkin bo'lgan VirtualClock ta'sir qilmaydi.
"""
from __future__ import annotations

from datetime import timedelta

from apps.api.src.agents.trader.core.clock import get_clock, set_clock
from apps.api.src.agents.trader.models.config import RiskConfig
from apps.api.src.agents.trader.risk.manager import RiskManagement


def test_add_closed_trade_today_filter(frozen_now) -> None:
    """T-01 callee kontrakti: bugungi savdo qo'shiladi, o'tgan kunniki yo'q."""
    # RealClock'ni majburla — freezegun (`frozen_now`) faqat wall-clock'ni
    # qotiradi; leaked VirtualClock bu testni nondeterministik qilib qo'yardi.
    set_clock(None)
    try:
        rm = RiskManagement(RiskConfig())
        assert rm._today_trades == []  # boshlang'ich holat: bo'sh

        now = get_clock().now()  # == frozen_now (2026-05-14 12:00 UTC)

        # 1) Bugun yopilgan LOSS → qo'shiladi (len 0 → 1).
        rm.add_closed_trade({"pnl": -20.0, "close_time": now})
        assert len(rm._today_trades) == 1
        assert rm._today_trades[0]["pnl"] == -20.0

        # 2) 2 kun oldin yopilgan savdo → today-filter uni RAD etadi (len == 1).
        rm.add_closed_trade({"pnl": -99.0, "close_time": now - timedelta(days=2)})
        assert len(rm._today_trades) == 1
        # O'tgan-kun savdosi ro'yxatga umuman kirmagan.
        assert all(t["pnl"] != -99.0 for t in rm._today_trades)

        # 3) Bugun yopilgan WIN ham qayd etiladi (pnl belgisi filtr emas) → len 2.
        rm.add_closed_trade({"pnl": 35.0, "close_time": now})
        assert len(rm._today_trades) == 2
        assert {t["pnl"] for t in rm._today_trades} == {-20.0, 35.0}
    finally:
        set_clock(None)  # global soatni tozalab qo'y (test izolatsiyasi)
