"""Backtest engine: BacktestEngine, stub agent integration, BacktestJournal.

F2-2 port of TMAS engine. Real Reflector/Risk agents are replaced with
minimal pass-through stubs (see ``stubs.py``) — full risk logic lives in
``apps/api/src/agents/trader/risk/manager.py`` (async, trader-loop scoped)
and is wired separately.
"""

from apps.api.src.agents.trader.engine.config import (
    AnalystParams,
    BacktestConfig,
    DailyRiskState,
    DynamicRiskConfig,
    RiskConfig,
)
from apps.api.src.agents.trader.engine.result import BacktestResult

# BacktestEngine va BacktestJournal lazy import — circular import oldini olish uchun.
__all__ = [
    "AnalystParams",
    "BacktestConfig",
    "BacktestEngine",
    "BacktestJournal",
    "BacktestResult",
    "DailyRiskState",
    "DynamicRiskConfig",
    "RiskConfig",
]


def __getattr__(name: str):
    if name == "BacktestEngine":
        from apps.api.src.agents.trader.engine.engine import BacktestEngine
        return BacktestEngine
    if name == "BacktestJournal":
        from apps.api.src.agents.trader.engine.backtest_journal import BacktestJournal
        return BacktestJournal
    raise AttributeError(
        f"module 'apps.api.src.agents.trader.engine' has no attribute {name!r}"
    )
