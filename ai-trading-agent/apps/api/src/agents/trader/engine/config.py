"""
BacktestConfig — Backtest engine uchun konfiguratsiya poydevori.

Barcha dataclass'lar bu yerda jamlangan: risk parametrlari, dinamik risk
holati, kunlik risk hisoblagichlari, analyst parametrlari va asosiy
BacktestConfig. Vaqt maydonlari har doim tz-aware (UTC) bo'lishi shart —
naive datetime TAQIQLANGAN (DTZ qoidalari va look-ahead bias himoyasi).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from apps.api.src.agents.trader.core.broker import BrokerConfig



@dataclass(frozen=True)
class RiskConfig:
    """Risk boshqaruvi uchun immutable parametrlar."""

    risk_per_trade_pct: float = 1.0
    max_open_positions: int = 3
    max_daily_loss_pct: float = 5.0
    min_rr: float = 2.0
    losing_streak_threshold: int = 3
    streak_risk_multiplier: float = 0.5


@dataclass
class DynamicRiskConfig:
    """Dinamik risk holati — losing streak'ga qarab o'zgaradi."""

    current_risk_multiplier: float = 1.0
    consecutive_losses: int = 0
    paused_until: datetime | None = None


@dataclass
class DailyRiskState:
    """Bir kun ichidagi risk hisoblagichlari (P&L, savdolar soni)."""

    daily_loss_pnl: float = 0.0
    daily_profit_pnl: float = 0.0
    trades_today: int = 0
    last_reset_date: datetime | None = None


@dataclass(frozen=True)
class AnalystParams:
    """Analyst agent uchun immutable parametrlar."""

    min_confluence: float = 0.6
    sniper_min_score: int = 7
    flow_min_score: int = 3
    htf_lookback_bars: int = 200


@dataclass(frozen=True)
class BacktestConfig:
    """Backtest engine uchun asosiy konfiguratsiya — barcha komponentlarni bog'laydi."""

    start: datetime
    end: datetime
    symbol: str
    data_path: Path
    broker: BrokerConfig
    risk_config: RiskConfig
    primary_timeframe: str = "M15"
    timeframes: tuple[str, ...] = ("M15", "H1", "H4")
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    min_confluence: float = 0.6
    reflector_shadow_mode: bool = True
    seed: int = 42
    analyst_params: AnalystParams = field(default_factory=AnalystParams)

    def __post_init__(self) -> None:
        if self.start.tzinfo is None:
            raise ValueError(
                "BacktestConfig.start must be tz-aware (UTC). Got naive datetime."
            )
        if self.end.tzinfo is None:
            raise ValueError(
                "BacktestConfig.end must be tz-aware (UTC). Got naive datetime."
            )
        if self.end <= self.start:
            raise ValueError(
                f"BacktestConfig.end ({self.end.isoformat()}) must be strictly "
                f"after BacktestConfig.start ({self.start.isoformat()})."
            )
        if not self.symbol:
            raise ValueError("BacktestConfig.symbol cannot be empty")
        if self.primary_timeframe not in self.timeframes:
            raise ValueError(
                f"BacktestConfig.primary_timeframe ({self.primary_timeframe!r}) "
                f"must be one of timeframes ({self.timeframes!r})."
            )
