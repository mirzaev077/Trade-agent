"""
VirtualClock — Backtest uchun yagona vaqt manbai.

LINT QOIDASI: `datetime.utcnow()` ishlatish TAQIQLANGAN.
Har bir agent faqat `clock.now()` orqali vaqtni olishi shart.
Bu look-ahead bias'ning birinchi himoyasi.
"""

from datetime import datetime, timedelta, timezone
from typing import Callable


class BacktestComplete(Exception):
    """Backtest tugadi — clock end vaqtidan o'tib ketdi."""


class VirtualClock:
    """
    Backtest uchun yagona vaqt manbai.

    Hech qachon `datetime.utcnow()` ishlatmang — har doim clock'dan so'rang.
    Bu look-ahead bias'ning birinchi himoyasi.

    Qoida: Har bir agent `clock.now()` ishlatishi shart.
    `datetime.utcnow()` — taqiqlangan import (lint qoidasi).
    """

    def __init__(self, start: datetime, end: datetime) -> None:
        """
        VirtualClock'ni boshlang'ich va tugash vaqtlari bilan yaratadi.

        :param start: Backtest boshlanish vaqti
        :param end: Backtest tugash vaqti
        """
        self.start = start
        self.end = end
        self.current = start
        self._listeners: list[Callable[[datetime], None]] = []

    def now(self) -> datetime:
        """Hozirgi simulyatsiya vaqtini qaytaradi."""
        return self.current

    def advance(self, delta: timedelta) -> None:
        """
        Vaqtni delta miqdoriga oldinga suradi.

        Agar current end'dan oshib ketsa, BacktestComplete xatosi chiqadi.

        :param delta: Vaqt oralig'i
        :raises BacktestComplete: current > end bo'lganda
        """
        self.current += delta
        if self.current > self.end:
            raise BacktestComplete()
        self.notify_listeners()

    def advance_to(self, target: datetime) -> None:
        """
        Vaqtni aniq target vaqtga o'tkazadi.

        Target hozirgi vaqtdan kichik bo'lsa, AssertionError chiqadi.

        :param target: Maqsad vaqt
        :raises AssertionError: target < current bo'lganda
        """
        assert target >= self.current, "Vaqt orqaga harakat qilolmaydi!"
        self.current = target
        self.notify_listeners()

    def is_finished(self) -> bool:
        """Backtest tugaganligini tekshiradi (current >= end)."""
        return self.current >= self.end

    def on_tick(self, callback: Callable[[datetime], None]) -> None:
        """
        Tick hodisasiga listener qo'shadi.

        Har safar vaqt o'zgarganda, barcha listener'lar chaqiriladi.

        :param callback: Chaqiriladigan funksiya, argument sifatida hozirgi vaqtni oladi
        """
        self._listeners.append(callback)

    def notify_listeners(self) -> None:
        """Barcha ro'yxatdagi listener'larni hozirgi vaqt bilan xabardor qiladi."""
        for listener in self._listeners:
            listener(self.current)


# ─────────────────────────────────────────────────────────────────
# F2-1 extension: Production clock + module-level singleton.
# ─────────────────────────────────────────────────────────────────
#
# Production code (agent.py, self_learner.py, ...) calls `get_clock().now()`
# instead of `datetime.utcnow()`. By default this resolves to RealClock
# (wall-clock time, timezone-aware UTC). Backtests swap in a VirtualClock
# via `set_clock(virtual_clock)`.


class RealClock:
    """Production wall-clock — wraps datetime.now(timezone.utc)."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


_clock: "RealClock | VirtualClock" = RealClock()


def get_clock() -> "RealClock | VirtualClock":
    """Return the current global clock. Used by all production code paths."""
    return _clock


def set_clock(clock) -> None:
    """Swap the global clock — used by backtest engine and tests.

    Pass `None` (or a fresh RealClock()) to restore production behavior.
    """
    global _clock
    _clock = clock if clock is not None else RealClock()
