"""
F3 acceptance: Telegram inbound commands — dispatcher + 4 handler.

Tasks.md F3-3 oxirgi qatorida ko'rsatilgan:
    "Telegram bot'ga /status, /positions, /pause, /resume commandlari qo'shish"

Test guruhlari:
  • dispatcher: parse_command, auth (chat_id whitelist), unknown command,
    handler exception → friendly error
  • /status: format, paused/running, balance/equity, WR
  • /positions: bo'sh, bir nechta, MT5 xatolik
  • /pause + /resume: state mutation, idempotency
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from apps.api.src.agents.trader.utils import telegram_bot as tg
from apps.api.src.agents.trader.utils.telegram_commands import TraderCommands


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_agent():
    """Minimal TraderAgent stub bilan TraderCommands handler'lari ishlasin."""
    mt5 = MagicMock(name="MT5Connector")
    mt5.refresh_account.return_value = {
        "balance": 10000.0,
        "equity": 10050.0,
    }
    mt5.get_open_positions.return_value = []

    hs = SimpleNamespace(
        started_at=datetime.now(timezone.utc) - timedelta(minutes=42),
    )

    return SimpleNamespace(
        mt5=mt5,
        symbol="XAUUSD",
        _mode="FLOW",
        _pause_until=None,
        health_state=hs,
        _starting_balance=10000.0,
        _wins=3,
        _losses=2,
        _pending_zones={"id1": {}, "id2": {}},
    )


@pytest.fixture
def tg_chat_setup(monkeypatch):
    """Telegram modulida chat_id ni testlash uchun o'rnatamiz."""
    monkeypatch.setattr(tg, "_CHAT_ID", "12345", raising=False)
    # Har test toza registry bilan boshlasin
    monkeypatch.setattr(tg, "_HANDLERS", {}, raising=False)
    yield
    monkeypatch.setattr(tg, "_HANDLERS", {}, raising=False)


# ── Dispatcher ────────────────────────────────────────────────────────────────


def test_parse_command_extracts_cmd_and_args():
    assert tg._parse_command("/status") == ("status", "")
    assert tg._parse_command("/pause now") == ("pause", "now")
    assert tg._parse_command("/cmd@OpenClawBot foo bar") == ("cmd", "foo bar")
    assert tg._parse_command("plain text") is None
    assert tg._parse_command("/") is None
    assert tg._parse_command("") is None


def test_dispatch_rejects_unauthorized_chat(tg_chat_setup):
    tg.register_command("status", lambda args: "OK")
    # Wrong chat_id → None (silent reject)
    assert tg.dispatch_command("/status", from_chat_id="99999") is None
    # Correct chat_id → reply
    assert tg.dispatch_command("/status", from_chat_id="12345") == "OK"
    # Int chat_id ham ishlashi kerak
    assert tg.dispatch_command("/status", from_chat_id=12345) == "OK"


def test_dispatch_unknown_command_returns_help(tg_chat_setup):
    tg.register_command("status", lambda args: "OK")
    reply = tg.dispatch_command("/zzz", from_chat_id="12345")
    assert reply is not None
    assert "Noma'lum" in reply
    assert "/status" in reply  # available list


def test_dispatch_handler_exception_returns_error(tg_chat_setup):
    def bad_handler(args: str) -> str:
        raise ValueError("oh no")
    tg.register_command("crash", bad_handler)
    reply = tg.dispatch_command("/crash", from_chat_id="12345")
    assert reply is not None
    assert "xato" in reply.lower()


def test_register_and_list_commands(tg_chat_setup):
    tg.register_command("foo", lambda a: "")
    tg.register_command("BAR", lambda a: "")  # case-insensitive
    cmds = tg.list_commands()
    assert "foo" in cmds
    assert "bar" in cmds
    tg.unregister_command("foo")
    assert "foo" not in tg.list_commands()


def test_register_all_adds_four_commands(tg_chat_setup, fake_agent):
    TraderCommands(fake_agent).register_all(bot_module=tg)
    cmds = tg.list_commands()
    assert set(cmds) >= {"status", "positions", "pause", "resume"}


# ── /status ───────────────────────────────────────────────────────────────────


def test_status_running_state(fake_agent):
    out = TraderCommands(fake_agent).cmd_status("")
    assert "XAUUSD" in out
    assert "[FLOW]" in out
    assert "RUNNING" in out
    assert "10000.00" in out
    assert "10050.00" in out
    assert "WR" in out or "wr" in out.lower() or "60%" in out  # 3/(3+2)=60%
    assert "60%" in out
    assert "Uptime" in out


