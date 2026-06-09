"""Shared backtest runner — single source of truth for engine wiring.

Both the CLI (``__main__``) and the offline validation job
(``analysis.offline_validation``) need to stand up a ``BacktestEngine`` with the
same production wiring: blocklist ∪ defaults, the high-volatility regime gate,
and the structural min-RR floor. Duplicating that across call sites is how the
two drift apart, so it lives here.

``run_backtest(start, end, params)`` returns the raw ``BacktestResult`` plus the
computed ``PerformanceReport`` and the journal rows / equity curve, because the
raw result carries None for win_rate / profit_factor / sharpe (a known
serialization gap) — the real metrics only exist on the PerformanceReport.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from apps.api.src.agents.trader.analysis import performance as perf
from apps.api.src.agents.trader.core.broker import BrokerConfig
from apps.api.src.agents.trader.core.clock import VirtualClock, set_clock
from apps.api.src.agents.trader.core.data import HistoricalDataManager
from apps.api.src.agents.trader.engine.analyst_ict import ICTAnalyst
from apps.api.src.agents.trader.engine.config import BacktestConfig
from apps.api.src.agents.trader.engine.config import RiskConfig as EngineRiskConfig
from apps.api.src.agents.trader.engine.engine import BacktestEngine
from apps.api.src.agents.trader.engine.regime import (
    DEFAULT_REGIME_BLOCKED_SETUPS,
    RegimeGate,
)
from apps.api.src.agents.trader.engine.result import BacktestResult


@dataclass(frozen=True)
class BacktestParams:
    """Everything needed to wire one backtest run, independent of date window.

    ``block_setup`` are CLI-style extra labels unioned with the analyst's
    ``DEFAULT_BLOCKED_SETUPS`` (matching __main__'s behaviour). The regime gate
    and min-RR floor are off by default so a bare run reproduces v3.
    """
    symbol: str
    data_path: Path
    primary_timeframe: str = "M15"
    initial_balance: float = 10000.0
    risk_per_trade: float = 1.0
    block_setup: tuple[str, ...] = ()
    regime_atr_threshold: float = 0.0
    regime_atr_period: int = 14
    regime_block_setup: tuple[str, ...] = ()
    min_rr: float = 0.0


@dataclass
class RunArtifacts:
    result: BacktestResult
    perf_report: Any
    closed_trades: list
    equity_curve: list
    broker_summary: dict


def _timeframes_for(primary: str) -> tuple[str, ...]:
    if primary == "M15":
        return ("M15", "H1", "H4")
    if primary == "H1":
        return ("H1", "H4")
    if primary == "H4":
        return ("H4",)
    return (primary,)


def build_analyst(params: BacktestParams, clock, data) -> ICTAnalyst:
    """Wire an ICTAnalyst with blocklist ∪ defaults + regime gate + min-RR floor.

    Identical policy to __main__.main(); extracted so the CLI and the offline
    validators cannot drift.
    """
    blocked = set(ICTAnalyst.DEFAULT_BLOCKED_SETUPS) | set(params.block_setup)
    regime = None
    if params.regime_atr_threshold and params.regime_atr_threshold > 0:
        regime_blocked = (
            frozenset(params.regime_block_setup) if params.regime_block_setup
            else DEFAULT_REGIME_BLOCKED_SETUPS
        )
        regime = RegimeGate(
            atr_threshold=params.regime_atr_threshold,
            atr_period=params.regime_atr_period,
            blocked_setups=regime_blocked,
        )
    return ICTAnalyst(
        clock=clock, data=data, blocked_setups=blocked, regime=regime,
        min_rr=params.min_rr,
    )


def run_backtest(
    start: datetime,
    end: datetime,
    params: BacktestParams,
    *,
    progress_every_bars: int = 0,
) -> RunArtifacts:
    """Stand up + run one backtest over [start, end). Owns the global clock
    lifecycle (set on entry, restored to None on exit) so repeated calls — e.g.
    walk-forward windows — never leak a virtual clock into later runs."""
    broker_cfg = BrokerConfig(
        initial_balance=params.initial_balance,
        commission_per_lot=3.0,
        slippage_model="fixed",
        swap_rates={},
        fixed_slippage_pips=1.0,
        leverage=100,
        contract_size=100,  # XAUUSD default
    )
    risk_cfg = EngineRiskConfig(risk_per_trade_pct=params.risk_per_trade)
    timeframes = _timeframes_for(params.primary_timeframe)

    config = BacktestConfig(
        start=start,
        end=end,
        symbol=params.symbol,
        data_path=params.data_path,
        broker=broker_cfg,
        risk_config=risk_cfg,
        primary_timeframe=params.primary_timeframe,
        timeframes=timeframes,
    )

    clock = VirtualClock(start=start, end=end)
    set_clock(clock)
    try:
        data = HistoricalDataManager(data_path=params.data_path)
        data.preload(params.symbol, timeframes=list(timeframes))

        analyst = build_analyst(params, clock, data)
        engine = BacktestEngine(
            config, analyst=analyst, progress_every_bars=progress_every_bars,
        )
        # Analyst shares the engine's clock + data (same simulated time).
        analyst.clock = engine.clock
        analyst.data = engine.data

        result = engine.run()
        broker_summary = engine.broker.get_summary()

        if hasattr(engine.journal, "get_closed_trades"):
            closed_trades = engine.journal.get_closed_trades()
        else:
            closed_trades = list(engine.broker.history)
        equity_curve = (
            engine.journal.get_equity_curve()
            if hasattr(engine.journal, "get_equity_curve") else []
        )

        perf_report = perf.compute(
            closed_trades=closed_trades,
            equity_curve=equity_curve,
            starting_balance=float(config.broker.initial_balance),
            run_id=config.run_id,
        )
        return RunArtifacts(
            result=result,
            perf_report=perf_report,
            closed_trades=closed_trades,
            equity_curve=equity_curve,
            broker_summary=broker_summary,
        )
    finally:
        set_clock(None)
