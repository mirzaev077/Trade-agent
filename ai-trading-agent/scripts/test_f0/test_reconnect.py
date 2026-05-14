"""F0-2 test: mt5_connector.py — is_connected, ensure_connected, get_disconnect_duration_min"""
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from apps.api.src.agents.trader.mt5_connector import MT5Connector

PASSED = 0
FAILED = 0


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


print("\n=== F0-2: reconnect tests ===")


@test("is_connected() sim mode'da True qaytaradi")
def _():
    conn = MT5Connector()
    # __init__ da _sim_mode = not MT5_AVAILABLE — Windows'da MT5 mavjud bo'lishi mumkin
    # Test izolatsiyasi uchun majburiy sim_mode qilamiz
    conn._sim_mode = True
    assert conn.is_connected() is True, "Sim mode'da is_connected True bo'lishi kerak"


@test("ensure_connected() kreditialssiz False qaytaradi")
def _():
    conn = MT5Connector()
    conn._sim_mode = False  # real mode simulate
    # is_connected — False qaytadigan qilib qo'yamiz
    conn.is_connected = lambda: False
    # Kreditiallar yo'q -> ensure_connected darhol False
    conn._login = None
    conn._password = None
    conn._server = None
    result = conn.ensure_connected(max_retries=3)
    assert result is False, f"Kreditialssiz False kutilgan, lekin {result}"
    # _last_disconnect_at marker o'rnatildi
    assert conn._last_disconnect_at is not None


@test("ensure_connected() — connect 3 marta fail, keyin success (sleep mocked)")
def _():
    # time.sleep'ni mock qilamiz — test 60s kutmaslik uchun
    import apps.api.src.agents.trader.mt5_connector as mod
    original_sleep = mod.time.sleep
    sleep_calls = []
    mod.time.sleep = lambda x: sleep_calls.append(x)
    try:
        conn = MT5Connector()
        conn._sim_mode = False
        conn._login = 12345
        conn._password = "x"
        conn._server = "test-server"

        # is_connected: birinchi marta False (uzilgan), keyin connect dan keyin True
        call_count = {"n": 0}
        def fake_is_connected():
            call_count["n"] += 1
            # Mantiq: connect chaqirilmaguncha False
            # connect attempts >= 4 (3 fail + 4-chi success) bo'lganda True
            return conn._connect_attempts >= 4

        conn._connect_attempts = 0

        def fake_connect(login, password, server):
            conn._connect_attempts += 1
            if conn._connect_attempts <= 3:
                raise Exception(f"mock connect fail #{conn._connect_attempts}")
            conn.connected = True
            return {"login": login, "balance": 1000}

        conn.is_connected = fake_is_connected
        conn.connect = fake_connect

        result = conn.ensure_connected(max_retries=5, initial_backoff=2.0)
        assert result is True, f"Reconnect success kutilgan, lekin {result}"
        assert conn._connect_attempts == 4, f"4 ta connect attempt kutilgan, lekin {conn._connect_attempts}"
        # 3 ta sleep chaqirilgan bo'lishi kerak (har failed attempt'dan keyin)
        assert len(sleep_calls) == 3, f"3 ta sleep kutilgan, lekin {len(sleep_calls)}: {sleep_calls}"
        # Exponential backoff: 2, 4, 8
        assert sleep_calls == [2.0, 4.0, 8.0], f"Backoff sequence noto'g'ri: {sleep_calls}"
    finally:
        mod.time.sleep = original_sleep


@test("ensure_connected() — barcha retry'lar fail bo'lsa False")
def _():
    import apps.api.src.agents.trader.mt5_connector as mod
    original_sleep = mod.time.sleep
    mod.time.sleep = lambda x: None
    try:
        conn = MT5Connector()
        conn._sim_mode = False
        conn._login = 12345
        conn._password = "x"
        conn._server = "test-server"
        conn.is_connected = lambda: False
        attempts = {"n": 0}

        def always_fail(login, password, server):
            attempts["n"] += 1
            raise Exception(f"fail #{attempts['n']}")

        conn.connect = always_fail
        result = conn.ensure_connected(max_retries=4, initial_backoff=1.0)
        assert result is False, f"All-fail'da False kutilgan, lekin {result}"
        assert attempts["n"] == 4, f"4 ta attempt kutilgan, lekin {attempts['n']}"
    finally:
        mod.time.sleep = original_sleep