def test_status_paused_manual(fake_agent):
    fake_agent._pause_until = datetime.now(timezone.utc) + timedelta(days=100)
    out = TraderCommands(fake_agent).cmd_status("")
    assert "PAUSED" in out
    assert "MANUAL" in out


def test_status_paused_auto(fake_agent):
    fake_agent._pause_until = datetime.now(timezone.utc) + timedelta(minutes=30)
    out = TraderCommands(fake_agent).cmd_status("")
    assert "PAUSED" in out
    assert "AUTO" in out


def test_status_no_account_no_crash(fake_agent):
    fake_agent.mt5.refresh_account.side_effect = RuntimeError("MT5 down")
    out = TraderCommands(fake_agent).cmd_status("")
    # Crash bermaslik kerak, balance 0 ko'rinadi
    assert "XAUUSD" in out
    assert "0.00" in out


# ── /positions ────────────────────────────────────────────────────────────────


def test_positions_empty(fake_agent):
    out = TraderCommands(fake_agent).cmd_positions("")
    assert "yo'q" in out.lower() or "no open" in out.lower() or "📭" in out


def test_positions_lists_open(fake_agent):
    fake_agent.mt5.get_open_positions.return_value = [
        {
            "ticket": 12345,
            "symbol": "XAUUSD",
            "type": 0,  # buy
            "volume": 0.05,
            "price_open": 2050.5,
            "sl": 2045.0,
            "tp": 2065.0,
            "profit": 12.30,
        },
        {
            "ticket": 12346,
            "symbol": "XAUUSD",
            "type": 1,  # sell
            "volume": 0.03,
            "price_open": 2070.0,
            "sl": 2075.0,
            "tp": 2060.0,
            "profit": -5.40,
        },
    ]
    out = TraderCommands(fake_agent).cmd_positions("")
    assert "12345" in out
    assert "12346" in out
    assert "BUY" in out
    assert "SELL" in out
    assert "2050.50" in out
    assert "2070.00" in out
    assert "+12.30" in out
    assert "-5.40" in out


def test_positions_truncates_above_10(fake_agent):
    fake_agent.mt5.get_open_positions.return_value = [
        {"ticket": i, "symbol": "XAUUSD", "type": 0, "volume": 0.01,
         "price_open": 2000.0, "sl": 0, "tp": 0, "profit": 0.0}
        for i in range(15)
    ]
    out = TraderCommands(fake_agent).cmd_positions("")
    assert "yana 5 ta" in out or "yana 5" in out


def test_positions_mt5_failure(fake_agent):
    fake_agent.mt5.get_open_positions.side_effect = RuntimeError("conn lost")
    out = TraderCommands(fake_agent).cmd_positions("")
    assert "xato" in out.lower()


# ── /pause + /resume ──────────────────────────────────────────────────────────


def test_pause_sets_pause_until_far_future(fake_agent):
    assert fake_agent._pause_until is None
    out = TraderCommands(fake_agent).cmd_pause("")
    assert "MANUAL" in out or "PAUSE" in out
    assert fake_agent._pause_until is not None
    # Manual pause = 10 yil oldinda bo'lishi kerak
    delta = fake_agent._pause_until - datetime.now(timezone.utc)
    assert delta > timedelta(days=30)


def test_pause_idempotent_when_already_paused(fake_agent):
    fake_agent._pause_until = datetime.now(timezone.utc) + timedelta(days=100)
    out = TraderCommands(fake_agent).cmd_pause("")
    assert "Allaqachon" in out or "MANUAL" in out


def test_resume_clears_pause_until(fake_agent):
    fake_agent._pause_until = datetime.now(timezone.utc) + timedelta(days=100)
    out = TraderCommands(fake_agent).cmd_resume("")
    assert "RESUMED" in out
    assert fake_agent._pause_until is None


def test_resume_when_not_paused(fake_agent):
    out = TraderCommands(fake_agent).cmd_resume("")
    assert "emas" in out.lower() or "not paused" in out.lower()


def test_resume_after_auto_pause_mentions_auto(fake_agent):
    fake_agent._pause_until = datetime.now(timezone.utc) + timedelta(minutes=15)
    out = TraderCommands(fake_agent).cmd_resume("")
    assert "RESUMED" in out
    assert "AUTO" in out  # avval auto-pause edi
