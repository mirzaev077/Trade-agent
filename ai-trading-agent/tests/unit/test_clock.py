"""VirtualClock uchun unit testlar."""

from datetime import datetime, timedelta

import pytest

from apps.api.src.agents.trader.core.clock import BacktestComplete, VirtualClock


# ---------------------------------------------------------------------------
# Boshlang'ich holat testlari
# ---------------------------------------------------------------------------


class TestVirtualClockInit:
    """VirtualClock yaratilganda boshlang'ich holatni tekshiradi."""

    def test_initial_current_equals_start(self) -> None:
        start = datetime(2024, 1, 1)
        end = datetime(2024, 12, 31)
        clock = VirtualClock(start, end)
        assert clock.current == start

    def test_now_returns_current(self) -> None:
        start = datetime(2024, 1, 1)
        end = datetime(2024, 12, 31)
        clock = VirtualClock(start, end)
        assert clock.now() == start

    def test_start_and_end_stored(self) -> None:
        start = datetime(2024, 1, 1)
        end = datetime(2024, 6, 15)
        clock = VirtualClock(start, end)
        assert clock.start == start
        assert clock.end == end

    def test_listeners_empty_at_init(self) -> None:
        clock = VirtualClock(datetime(2024, 1, 1), datetime(2024, 12, 31))
        assert clock._listeners == []


# ---------------------------------------------------------------------------
# Vaqtni oldinga surish testlari (advance)
# ---------------------------------------------------------------------------


class TestAdvance:
    """advance() metodini tekshiradi."""

    def test_advance_moves_time_forward(self) -> None:
        start = datetime(2024, 1, 1)
        clock = VirtualClock(start, datetime(2024, 12, 31))
        clock.advance(timedelta(hours=1))
        assert clock.now() == start + timedelta(hours=1)

    def test_advance_multiple_times(self) -> None:
        start = datetime(2024, 1, 1)
        clock = VirtualClock(start, datetime(2024, 12, 31))
        clock.advance(timedelta(days=1))
        clock.advance(timedelta(days=2))
        clock.advance(timedelta(hours=6))
        assert clock.now() == start + timedelta(days=3, hours=6)

    def test_advance_by_zero_delta(self) -> None:
        start = datetime(2024, 1, 1)
        clock = VirtualClock(start, datetime(2024, 12, 31))
        clock.advance(timedelta(0))
        assert clock.now() == start

    def test_advance_exactly_to_end_does_not_raise(self) -> None:
        start = datetime(2024, 1, 1)
        end = datetime(2024, 1, 2)
        clock = VirtualClock(start, end)
        # current == end bo'lganda xato chiqmasligi kerak
        clock.advance(timedelta(days=1))
        assert clock.now() == end

    def test_advance_past_end_raises_backtest_complete(self) -> None:
        start = datetime(2024, 1, 1)
        end = datetime(2024, 1, 2)
        clock = VirtualClock(start, end)
        with pytest.raises(BacktestComplete):
            clock.advance(timedelta(days=2))

    def test_advance_one_tick_past_end_raises(self) -> None:
        start = datetime(2024, 1, 1)
        end = datetime(2024, 1, 1, 0, 0, 1)
        clock = VirtualClock(start, end)
        with pytest.raises(BacktestComplete):
            clock.advance(timedelta(seconds=2))


# ---------------------------------------------------------------------------
# Aniq vaqtga o'tkazish testlari (advance_to)
# ---------------------------------------------------------------------------


