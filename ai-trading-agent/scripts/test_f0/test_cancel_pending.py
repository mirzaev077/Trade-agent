"""F0-4 test: agent._cancel_all_pending_orders — News blackout pending cancel"""
import sys
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from apps.api.src.agents.trader.agent import TraderAgent

PASSED = 0
FAILED = 0
OPENCLAW_MAGIC = 20240101


def test(name):
    def decorator(fn):
        global PASSED, FAILED
        try:
            fn()
            PASSED += 1
            print(f"  [PASS] {name}")
        except AssertionError as e:
            FAILED += 1
            print(f"  [FAIL] {name}: {e}")
        except Exception as e:
            FAILED += 1
            print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
        return fn
    return decorator


class MockMT5Connector:
    """Faqat get_pending_orders + cancel_pending_order kerak"""
    def __init__(self, pending=None, fail_cancel=False, raise_on_get=False):
        self._pending = pending if pending is not None else []
        self._fail_cancel = fail_cancel
        self._raise_on_get = raise_on_get
        self.cancelled = []  # cancel chaqirilgan ticket'lar
        self.cancel_calls = 0

    def get_pending_orders(self, symbol=None):
        if self._raise_on_get:
            raise RuntimeError("simulated MT5 failure")
        return self._pending

    def cancel_pending_order(self, ticket):
        self.cancel_calls += 1
        if self._fail_cancel:
            return False
        self.cancelled.append(ticket)
        return True


def make_stub(mt5_mock):
    """TraderAgent o'rnida ishlash uchun minimal stub.
    `_cancel_all_pending_orders` faqat self.mt5, self.symbol, self._pending_zones'ga tegadi.
    """
    return SimpleNamespace(
        mt5=mt5_mock,
        symbol="XAUUSD",
        _pending_zones={},
    )


print("\n=== F0-4: cancel pending tests ===")


@test("Method TraderAgent klassida mavjud")
def _():
    assert hasattr(TraderAgent, "_cancel_all_pending_orders"), \
        "_cancel_all_pending_orders TraderAgent'da yo'q"
    assert callable(getattr(TraderAgent, "_cancel_all_pending_orders"))


@test("3 ta pending (2 OpenClaw + 1 boshqa magic) -> 2 ta cancel")
def _():
    pending = [
        {"ticket": 333, "magic": OPENCLAW_MAGIC, "price_open": 2050.0, "symbol": "XAUUSD"},
        {"ticket": 444, "magic": OPENCLAW_MAGIC, "price_open": 2055.0, "symbol": "XAUUSD"},
        {"ticket": 555, "magic": 99999, "price_open": 2060.0, "symbol": "XAUUSD"},
    ]
    mt5_mock = MockMT5Connector(pending=pending)
    stub = make_stub(mt5_mock)
    result = TraderAgent._cancel_all_pending_orders(stub, reason="TEST")
    assert result == 2, f"2 ta cancel kutilgan, lekin {result}"
    assert sorted(mt5_mock.cancelled) == [333, 444], \
        f"333 va 444 cancel bo'lishi kerak, lekin {mt5_mock.cancelled}"
    assert 555 not in mt5_mock.cancelled, "555 (boshqa magic) cancel bo'lmasligi kerak"


@test("Bo'sh pending ro'yxat -> 0 cancelled")
def _():
    mt5_mock = MockMT5Connector(pending=[])
    stub = make_stub(mt5_mock)
    result = TraderAgent._cancel_all_pending_orders(stub, reason="TEST")
    assert result == 0
    assert mt5_mock.cancel_calls == 0, "cancel chaqirilmasligi kerak"


@test("Faqat boshqa magic pending'lar bor -> 0 cancelled")
def _():
    pending = [
        {"ticket": 1, "magic": 11111, "price_open": 2050.0, "symbol": "XAUUSD"},
        {"ticket": 2, "magic": 22222, "price_open": 2055.0, "symbol": "XAUUSD"},
    ]
    mt5_mock = MockMT5Connector(pending=pending)
    stub = make_stub(mt5_mock)
    result = TraderAgent._cancel_all_pending_orders(stub, reason="TEST")
    assert result == 0
    assert mt5_mock.cancel_calls == 0


