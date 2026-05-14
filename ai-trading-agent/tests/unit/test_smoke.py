"""
F1-5: Headline smoke tests.

Goals:
  • Verify every top-level subsystem imports and runs without crashing on
    realistic, deterministic input.
  • Lock down public-API shapes (attribute names, return types).
  • Catch the regressions most likely to leak into live trading:
      - ICTAnalysis.analyze() blowing up on any timeframe
      - RiskConfig invariants drifting (sum-to-100, daily>=per-trade, etc.)
      - TradingAIBrain accidentally calling Anthropic with no API key
      - MT5Connector sim mode breaking
      - State persistence round-trip losing data
      - F1 admin-approval DB layer regressions
      - F1-4 healthcheck initial status

Tests are intentionally narrow — single responsibility, no business-logic
assertions beyond "did not crash + returned the documented shape".
"""
from __future__ import annotations

from datetime import datetime, timezone  # noqa: F401  # used by frozen_now fixture consumers

import pytest
from pydantic import ValidationError

from apps.api.src.agents.trader.analysis.ict import ICTAnalysis
from apps.api.src.agents.trader.brain.ai_validator import TradingAIBrain
from apps.api.src.agents.trader.health import HealthState, _compute_status
from apps.api.src.agents.trader.models.config import RiskConfig, TradingConfig
from apps.api.src.agents.trader.models.signals import AIDecision, ICTSignal, TradeSignal
from apps.api.src.agents.trader.mt5_connector import MT5Connector
from apps.api.src.agents.trader.risk.manager import RiskManagement
from apps.api.src.agents.trader.state import db as state_db
from apps.api.src.agents.trader.state.persistence import (
    load_trade_meta,
    save_trade_meta,
)


# ---------------------------------------------------------------------------
# Category 1 — ICTAnalysis.analyze() crash-free across all 6 timeframes
# ---------------------------------------------------------------------------
# The agent loop feeds D1/H4/H1/M30/M15/M5 candles to a single ICTAnalysis
# instance. Any uncaught exception here aborts the tick. Smoke-level guarantee:
# every TF returns a populated ICTSignal with the documented attributes.


def _assert_ict_shape(result):
    assert isinstance(result, ICTSignal)
    # Documented public attributes (see models/signals.py:ICTSignal)
    for attr in ("signal_type", "structure", "order_blocks", "fvgs",
                 "liquidity_zones", "kill_zone", "pd_zone", "confidence"):
        assert hasattr(result, attr), f"ICTSignal missing attr: {attr}"
    assert isinstance(result.structure, dict)
    assert isinstance(result.signal_type, str)


def test_ict_analyze_does_not_crash_on_d1(sample_candles_xauusd_m15):
    result = ICTAnalysis().analyze(sample_candles_xauusd_m15, timeframe="D1")
    _assert_ict_shape(result)


def test_ict_analyze_does_not_crash_on_h4(sample_candles_xauusd_m15):
    result = ICTAnalysis().analyze(sample_candles_xauusd_m15, timeframe="H4")
    _assert_ict_shape(result)


def test_ict_analyze_does_not_crash_on_h1(sample_candles_xauusd_m15):
    result = ICTAnalysis().analyze(sample_candles_xauusd_m15, timeframe="H1")
    _assert_ict_shape(result)


def test_ict_analyze_does_not_crash_on_m30(sample_candles_xauusd_m15):
    result = ICTAnalysis().analyze(sample_candles_xauusd_m15, timeframe="M30")
    _assert_ict_shape(result)


def test_ict_analyze_does_not_crash_on_m15(sample_candles_xauusd_m15):
    result = ICTAnalysis().analyze(sample_candles_xauusd_m15, timeframe="M15")
    _assert_ict_shape(result)


def test_ict_analyze_does_not_crash_on_m5(sample_candles_xauusd_m15):
    result = ICTAnalysis().analyze(sample_candles_xauusd_m15, timeframe="M5")
    _assert_ict_shape(result)


# ---------------------------------------------------------------------------
# Category 2 — RiskManagement.calculate_position_size()
# ---------------------------------------------------------------------------
# Signature (risk/manager.py:31):
#     calculate_position_size(account_balance, entry, sl, symbol_info)
# XAUUSD: point=0.01, 20 pips ≈ 0.20 price distance.

