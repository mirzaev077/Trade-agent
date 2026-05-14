"""
Signal va RiskCheckResult dataclass'lari — Analyst → Reflector → Risk → Executor pipeline data.

Strategy analytics maydonlari (setup_type, session, day_of_week, mode) backtest
trades jadvali ustunlari bilan moslashtirilgan.

LINT QOIDASI: `datetime.utcnow()` ishlatish TAQIQLANGAN — har doim clock.now() orqali.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

_VALID_DIRECTIONS = frozenset({"buy", "sell"})
_VALID_MODES = frozenset({"sniper", "flow"})
_VALID_SESSIONS = frozenset({"asia", "london", "ny", "off"})
_VALID_DOW = frozenset({"mon", "tue", "wed", "thu", "fri", "sat", "sun"})


@dataclass(frozen=True)
class Signal:
    """
    Analyst → pipeline signal payloadi.

    :param signal_id: Unikal identifikator (UUID4 default)
    :param symbol: Savdo instrumenti (masalan 'EURUSD')
    :param direction: 'buy' | 'sell'
    :param entry_price: Taklif qilingan kirish narxi
    :param sl: Stop Loss narxi (None — Risk gate `min_rr` ni hisoblay olmaydi)
    :param tp: Take Profit narxi
    :param confluence_score: 0.0–1.0 confluence ball (Reflector low_confluence gate uchun)
    :param setup_type: Setup turi: 'OB' | 'FVG' | 'BB' | 'OTE' | 'PDL' | ...
    :param session: Bozor session: 'asia' | 'london' | 'ny' | 'off'
    :param day_of_week: Hafta kuni: 'mon' | 'tue' | ... | 'sun'
    :param mode: Analyst mode: 'sniper' | 'flow'
    :param timestamp: Signal yaratilgan vaqt (tz-aware UTC majburiy)
    :param metadata: Qo'shimcha analyst ma'lumotlari (HTF bias, manipulation, scores)
    """

    symbol: str
    direction: str
    entry_price: float
    sl: float | None
    tp: float | None
    confluence_score: float
    setup_type: str
    session: str
    day_of_week: str
    mode: str
    timestamp: datetime
    signal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError(
                "Signal.timestamp must be tz-aware (UTC). Got naive datetime."
            )
        if self.direction not in _VALID_DIRECTIONS:
            raise ValueError(
                f"Signal.direction ({self.direction!r}) must be one of {sorted(_VALID_DIRECTIONS)!r}."
            )
        if not self.symbol:
            raise ValueError("Signal.symbol cannot be empty")
        if self.entry_price <= 0:
            raise ValueError(
                f"Signal.entry_price must be positive, got {self.entry_price}"
            )
        if not (0.0 <= self.confluence_score <= 1.0):
            raise ValueError(
                f"Signal.confluence_score must be in [0.0, 1.0], got {self.confluence_score}"
            )
        if self.mode not in _VALID_MODES:
            raise ValueError(
                f"Signal.mode ({self.mode!r}) must be one of {sorted(_VALID_MODES)!r}."
            )
        if self.session not in _VALID_SESSIONS:
            raise ValueError(
                f"Signal.session ({self.session!r}) must be one of {sorted(_VALID_SESSIONS)!r}."
            )
        if self.day_of_week not in _VALID_DOW:
            raise ValueError(
                f"Signal.day_of_week ({self.day_of_week!r}) must be one of {sorted(_VALID_DOW)!r}."
            )

    def rr(self) -> float | None:
        """
        Risk:Reward nisbatini hisoblaydi.

        :return: |TP-entry| / |entry-SL|, yoki None agar SL/TP yo'q
        """
        if self.sl is None or self.tp is None:
            return None
        risk = abs(self.entry_price - self.sl)
        reward = abs(self.tp - self.entry_price)
        if risk == 0:
            return None
        return reward / risk


@dataclass(frozen=True)
class RiskCheckResult:
    """
    RiskAgent.approve_trade natijasi.

    :param approved: Signal qabul qilindimi
    :param reason: Tasdiqlash yoki rad etish sababi (audit uchun)
    :param lot_size: Hisoblangan lot hajmi (approved=True bo'lganda > 0)
    :param risk_amount_usd: Bu trade'ga ajratilgan risk miqdori (USD)
    :param risk_pct: Joriy risk foiz (dynamic multiplier qo'llangan)
    """

    approved: bool
    reason: str
    lot_size: float = 0.0
    risk_amount_usd: float = 0.0
    risk_pct: float = 0.0

    def __post_init__(self) -> None:
        if self.approved and self.lot_size <= 0:
            raise ValueError(
                f"RiskCheckResult.lot_size must be > 0 when approved=True, got {self.lot_size}"
            )
        if not self.approved and self.lot_size != 0.0:
            raise ValueError(
                f"RiskCheckResult.lot_size must be 0.0 when approved=False, got {self.lot_size}"
            )
