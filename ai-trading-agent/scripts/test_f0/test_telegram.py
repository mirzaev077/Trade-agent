"""F0-3 test: telegram_bot.py sync notify funksiyalari + main.py hooks

Tekshiruvlar:
  - notify_crash / notify_disconnect / notify_recovered / notify_shutdown
    token yo'q paytda exception otmaydi (logger.error)
  - Telegram API call format (urllib.request mock orqali)
  - Token + chat_id env vars dan o'qiladi (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
  - main.py import qilingach sys.excepthook o'rnatilgan
  - Platform-mos signal handler'lar (POSIX: SIGTERM+SIGINT; Windows: SIGBREAK+SIGINT)
  - Crash xabari uzun bo'lsa _MAX_TG_LEN ga trim qilinadi

Run:
    cd ai-trading-agent
    python scripts/test_f0/test_telegram.py
"""
import sys
import os
import importlib
import signal
from pathlib import Path
from unittest.mock import patch, MagicMock

# loyiha rootini sys.path'ga qo'shish
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# main.py uchun `from src.agents.trader ...` import ishlashi shart —
# bu `apps/api` ni path'ga qo'shishni talab qiladi.
APPS_API = ROOT / "apps" / "api"
sys.path.insert(0, str(APPS_API))


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


TG_MOD = "apps.api.src.agents.trader.utils.telegram_bot"


def _reload_telegram(env: dict | None = None):
    """Modulni fresh state bilan reload qilish.

    Module global'lar (_TOKEN, _CHAT_ID, _ENABLED) eski testlardan
    sizib kelmasligi uchun env'ni ham tozalab reimport qilamiz.

    MUHIM: Bu funksiya os.environ'ni MUTATIYA qiladi (test ichida
    permanent o'zgartirmaslik uchun har test boshida qayta chaqirilishi shart).
    """
    # Eski modul versiyasini olib tashlash
    sys.modules.pop(TG_MOD, None)

    # TELEGRAM_* env'ni tozalash
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    os.environ.pop("TELEGRAM_CHAT_ID", None)
    if env:
        for k, v in env.items():
            os.environ[k] = v

    return importlib.import_module(TG_MOD)


def _cleanup_env():
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    os.environ.pop("TELEGRAM_CHAT_ID", None)


def _make_urlopen_mock(captured: list):
    """urllib.request.urlopen uchun mock — kelgan so'rovni captured'ga yozadi."""

    def fake_urlopen(req, timeout=5):
        try:
            url = req.full_url
            data = req.data.decode("utf-8") if req.data else ""
        except Exception:
            url = str(req)
            data = ""
        captured.append({"url": url, "data": data, "timeout": timeout})
        resp = MagicMock()
        resp.read.return_value = b'{"ok": true}'
        resp.__enter__ = lambda self: resp
        resp.__exit__ = lambda *a: None
        return resp

    return fake_urlopen


print("\n=== F0-3 telegram_bot sync alerts ===")


# --- Token yo'q paytda crash yo'q ---------------------------------------------

@test("notify_crash without token -> no exception, logger.error qiladi")
def _():
    tg = _reload_telegram(env=None)  # token + chat_id yo'q
    # Hech qanday exception otmasligi shart
    tg.notify_crash(ValueError, ValueError("test"), "Traceback line\n  File ...\n")


@test("notify_disconnect without token -> no exception")
def _():
    tg = _reload_telegram(env=None)
    tg.notify_disconnect(7)


@test("notify_recovered without token -> no exception")
def _():
    tg = _reload_telegram(env=None)
    tg.notify_recovered(12)


@test("notify_shutdown without token -> no exception")
def _():
    tg = _reload_telegram(env=None)
    tg.notify_shutdown("SIGTERM")


# --- Token + chat_id env vars dan o'qiladi ------------------------------------

@test("env TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID resolve qilinadi")
def _():
    try:
        tg = _reload_telegram(env={
            "TELEGRAM_BOT_TOKEN": "envtok123",
            "TELEGRAM_CHAT_ID":   "envchat456",
        })
        # init() chaqirilmasa ham _resolve_token_chat env'dan olishi kerak
        token, chat = tg._resolve_token_chat()
        assert token == "envtok123", f"token={token!r}"
        assert chat == "envchat456", f"chat={chat!r}"
    finally:
        _cleanup_env()


@test("init() module globals'ni o'rnatadi")
def _():
    tg = _reload_telegram(env=None)
    tg.init("inittok", "initchat")
    token, chat = tg._resolve_token_chat()
    assert token == "inittok"
    assert chat == "initchat"