class TestAdvanceTo:
    """advance_to() metodini tekshiradi."""

    def test_advance_to_future_time(self) -> None:
        start = datetime(2024, 1, 1)
        target = datetime(2024, 6, 15, 12, 0)
        clock = VirtualClock(start, datetime(2024, 12, 31))
        clock.advance_to(target)
        assert clock.now() == target

    def test_advance_to_same_time_allowed(self) -> None:
        start = datetime(2024, 1, 1)
        clock = VirtualClock(start, datetime(2024, 12, 31))
        # O'ziga o'tkazish (target == current) xato bermasligi kerak
        clock.advance_to(start)
        assert clock.now() == start

    def test_advance_to_past_raises_assertion(self) -> None:
        start = datetime(2024, 6, 1)
        clock = VirtualClock(start, datetime(2024, 12, 31))
        past = datetime(2024, 1, 1)
        with pytest.raises(AssertionError, match="Vaqt orqaga harakat qilolmaydi!"):
            clock.advance_to(past)

    def test_advance_to_backward_by_one_second_raises(self) -> None:
        start = datetime(2024, 6, 1, 12, 0, 0)
        clock = VirtualClock(start, datetime(2024, 12, 31))
        backward = datetime(2024, 6, 1, 11, 59, 59)
        with pytest.raises(AssertionError):
            clock.advance_to(backward)

    def test_advance_to_after_advance(self) -> None:
        start = datetime(2024, 1, 1)
        clock = VirtualClock(start, datetime(2024, 12, 31))
        clock.advance(timedelta(days=10))
        target = datetime(2024, 3, 1)
        clock.advance_to(target)
        assert clock.now() == target

    def test_advance_to_backward_after_advance_raises(self) -> None:
        start = datetime(2024, 1, 1)
        clock = VirtualClock(start, datetime(2024, 12, 31))
        clock.advance(timedelta(days=30))
        # start'ga qaytmoqchi bo'lish
        with pytest.raises(AssertionError):
            clock.advance_to(start)


# ---------------------------------------------------------------------------
# is_finished testlari
# ---------------------------------------------------------------------------


class TestIsFinished:
    """is_finished() metodini tekshiradi."""

    def test_not_finished_at_start(self) -> None:
        clock = VirtualClock(datetime(2024, 1, 1), datetime(2024, 12, 31))
        assert clock.is_finished() is False

    def test_not_finished_before_end(self) -> None:
        clock = VirtualClock(datetime(2024, 1, 1), datetime(2024, 12, 31))
        clock.advance(timedelta(days=100))
        assert clock.is_finished() is False

    def test_finished_at_end(self) -> None:
        start = datetime(2024, 1, 1)
        end = datetime(2024, 1, 2)
        clock = VirtualClock(start, end)
        clock.advance_to(end)
        assert clock.is_finished() is True

    def test_finished_when_current_equals_end(self) -> None:
        start = datetime(2024, 1, 1)
        end = datetime(2024, 1, 1, 0, 1, 0)
        clock = VirtualClock(start, end)
        clock.advance(timedelta(minutes=1))
        assert clock.is_finished() is True

    def test_start_equals_end_is_finished(self) -> None:
        moment = datetime(2024, 6, 1)
        clock = VirtualClock(moment, moment)
        assert clock.is_finished() is True


# ---------------------------------------------------------------------------
# Listener mexanizmi testlari
# ---------------------------------------------------------------------------


