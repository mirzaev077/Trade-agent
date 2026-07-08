"""
F1-5: Shared pytest fixtures for unit tests.

Fixtures:
  • mock_mt5_connector          — MT5Connector MagicMock (connected, no positions)
  • sample_candles_xauusd_m15   — 200-bar synthetic XAUUSD M15 OHLCV DataFrame
  • frozen_now                  — freezegun fixed clock (2026-05-14 12:00 UTC)
  • clean_config_env            — config-related env vars cleared for test isolation
  • tmp_state_dir               — temp dir for state/db isolation (sets OPENCLAW_STATE_DIR)

Conventions:
  • Run pytest from `ai-trading-agent/` root.
  • pyproject.toml sets pythonpath = ["."], so `apps.api.src...` imports just work.
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest


# Some imports below need apps.api... — ensure pythonpath is set when running
# without pytest config (e.g., REPL debugging of fixtures).
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


_CONFIG_ENV_KEYS = {
    "MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER",
    "SYMBOL", "SCAN_INTERVAL",
    "RISK_PER_TRADE", "DAILY_MAX_RISK", "DAILY_PROFIT_TARGET",
    "MAX_POSITIONS", "MAX_TRADES_PER_DAY", "MAX_DRAWDOWN",
    "REGIME_ATR_THRESHOLD", "REGIME_ATR_PERIOD", "BLOCKED_SETUPS",
    "EXECUTION_MODE", "MAX_LOT_CAP",
    "CLAUDE_API_KEY", "MIN_AI_CONFIDENCE",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "AUTO_APPLY_LEARNING",
    "DATABASE_URL", "REDIS_URL", "SOCKET_URL",
}


@pytest.fixture
def mock_mt5_connector():
    """
    MagicMock'lashtirilgan MT5Connector. Default qiymatlar:
      • is_connected() → True
      • get_open_positions() → []
      • refresh_account() → balance=10000, equity=10000
      • find_symbol() → "XAUUSD"
      • _sim_mode = True

    Testlar bu fixture'ni kerak bo'lsa overridelashlari mumkin:
        mock_mt5_connector.get_open_positions.return_value = [{"ticket": 1, ...}]
    """
    m = MagicMock(name="MT5Connector")
    m.is_connected.return_value = True
    m.get_open_positions.return_value = []
    m.refresh_account.return_value = {
        "balance":   10000.0,
        "equity":    10000.0,
        "leverage":  100,
        "currency":  "USD",
        "margin":    0.0,
    }
    m.find_symbol.return_value = "XAUUSD"
    m.get_disconnect_duration_min.return_value = 0
    m.ensure_connected.return_value = True
    m._sim_mode = True
    m._last_recovery_downtime_min = 0
    return m


@pytest.fixture
def sample_candles_xauusd_m15():
    """
    200 bar (50 soat) sintetik XAUUSD M15 OHLCV DataFrame.

    Seed=42 — deterministik. Realistik price action: ~2000 atrofida walk,
    sigma=0.001 returns. Sufficient bars for any ICT analyzer (D1→M5).
    Columns: time, open, high, low, close, volume — agent.py kutgan format.
    """
    rng = np.random.default_rng(42)
    n = 200
    base = 2000.0
    returns = rng.normal(0, 0.0015, n)
    close = base * np.exp(np.cumsum(returns))

    body_size = np.abs(rng.normal(0, 0.0008, n))
    wick_up = np.abs(rng.normal(0, 0.0005, n))
    wick_dn = np.abs(rng.normal(0, 0.0005, n))

    open_ = np.roll(close, 1)
    open_[0] = base
    high = np.maximum(open_, close) * (1 + wick_up)
    low  = np.minimum(open_, close) * (1 - wick_dn)
    volume = rng.integers(100, 1000, n)

    end_time = pd.Timestamp("2026-05-14 12:00:00", tz="UTC")
    idx = pd.date_range(end=end_time, periods=n, freq="15min")

    return pd.DataFrame({
        "time":   idx,
        "open":   open_,
        "high":   high,
        "low":    low,
        "close":  close,
        "volume": volume,
    })


@pytest.fixture
def frozen_now():
    """
    freezegun bilan vaqtni qotirish. Fixed instant: 2026-05-14 12:00 UTC.
    Foydalanish:
        def test_x(frozen_now):
            assert datetime.utcnow() == datetime(2026, 5, 14, 12, 0)
    """
    from freezegun import freeze_time
    fixed = datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)
    with freeze_time(fixed):
        yield fixed


@pytest.fixture
def clean_config_env(monkeypatch):
    """
    TradingConfig env vars'ni o'chiradi. Testlar default qiymatlar bilan ishlasin.
    Real .env'ni o'qishni o'chirish uchun TradingConfig(_env_file=None) ishlatiladi.
    """
    for k in _CONFIG_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    yield


@pytest.fixture
def tmp_state_dir(monkeypatch):
    """
    Vaqtinchalik state dir — db.py va admin_cli.py shu yerga yozadi.
    OPENCLAW_STATE_DIR env'ni o'rnatadi (db.py shuni o'qiydi).
    """
    with tempfile.TemporaryDirectory(prefix="openclaw_test_") as tmp:
        monkeypatch.setenv("OPENCLAW_STATE_DIR", tmp)
        yield Path(tmp)