_XAUUSD_INFO = {
    "point": 0.01,
    "trade_tick_value": 1.0,
    "volume_min": 0.01,
    "volume_max": 100.0,
    "volume_step": 0.01,
}


def test_calculate_position_size_returns_positive_lot():
    rm = RiskManagement(RiskConfig(risk_per_trade=1.0))
    lot = rm.calculate_position_size(
        account_balance=10000.0,
        entry=2000.00,
        sl=1999.80,  # 20 pips for XAUUSD (point=0.01)
        symbol_info=_XAUUSD_INFO,
    )
    assert lot > 0
    assert isinstance(lot, float)


def test_calculate_position_size_zero_sl_returns_volume_min():
    # Documented behavior (manager.py:44-45): sl_distance<=0 → return volume_min.
    rm = RiskManagement(RiskConfig(risk_per_trade=1.0))
    lot = rm.calculate_position_size(
        account_balance=10000.0,
        entry=2000.00,
        sl=2000.00,            # zero distance
        symbol_info=_XAUUSD_INFO,
    )
    assert lot == _XAUUSD_INFO["volume_min"]


def test_calculate_position_size_wider_sl_yields_smaller_lot():
    rm = RiskManagement(RiskConfig(risk_per_trade=1.0))
    narrow = rm.calculate_position_size(10000.0, 2000.0, 1999.80, _XAUUSD_INFO)
    wide   = rm.calculate_position_size(10000.0, 2000.0, 1999.00, _XAUUSD_INFO)
    assert wide < narrow, f"wide SL ({wide}) should produce smaller lot than narrow ({narrow})"


# ---------------------------------------------------------------------------
# Category 3 — TradingAIBrain auto-approves with no API key
# ---------------------------------------------------------------------------
# Critical safety check: if claude_api_key="", the bot must keep running with
# a soft approval, NEVER attempt a network call.


async def test_ai_brain_auto_approves_when_no_api_key():
    brain = TradingAIBrain(api_key="", min_confidence=0.7)
    assert brain._enabled is False
    assert brain.client is None

    signal = TradeSignal(
        symbol="XAUUSD", direction="buy", timeframe="H1",
        entry=2000.0, sl=1990.0, tp1=2020.0, rr_ratio=2.0,
    )
    decision = await brain.validate_signal(signal, market_data={})
    assert isinstance(decision, AIDecision)
    assert decision.approved is True
    assert decision.confidence >= 0.70


async def test_ai_brain_returns_warnings_when_disabled():
    brain = TradingAIBrain(api_key="")
    signal = TradeSignal(
        symbol="XAUUSD", direction="sell", timeframe="H1",
        entry=2000.0, sl=2010.0, tp1=1980.0, rr_ratio=2.0,
    )
    decision = await brain.validate_signal(signal, market_data={})
    assert "AI validation skipped" in decision.warnings


# ---------------------------------------------------------------------------
# Category 4 — MT5Connector sim mode
# ---------------------------------------------------------------------------
# When MetaTrader5 isn't installed (CI / non-Windows), MT5Connector flips
# _sim_mode=True automatically. We force it on regardless to keep tests
# deterministic.


def _force_sim_connector() -> MT5Connector:
    c = MT5Connector()
    c._sim_mode = True
    # connect() in sim mode populates account_info and sets connected=True
    c.connect(login=999, password="x", server="DemoServer")
    return c


def test_mt5_connector_sim_is_connected_returns_true():
    c = _force_sim_connector()
    assert c.is_connected() is True


def test_mt5_connector_sim_open_positions_empty():
    c = _force_sim_connector()
    assert c.get_open_positions() == []


def test_mt5_connector_sim_refresh_account_has_balance():
    c = _force_sim_connector()
    info = c.refresh_account()
    assert isinstance(info, dict)
    assert info.get("balance", 0) > 0
    assert info.get("currency") == "USD"


# ---------------------------------------------------------------------------
# Category 5 — TradingConfig env validation
# ---------------------------------------------------------------------------
# F1-2: silent fallback removed. Bad env value must abort startup.