class TestListeners:
    """on_tick() va notify_listeners() metodlarini tekshiradi."""

    def test_on_tick_registers_listener(self) -> None:
        clock = VirtualClock(datetime(2024, 1, 1), datetime(2024, 12, 31))
        called: list[datetime] = []
        clock.on_tick(lambda t: called.append(t))
        assert len(clock._listeners) == 1

    def test_listener_called_on_advance(self) -> None:
        start = datetime(2024, 1, 1)
        clock = VirtualClock(start, datetime(2024, 12, 31))
        called: list[datetime] = []
        clock.on_tick(lambda t: called.append(t))
        clock.advance(timedelta(hours=1))
        assert len(called) == 1
        assert called[0] == start + timedelta(hours=1)

    def test_listener_called_on_advance_to(self) -> None:
        start = datetime(2024, 1, 1)
        target = datetime(2024, 3, 15)
        clock = VirtualClock(start, datetime(2024, 12, 31))
        called: list[datetime] = []
        clock.on_tick(lambda t: called.append(t))
        clock.advance_to(target)
        assert len(called) == 1
        assert called[0] == target

    def test_multiple_listeners_all_called(self) -> None:
        clock = VirtualClock(datetime(2024, 1, 1), datetime(2024, 12, 31))
        results_a: list[datetime] = []
        results_b: list[datetime] = []
        results_c: list[datetime] = []
        clock.on_tick(lambda t: results_a.append(t))
        clock.on_tick(lambda t: results_b.append(t))
        clock.on_tick(lambda t: results_c.append(t))
        clock.advance(timedelta(days=1))
        assert len(results_a) == 1
        assert len(results_b) == 1
        assert len(results_c) == 1

    def test_listener_called_each_advance(self) -> None:
        clock = VirtualClock(datetime(2024, 1, 1), datetime(2024, 12, 31))
        called: list[datetime] = []
        clock.on_tick(lambda t: called.append(t))
        clock.advance(timedelta(hours=1))
        clock.advance(timedelta(hours=2))
        clock.advance(timedelta(hours=3))
        assert len(called) == 3

    def test_notify_listeners_manually(self) -> None:
        clock = VirtualClock(datetime(2024, 1, 1), datetime(2024, 12, 31))
        called: list[datetime] = []
        clock.on_tick(lambda t: called.append(t))
        clock.notify_listeners()
        assert len(called) == 1
        assert called[0] == datetime(2024, 1, 1)

    def test_no_listeners_no_error(self) -> None:
        clock = VirtualClock(datetime(2024, 1, 1), datetime(2024, 12, 31))
        # Listener yo'q — xato chiqmasligi kerak
        clock.advance(timedelta(hours=1))

    def test_listeners_not_called_when_advance_raises(self) -> None:
        start = datetime(2024, 1, 1)
        end = datetime(2024, 1, 1, 0, 0, 1)
        clock = VirtualClock(start, end)
        called: list[datetime] = []
        clock.on_tick(lambda t: called.append(t))
        with pytest.raises(BacktestComplete):
            clock.advance(timedelta(seconds=5))
        # BacktestComplete chiqsa, listener chaqirilmasligi kerak
        assert len(called) == 0


# ---------------------------------------------------------------------------
# BacktestComplete exception testlari
# ---------------------------------------------------------------------------


class TestBacktestComplete:
    """BacktestComplete exception'ni tekshiradi."""

    def test_is_exception(self) -> None:
        assert issubclass(BacktestComplete, Exception)

    def test_can_be_raised_and_caught(self) -> None:
        with pytest.raises(BacktestComplete):
            raise BacktestComplete()

    def test_advance_raises_it(self) -> None:
        clock = VirtualClock(datetime(2024, 1, 1), datetime(2024, 1, 1))
        with pytest.raises(BacktestComplete):
            clock.advance(timedelta(seconds=1))


# F2-1 extensions

def test_real_clock_returns_aware_utc():
    from apps.api.src.agents.trader.core.clock import RealClock
    rc = RealClock()
    now = rc.now()
    assert now.tzinfo is not None
    assert now.utcoffset().total_seconds() == 0  # UTC


def test_get_clock_defaults_to_real_clock():
    from apps.api.src.agents.trader.core.clock import get_clock, RealClock, set_clock
    set_clock(None)  # reset
    assert isinstance(get_clock(), RealClock)


def test_set_clock_swaps_global():
    from apps.api.src.agents.trader.core.clock import (
        get_clock, set_clock, VirtualClock, RealClock,
    )
    from datetime import datetime, timezone, timedelta
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end   = start + timedelta(days=1)
    vc = VirtualClock(start=start, end=end)
    set_clock(vc)
    assert get_clock() is vc
    # cleanup
    set_clock(None)
    assert isinstance(get_clock(), RealClock)


def test_set_clock_none_restores_real():
    from apps.api.src.agents.trader.core.clock import (
        get_clock, set_clock, VirtualClock, RealClock,
    )
    from datetime import datetime, timezone, timedelta
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    vc = VirtualClock(start=start, end=start + timedelta(days=1))
    set_clock(vc)
    set_clock(None)
    assert isinstance(get_clock(), RealClock)
