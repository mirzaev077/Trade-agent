"""F4 scheduler — daily / weekly Telegram report firing.

Two layers:
  * pure predicates `_daily_report_due` / `_weekly_report_due` (timing + marker
    logic, no agent needed);
  * the async `_maybe_send_scheduled_reports` bound to a lightweight stub (it
    only touches a few attrs), with the report generators + telegram mocked, so
    we verify it fires once, sets its marker, and never lets a report error
    escape into the trading loop.

The reports run BEFORE the weekend guard, so Friday (weekday 4) must qualify —
the old Sunday block was dead code (the guard returns on weekends).
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import apps.api.src.agents.trader.agent as agent_mod
from apps.api.src.agents.trader.agent import (
    TraderAgent,
    _daily_report_due,
    _weekly_report_due,
)

# Known UTC weekdays in 2025 (June 1 2025 = Sunday)
WED = datetime(2025, 6, 11, 23, 0, tzinfo=timezone.utc)   # Wednesday 23:00
FRI = datetime(2025, 6, 13, 23, 0, tzinfo=timezone.utc)   # Friday 23:00
SAT = datetime(2025, 6, 14, 23, 0, tzinfo=timezone.utc)   # Saturday 23:00
SUN = datetime(2025, 6, 15, 23, 0, tzinfo=timezone.utc)   # Sunday 23:00


# ── pure predicates ───────────────────────────────────────────────────────────

class TestDailyDue:
    def test_not_due_before_23(self) -> None:
        assert not _daily_report_due(WED.replace(hour=22), "")

    def test_due_at_23_weekday(self) -> None:
        assert _daily_report_due(WED, "")

    def test_due_on_friday(self) -> None:
        assert _daily_report_due(FRI, "")

    def test_not_due_weekend(self) -> None:
        assert not _daily_report_due(SAT, "")
        assert not _daily_report_due(SUN, "")

    def test_marker_blocks_same_day(self) -> None:
        assert not _daily_report_due(WED, WED.date().isoformat())

    def test_new_day_fires_again(self) -> None:
        assert _daily_report_due(FRI, WED.date().isoformat())


class TestWeeklyDue:
    def test_due_friday_23(self) -> None:
        assert _weekly_report_due(FRI, -1)

    def test_not_due_other_weekday(self) -> None:
        assert not _weekly_report_due(WED, -1)

    def test_not_due_before_23(self) -> None:
        assert not _weekly_report_due(FRI.replace(hour=22), -1)

    def test_marker_blocks_same_week(self) -> None:
        assert not _weekly_report_due(FRI, FRI.isocalendar()[1])


# ── async sender ──────────────────────────────────────────────────────────────

def _stub(balance: float = 10_000.0):
    return SimpleNamespace(
        _starting_balance=balance,
        _last_daily_summary_day="",
        _last_weekly_report_week=-1,
        symbol="XAUUSD",
    )


def _wire(monkeypatch, now, *, daily=None, weekly=None):
    monkeypatch.setattr(agent_mod, "get_clock",
                        lambda: SimpleNamespace(now=lambda: now))
    send = AsyncMock()
    monkeypatch.setattr(agent_mod, "tg", SimpleNamespace(send=send))
    dgen = daily or MagicMock(return_value=SimpleNamespace(telegram_text="DAILY"))
    wgen = weekly or MagicMock(return_value=SimpleNamespace(telegram_text="WEEKLY"))
    monkeypatch.setattr(agent_mod._daily_summary, "generate", dgen)
    monkeypatch.setattr(agent_mod._weekly_report, "generate", wgen)
    return send, dgen, wgen


async def test_daily_fires_and_sets_marker(monkeypatch) -> None:
    send, dgen, wgen = _wire(monkeypatch, WED)
    s = _stub()
    await TraderAgent._maybe_send_scheduled_reports(s)
    dgen.assert_called_once()
    send.assert_awaited_once_with("DAILY")
    assert s._last_daily_summary_day == WED.date().isoformat()
    wgen.assert_not_called()  # Wednesday → no weekly


async def test_daily_not_re_sent_same_day(monkeypatch) -> None:
    send, dgen, _ = _wire(monkeypatch, WED)
    s = _stub()
    await TraderAgent._maybe_send_scheduled_reports(s)
    await TraderAgent._maybe_send_scheduled_reports(s)  # second tick same day
    dgen.assert_called_once()
    assert send.await_count == 1


async def test_friday_fires_both_daily_and_weekly(monkeypatch) -> None:
    send, dgen, wgen = _wire(monkeypatch, FRI)
    s = _stub()
    await TraderAgent._maybe_send_scheduled_reports(s)
    dgen.assert_called_once()
    wgen.assert_called_once()
    assert send.await_count == 2
    assert s._last_weekly_report_week == FRI.isocalendar()[1]


async def test_no_fire_when_balance_unset(monkeypatch) -> None:
    send, dgen, wgen = _wire(monkeypatch, WED)
    s = _stub(balance=0.0)
    await TraderAgent._maybe_send_scheduled_reports(s)
    dgen.assert_not_called()
    send.assert_not_awaited()


async def test_report_error_never_escapes(monkeypatch) -> None:
    boom = MagicMock(side_effect=RuntimeError("csv exploded"))
    send, dgen, wgen = _wire(monkeypatch, WED, daily=boom)
    s = _stub()
    # Must not raise — a report failure cannot break the trading loop.
    await TraderAgent._maybe_send_scheduled_reports(s)
    boom.assert_called_once()
    send.assert_not_awaited()
    # Marker still advanced → no retry-spam within the day.
    assert s._last_daily_summary_day == WED.date().isoformat()