# --- API call format ---------------------------------------------------------

@test("notify_disconnect Telegram API ga to'g'ri format yuboradi")
def _():
    try:
        tg = _reload_telegram(env={
            "TELEGRAM_BOT_TOKEN": "test123",
            "TELEGRAM_CHAT_ID":   "456",
        })
        captured: list = []
        with patch("urllib.request.urlopen", _make_urlopen_mock(captured)):
            tg.notify_disconnect(7)

        assert len(captured) == 1, f"Expected 1 API call, got {len(captured)}"
        req = captured[0]
        assert "sendMessage" in req["url"], f"URL: {req['url']}"
        assert "test123" in req["url"], "Token URL'da bo'lishi shart"
        assert "456" in req["data"], f"chat_id payload'da bo'lishi shart: {req['data'][:200]}"
        assert "MT5" in req["data"]
        # Duration 7 daqiqa
        assert "7 daqiqa" in req["data"] or '"7' in req["data"], \
            f"Duration 7 ko'rinmadi: {req['data'][:300]}"
    finally:
        _cleanup_env()


@test("notify_crash API'ga exception turi va matni bilan yuboradi")
def _():
    try:
        tg = _reload_telegram(env={
            "TELEGRAM_BOT_TOKEN": "xx",
            "TELEGRAM_CHAT_ID":   "yy",
        })
        captured: list = []
        with patch("urllib.request.urlopen", _make_urlopen_mock(captured)):
            tg.notify_crash(RuntimeError, RuntimeError("boom"), "Traceback (...):\n  line 1\n")

        assert len(captured) == 1, f"calls={len(captured)}"
        body = captured[0]["data"]
        assert "RuntimeError" in body, f"Exception type yo'q: {body[:300]}"
        assert "boom" in body
        assert "CRASH" in body or "BOT" in body
    finally:
        _cleanup_env()


@test("notify_recovered downtime daqiqani payload'da uzatadi")
def _():
    try:
        tg = _reload_telegram(env={
            "TELEGRAM_BOT_TOKEN": "xx",
            "TELEGRAM_CHAT_ID":   "yy",
        })
        captured: list = []
        with patch("urllib.request.urlopen", _make_urlopen_mock(captured)):
            tg.notify_recovered(13)

        assert len(captured) == 1
        body = captured[0]["data"]
        assert "13" in body, body[:300]
        assert "QAYTA" in body or "ULANDI" in body
    finally:
        _cleanup_env()


@test("notify_shutdown reason'ni payload'ga qo'shadi")
def _():
    try:
        tg = _reload_telegram(env={
            "TELEGRAM_BOT_TOKEN": "xx",
            "TELEGRAM_CHAT_ID":   "yy",
        })
        captured: list = []
        with patch("urllib.request.urlopen", _make_urlopen_mock(captured)):
            tg.notify_shutdown("SIGINT (Ctrl+C)")

        assert len(captured) == 1
        body = captured[0]["data"]
        # HTML escape qilingan bo'lishi mumkin — substring tekshirish
        assert "SIGINT" in body, body[:300]
    finally:
        _cleanup_env()


# --- Trim logic --------------------------------------------------------------

@test("notify_crash uzun traceback'ni _MAX_TG_LEN ga trim qiladi")
def _():
    try:
        tg = _reload_telegram(env={
            "TELEGRAM_BOT_TOKEN": "xx",
            "TELEGRAM_CHAT_ID":   "yy",
        })
        huge_tb = "TBLINE\n" * 2000  # ~14000 belgi
        captured: list = []
        with patch("urllib.request.urlopen", _make_urlopen_mock(captured)):
            tg.notify_crash(ValueError, ValueError("test"), huge_tb)

        assert len(captured) == 1
        body = captured[0]["data"]
        # body — JSON payload. Text uzunligi _MAX_TG_LEN dan oshmasligi shart.
        assert len(body) < tg._MAX_TG_LEN + 500, f"Payload juda uzun: {len(body)}"
    finally:
        _cleanup_env()


@test("notify_crash trim — Telegram 4096 limitidan past")
def _():
    try:
        tg = _reload_telegram(env={
            "TELEGRAM_BOT_TOKEN": "xx",
            "TELEGRAM_CHAT_ID":   "yy",
        })
        huge_tb = "A" * 10000
        captured: list = []
        with patch("urllib.request.urlopen", _make_urlopen_mock(captured)):
            tg.notify_crash(ValueError, ValueError("test"), huge_tb)

        assert len(captured) == 1
        body = captured[0]["data"]
        # Telegram payload < 5500 (4096 text + JSON overhead ~ 300)
        assert len(body) < 5500, f"Body too long: {len(body)}"
    finally:
        _cleanup_env()