@test("ensure_connected() — muvaffaqiyatda _last_recovery_downtime_min hisoblanadi")
def _():
    import apps.api.src.agents.trader.mt5_connector as mod
    original_sleep = mod.time.sleep
    mod.time.sleep = lambda x: None
    try:
        conn = MT5Connector()
        conn._sim_mode = False
        conn._login = 12345
        conn._password = "x"
        conn._server = "test-server"
        # 7 daqiqa oldin uzilgan
        conn._last_disconnect_at = datetime.now(timezone.utc) - timedelta(minutes=7)
        conn._connect_attempts = 0

        def fake_is_connected():
            return conn._connect_attempts >= 1

        def fake_connect(login, password, server):
            conn._connect_attempts += 1
            return {"login": login}

        conn.is_connected = fake_is_connected
        conn.connect = fake_connect

        result = conn.ensure_connected(max_retries=3)
        assert result is True
        # Downtime taxminan 7 daqiqa
        assert conn._last_recovery_downtime_min >= 6, (
            f"Recovery downtime ~7 min kutilgan, lekin {conn._last_recovery_downtime_min}"
        )
        # Disconnect marker tozalanishi kerak
        assert conn._last_disconnect_at is None
    finally:
        mod.time.sleep = original_sleep


@test("get_disconnect_duration_min() — ulanganda None")
def _():
    conn = MT5Connector()
    conn._last_disconnect_at = None
    result = conn.get_disconnect_duration_min()
    assert result is None, f"Ulanganda None kutilgan, lekin {result}"


@test("get_disconnect_duration_min() — uzilganda int")
def _():
    conn = MT5Connector()
    conn._last_disconnect_at = datetime.now(timezone.utc) - timedelta(minutes=3)
    result = conn.get_disconnect_duration_min()
    assert isinstance(result, int), f"int kutilgan, lekin {type(result).__name__}"
    assert 2 <= result <= 4, f"~3 min kutilgan, lekin {result}"


@test("ensure_connected() — allaqachon ulangan bo'lsa darhol True")
def _():
    conn = MT5Connector()
    conn._sim_mode = True  # sim mode -> is_connected always True
    conn._login = 1
    conn._password = "x"
    conn._server = "s"
    # connect chaqirilmasligi kerak
    call_count = {"n": 0}
    original_connect = conn.connect

    def spy_connect(*a, **kw):
        call_count["n"] += 1
        return original_connect(*a, **kw)
    conn.connect = spy_connect

    result = conn.ensure_connected(max_retries=5)
    assert result is True, f"Allaqachon ulangan'da True kutilgan, lekin {result}"
    assert call_count["n"] == 0, f"connect chaqirilmasligi kerak edi, lekin {call_count['n']} marta"


@test("ensure_connected() — connect_attempts birinchisida success")
def _():
    import apps.api.src.agents.trader.mt5_connector as mod
    original_sleep = mod.time.sleep
    sleep_calls = []
    mod.time.sleep = lambda x: sleep_calls.append(x)
    try:
        conn = MT5Connector()
        conn._sim_mode = False
        conn._login = 1
        conn._password = "x"
        conn._server = "s"
        conn._connect_attempts = 0

        def fake_is_connected():
            return conn._connect_attempts >= 1

        def fake_connect(login, password, server):
            conn._connect_attempts += 1
            return {"login": login}

        conn.is_connected = fake_is_connected
        conn.connect = fake_connect

        result = conn.ensure_connected(max_retries=5)
        assert result is True
        assert conn._connect_attempts == 1, f"1 ta attempt kutilgan, lekin {conn._connect_attempts}"
        # Birinchi success — sleep chaqirilmasligi kerak (loop'dan oldin chiqib ketdi)
        assert len(sleep_calls) == 0, f"Sleep chaqirilmasligi kerak, lekin {sleep_calls}"
    finally:
        mod.time.sleep = original_sleep


print(f"\nResult: {PASSED} passed, {FAILED} failed")
sys.exit(0 if FAILED == 0 else 1)