def test_config_rejects_non_numeric_risk(clean_config_env):
    with pytest.raises(ValidationError):
        TradingConfig(_env_file=None, risk_per_trade="abc")


def test_config_rejects_max_positions_zero(clean_config_env):
    # Field constraint: max_positions ge=1
    with pytest.raises(ValidationError):
        TradingConfig(_env_file=None, max_positions=0)


def test_config_rejects_risk_above_daily_max(clean_config_env):
    # Cross-field invariant: daily_max_risk >= risk_per_trade
    with pytest.raises(ValidationError):
        TradingConfig(_env_file=None, daily_max_risk=2.0, risk_per_trade=3.0)


def test_config_defaults_pass_validation(clean_config_env):
    cfg = TradingConfig(_env_file=None)
    assert 0 < cfg.risk_per_trade <= 5.0
    assert cfg.daily_max_risk >= cfg.risk_per_trade
    assert cfg.max_drawdown >= cfg.daily_max_risk
    assert cfg.symbol == "XAUUSD"


# ---------------------------------------------------------------------------
# Category 6 — State persistence save/load round-trip
# ---------------------------------------------------------------------------
# F0-1: restart safety. Trade meta must survive an in-place crash.


def test_persistence_save_load_round_trip(tmp_path):
    meta = {
        12345: {"entry": 2000.0, "sl": 1990.0, "lot": 0.10, "direction": "buy"},
        67890: {"entry": 2050.5, "sl": 2060.0, "lot": 0.05, "direction": "sell"},
    }
    save_trade_meta(meta, state_dir=tmp_path)
    loaded = load_trade_meta(state_dir=tmp_path)
    assert loaded == meta  # int ticket keys preserved by load_trade_meta


def test_persistence_datetime_serializes_to_iso(tmp_path):
    # persistence._serialize_value() converts datetime -> ISO string.
    # _deserialize is currently a passthrough, so the round-trip yields
    # str on the load side (documented in persistence.py docstring).
    ts = datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)
    meta = {111: {"entry": 2000.0, "opened_at": ts}}
    save_trade_meta(meta, state_dir=tmp_path)
    loaded = load_trade_meta(state_dir=tmp_path)
    assert 111 in loaded
    assert loaded[111]["opened_at"] == ts.isoformat()


def test_persistence_load_missing_file_returns_empty(tmp_path):
    # No file written → load returns {} (no crash).
    out = load_trade_meta(state_dir=tmp_path / "nonexistent_subdir")
    assert out == {}


# ---------------------------------------------------------------------------
# Bonus — F1-1 propose_adjustment round-trip
# ---------------------------------------------------------------------------
# tmp_state_dir fixture sets OPENCLAW_STATE_DIR so state_db.get_db_path()
# resolves into a temp folder. init_db creates the schema.


def test_db_propose_adjustment_round_trip(tmp_state_dir):
    state_db.init_db()
    adj_id = state_db.propose_adjustment(
        param="tp1_pips",
        old_value=60.0,
        new_value=72.0,
        reason="smoke test",
    )
    assert isinstance(adj_id, str) and len(adj_id) == 36  # UUID4

    row = state_db.get_pending(adj_id)
    assert row is not None
    assert row["param"] == "tp1_pips"
    assert row["new_value"] == 72.0
    assert row["status"] == "pending"


def test_db_approve_adjustment_inserts_active(tmp_state_dir):
    state_db.init_db()
    adj_id = state_db.propose_adjustment(
        param="risk_per_trade",
        old_value=1.0,
        new_value=0.75,
        reason="dd guard",
    )
    active = state_db.approve_adjustment(adj_id, applied_by="smoke")
    assert active is not None
    assert active["param"] == "risk_per_trade"
    # get_active_value reads the latest applied
    assert state_db.get_active_value("risk_per_trade") == 0.75


# ---------------------------------------------------------------------------
# Bonus — F1-4 HealthState default status
# ---------------------------------------------------------------------------


def test_health_state_initial_status_is_initializing():
    state = HealthState()
    assert state.last_tick_at is None
    assert _compute_status(state) == "initializing"
    assert state.mt5_connected is False
    assert state.open_positions == 0
    assert state.is_paused is False