@test("cancel False qaytarsa, counter oshmaydi")
def _():
    pending = [
        {"ticket": 100, "magic": OPENCLAW_MAGIC, "price_open": 2050.0, "symbol": "XAUUSD"},
        {"ticket": 200, "magic": OPENCLAW_MAGIC, "price_open": 2055.0, "symbol": "XAUUSD"},
    ]
    mt5_mock = MockMT5Connector(pending=pending, fail_cancel=True)
    stub = make_stub(mt5_mock)
    result = TraderAgent._cancel_all_pending_orders(stub, reason="TEST")
    assert result == 0, f"Hamma cancel False qaytarganda 0 kutilgan, lekin {result}"
    # Lekin cancel chaqirilgan bo'lishi kerak
    assert mt5_mock.cancel_calls == 2, f"2 ta cancel attempt kutilgan, lekin {mt5_mock.cancel_calls}"


@test("get_pending_orders exception'da 0 qaytaradi (crash yo'q)")
def _():
    mt5_mock = MockMT5Connector(raise_on_get=True)
    stub = make_stub(mt5_mock)
    result = TraderAgent._cancel_all_pending_orders(stub, reason="TEST")
    assert result == 0, f"Exception'da 0 kutilgan, lekin {result}"


@test("cancel_pending_order exception graceful (boshqalari davom etadi)")
def _():
    pending = [
        {"ticket": 1, "magic": OPENCLAW_MAGIC, "price_open": 2050.0, "symbol": "XAUUSD"},
        {"ticket": 2, "magic": OPENCLAW_MAGIC, "price_open": 2055.0, "symbol": "XAUUSD"},
        {"ticket": 3, "magic": OPENCLAW_MAGIC, "price_open": 2060.0, "symbol": "XAUUSD"},
    ]

    class FlakyMock(MockMT5Connector):
        def cancel_pending_order(self, ticket):
            self.cancel_calls += 1
            if ticket == 2:
                raise RuntimeError("simulated cancel failure")
            self.cancelled.append(ticket)
            return True

    mt5_mock = FlakyMock(pending=pending)
    stub = make_stub(mt5_mock)
    result = TraderAgent._cancel_all_pending_orders(stub, reason="TEST")
    # 2 ta success (1 va 3), 2-chi exception
    assert result == 2, f"2 ta success kutilgan (1 va 3), lekin {result}"
    assert sorted(mt5_mock.cancelled) == [1, 3]


@test("_pending_zones tracking'dan cancel qilingan ticket olib tashlanadi")
def _():
    pending = [
        {"ticket": 777, "magic": OPENCLAW_MAGIC, "price_open": 2050.0, "symbol": "XAUUSD"},
    ]
    mt5_mock = MockMT5Connector(pending=pending)
    stub = make_stub(mt5_mock)
    # Pre-populate _pending_zones
    stub._pending_zones = {777: {"some": "data"}, 888: {"other": "data"}}
    result = TraderAgent._cancel_all_pending_orders(stub, reason="TEST")
    assert result == 1
    assert 777 not in stub._pending_zones, "777 _pending_zones'dan olib tashlanmadi"
    assert 888 in stub._pending_zones, "888 saqlanib qolishi kerak edi"


@test("ticket=0 bo'lgan order'ni o'tkazib yuboradi")
def _():
    pending = [
        {"ticket": 0, "magic": OPENCLAW_MAGIC, "price_open": 2050.0, "symbol": "XAUUSD"},
        {"ticket": 999, "magic": OPENCLAW_MAGIC, "price_open": 2055.0, "symbol": "XAUUSD"},
    ]
    mt5_mock = MockMT5Connector(pending=pending)
    stub = make_stub(mt5_mock)
    result = TraderAgent._cancel_all_pending_orders(stub, reason="TEST")
    # Faqat 999 cancel bo'lishi kerak
    assert result == 1, f"Faqat 1 ta cancel (ticket!=0), lekin {result}"
    assert mt5_mock.cancelled == [999]


@test("Default reason='NEWS_BLACKOUT' parametri ishlaydi")
def _():
    pending = [
        {"ticket": 111, "magic": OPENCLAW_MAGIC, "price_open": 2050.0, "symbol": "XAUUSD"},
    ]
    mt5_mock = MockMT5Connector(pending=pending)
    stub = make_stub(mt5_mock)
    # Reason argumentsiz chaqirish
    result = TraderAgent._cancel_all_pending_orders(stub)
    assert result == 1


print(f"\nResult: {PASSED} passed, {FAILED} failed")
sys.exit(0 if FAILED == 0 else 1)