@test("_send_sync ham juda uzun matnni trim qiladi")
def _():
    try:
        tg = _reload_telegram(env={
            "TELEGRAM_BOT_TOKEN": "xx",
            "TELEGRAM_CHAT_ID":   "yy",
        })
        captured: list = []
        text = "X" * 6000
        with patch("urllib.request.urlopen", _make_urlopen_mock(captured)):
            tg._send_sync(text)

        assert len(captured) == 1
        body = captured[0]["data"]
        assert "qisqartirildi" in body or len(body) < 5500
    finally:
        _cleanup_env()


# --- HTTP error suppression --------------------------------------------------

@test("urllib URLError -> logger.error, no raise")
def _():
    try:
        import urllib.error
        tg = _reload_telegram(env={
            "TELEGRAM_BOT_TOKEN": "xx",
            "TELEGRAM_CHAT_ID":   "yy",
        })

        def boom(req, timeout=5):
            raise urllib.error.URLError("network down")

        with patch("urllib.request.urlopen", boom):
            tg.notify_disconnect(5)
    finally:
        _cleanup_env()


@test("OSError -> logger.error, no raise")
def _():
    try:
        tg = _reload_telegram(env={
            "TELEGRAM_BOT_TOKEN": "xx",
            "TELEGRAM_CHAT_ID":   "yy",
        })

        def boom(req, timeout=5):
            raise OSError("connection refused")

        with patch("urllib.request.urlopen", boom):
            tg.notify_crash(ValueError, ValueError("x"), "tb")
    finally:
        _cleanup_env()


# --- main.py hooks -----------------------------------------------------------

@test("main.py import qilingach sys.excepthook o'rnatiladi")
def _():
    original_hook = sys.excepthook
    sys.excepthook = sys.__excepthook__
    sys.modules.pop("apps.api.main", None)
    try:
        importlib.import_module("apps.api.main")
        assert sys.excepthook is not sys.__excepthook__, \
            "main.py import qilingach excepthook hali ham default"
        assert sys.excepthook.__name__ == "_crash_handler", \
            f"excepthook nomi: {sys.excepthook.__name__}"
    finally:
        sys.excepthook = original_hook


@test("main.py SIGINT handler o'rnatadi")
def _():
    original = signal.getsignal(signal.SIGINT)
    try:
        sys.modules.pop("apps.api.main", None)
        importlib.import_module("apps.api.main")
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler), f"SIGINT handler callable emas: {handler}"
        assert getattr(handler, "__name__", "") == "_shutdown_handler", \
            f"SIGINT handler nomi: {getattr(handler, '__name__', handler)}"
    finally:
        try:
            signal.signal(signal.SIGINT, original)
        except (ValueError, OSError, TypeError):
            pass


@test("Platform-mos: Windows -> SIGBREAK, POSIX -> SIGTERM")
def _():
    sys.modules.pop("apps.api.main", None)
    importlib.import_module("apps.api.main")

    if os.name == "nt":
        # Windows: SIGBREAK o'rnatilgan, SIGTERM skip qilingan
        if hasattr(signal, "SIGBREAK"):
            handler = signal.getsignal(signal.SIGBREAK)
            assert callable(handler), \
                f"Windows: SIGBREAK handler o'rnatilmagan: {handler}"
        # SIGTERM Windows'da default qoladi (main.py talabi)
    else:
        # POSIX: SIGTERM ham o'rnatilgan
        if hasattr(signal, "SIGTERM"):
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler), \
                f"POSIX: SIGTERM handler o'rnatilmagan: {handler}"


# --- Loose: _send_sync token yo'q -> no HTTP call ---------------------------

@test("_send_sync token yo'q paytda HTTP chaqirmaydi")
def _():
    tg = _reload_telegram(env=None)
    captured: list = []
    with patch("urllib.request.urlopen", _make_urlopen_mock(captured)):
        tg._send_sync("hello")
    assert len(captured) == 0, f"Token yo'q, lekin HTTP chaqirildi: {captured}"


_cleanup_env()
print(f"\nResult: {PASSED} passed, {FAILED} failed")
sys.exit(0 if FAILED == 0 else 1)
