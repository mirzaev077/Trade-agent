# -*- coding: utf-8 -*-
"""
OpenClaw TraderAgent -- Interactive CLI Launcher with Hot Reload
"""
import asyncio
import sys
import os
import time
import signal
import traceback
import importlib
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv
_root = Path(__file__).parent.parent.parent
load_dotenv(_root / ".env", override=True)

from loguru import logger

# Fayl log'ini ENG BOSHIDA yoqamiz — bundan keyingi har bir import va
# startup xatosi diskka tushsin. Ilgari log faqat stderr'da edi, shuning
# uchun oyna yopilishi bilan crash sababi yo'qolardi (B3 blokeri).
from src.agents.trader.utils.logging_setup import setup_logging

_LOG_FILE = setup_logging()

from src.agents.trader import TraderAgent, TradingConfig
from src.agents.trader.utils.telegram_bot import (
    notify_crash as _tg_notify_crash,
    notify_shutdown as _tg_notify_shutdown,
    stop_polling as _tg_stop_polling,
)
from src.agents.trader.utils.telegram_commands import wire_up as _tg_wire_up
from src.agents.trader.health import HealthState, start_health_server_in_thread

# F1-4: bot ishga tushganda yagona shared state — agent va FastAPI o'rtasida
_HEALTH_STATE = HealthState()


# ── Crash + shutdown alerts (F0-3) ────────────────────────────────────────────
#
# Bu setup IMPORT paytida o'rnatiladi — `if __name__ == "__main__"` dan oldin.
# Sababi: agent crash bo'lsa yoki SIGTERM kelsa, Telegram'ga darhol xabar ketsin.

_shutdown_in_progress = False


def _crash_handler(exc_type, exc_value, exc_tb):
    """Uncaught exception → Telegram + default behaviour."""
    # KeyboardInterrupt ni alohida ishlatamiz (default chain'ga beramiz)
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    # Avval DISKKA yozamiz — Telegram yoki stderr ishlamay qolsa ham
    # crash sababi `errors.log` da qoladi. B3: aynan shu yozuv yetishmasdi.
    try:
        logger.opt(exception=(exc_type, exc_value, exc_tb)).critical(
            f"[crash] Kutilmagan xato — bot to'xtadi: {exc_type.__name__}: {exc_value}"
        )
    except Exception:  # noqa: BLE001 — crash ichida crash yo'q
        pass
    try:
        tb_str = "".join(traceback.format_tb(exc_tb))[-2500:]
        _tg_notify_crash(exc_type, exc_value, tb_str)
    except Exception:  # noqa: BLE001 — crash ichida crash yo'q
        pass
    try:
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    except Exception:  # noqa: BLE001
        pass


def _shutdown_handler(signum, frame):
    """SIGTERM / SIGINT → Telegram shutdown xabari + clean exit."""
    global _shutdown_in_progress
    if _shutdown_in_progress:
        # Ikkinchi marta signal — darhol majburiy chiqish
        os._exit(1)
    _shutdown_in_progress = True

    if hasattr(signal, "SIGTERM") and signum == signal.SIGTERM:
        reason = "SIGTERM"
    elif signum == signal.SIGINT:
        reason = "SIGINT (Ctrl+C)"
    else:
        reason = f"signal {signum}"

    try:
        logger.warning(f"[shutdown] {reason} qabul qilindi — bot to'xtatilmoqda...")
    except Exception:  # noqa: BLE001
        pass
    try:
        _tg_notify_shutdown(reason)
    except Exception:  # noqa: BLE001
        pass
    # enqueue=True — navbatdagi yozuvlar diskka yetib borsin, keyin chiqamiz
    try:
        from src.agents.trader.utils.logging_setup import shutdown_logging
        shutdown_logging()
    except Exception:  # noqa: BLE001
        pass
    sys.exit(0)


# Excepthook hamma platformalarda ishlaydi
sys.excepthook = _crash_handler

# SIGINT — barcha platformalarda (Ctrl+C)
try:
    signal.signal(signal.SIGINT, _shutdown_handler)
except (ValueError, OSError) as _e:
    # main thread'da bo'lmasa ValueError. Xizmat sifatida ishlasa shunday bo'lishi mumkin.
    logger.debug(f"[shutdown] SIGINT handler o'rnatilmadi: {_e}")

# SIGTERM — POSIX'da to'liq, Windows'da Python 3.x'da mavjud bo'lsa-da,
# faqat console-control orqali yetkaziladi. os.name check bilan o'raymiz.
if os.name != "nt" and hasattr(signal, "SIGTERM"):
    try:
        signal.signal(signal.SIGTERM, _shutdown_handler)
    except (ValueError, OSError) as _e:
        logger.debug(f"[shutdown] SIGTERM handler o'rnatilmadi: {_e}")
else:
    # Windows'da SIGBREAK (Ctrl+Break) mavjud — uni qo'shimcha sifatida o'rnatamiz
    if hasattr(signal, "SIGBREAK"):
        try:
            signal.signal(signal.SIGBREAK, _shutdown_handler)
        except (ValueError, OSError) as _e:
            logger.debug(f"[shutdown] SIGBREAK handler o'rnatilmadi: {_e}")


# ── Hot Reload ────────────────────────────────────────────────────────────────

# BUG (2026-08-03 gacha): bu yerda `parent.parent` yozilgan edi va yo'l
# `apps/src/agents/trader` ga ishora qilardi — bunday papka MAVJUD EMAS.
# `rglob("*.py")` bo'sh ro'yxat qaytargani uchun watcher hech qachon
# ishlamagan, lekin startupda "HOT RELOAD yoqilgan" deb yozilardi. Xato
# jimgina yutilgan: kod o'zgarsa ham bot qayta yuklanmagan.
# main.py `apps/api/` ichida, ya'ni kod papkasiga bitta `parent` yetadi.
_WATCH_DIR = Path(__file__).resolve().parent / "src" / "agents" / "trader"


def _hot_reload_enabled() -> bool:
    """`HOT_RELOAD` env kaliti. Default: yoqilgan (avvaldan e'lon qilingan xulq).

    F5-5 (100+ savdo, ~30 kun uzluksiz) davomida kodga tegib ketish jonli
    botni qayta ishga tushirmasin desangiz — `.env` da `HOT_RELOAD=false`.
    """
    return os.getenv("HOT_RELOAD", "true").strip().lower() not in {"false", "0", "no", "off"}

# Reload qilish tartibi: leaf → root (dependency order)
_RELOAD_ORDER = [
    "utils.timeframes",
    "utils.session_times",
    "utils",
    "models.config",
    "models.signals",
    "models.orders",
    "models",
    "risk.manager",
    "risk",
    "brain.ai_validator",
    "brain",
    "analysis.ict",
    "analysis",
    "mt5_connector",
    "agent",
]


def _reload_modules():
    """Barcha trader modullarini to'g'ri tartibda qayta yuklash."""
    prefix = "src.agents.trader"
    loaded = {
        name: mod for name, mod in sys.modules.items()
        if name.startswith(prefix)
    }
    reloaded = set()

    def try_reload(name):
        if name in reloaded:
            return
        for full_name, mod in loaded.items():
            if full_name.endswith(name) or full_name == f"{prefix}.{name}":
                try:
                    importlib.reload(mod)
                    reloaded.add(full_name)
                except Exception as e:
                    logger.warning(f"Reload skip {full_name}: {e}")

    for keyword in _RELOAD_ORDER:
        try_reload(keyword)

    # Qayta yuklanmagan qolganlarni ham reload qilish
    for name, mod in loaded.items():
        if name not in reloaded:
            try:
                importlib.reload(mod)
            except Exception:
                pass

    logger.info(f"[HOT RELOAD] {len(reloaded)} ta modul qayta yuklandi")


class _FileWatcher:
    """Arzon polling-based file watcher — tashqi kutubxona talab qilmaydi."""

    def __init__(self, directory: Path, on_change):
        self._dir       = directory
        self._callback  = on_change
        self._stop      = threading.Event()
        self._mtimes: dict = {}
        self._thread    = threading.Thread(target=self._run, daemon=True)

    def start(self):
        for f in self._dir.rglob("*.py"):
            try:
                self._mtimes[str(f)] = f.stat().st_mtime
            except Exception:
                pass
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            time.sleep(1.5)
            for f in self._dir.rglob("*.py"):
                path = str(f)
                try:
                    mtime = f.stat().st_mtime
                    prev  = self._mtimes.get(path)
                    self._mtimes[path] = mtime
                    if prev is not None and mtime != prev:
                        self._callback(f.name)
                        return   # bitta o'zgarish — watcher to'xtaydi
                except Exception:
                    pass


# ── Onboarding ────────────────────────────────────────────────────────────────

def print_banner():
    print("=" * 58)
    print("     OpenClaw Trading Agent  v1.0")
    print("     Senior Quantitative Trader -- MT5 Edition")
    print("=" * 58)
    print()


def ask(prompt, default=""):
    val = input("  " + prompt + ": ").strip()
    return val if val else default


def onboarding():
    print_banner()

    # .env dan avtomatik o'qish
    env_login    = os.getenv("MT5_LOGIN", "")
    env_password = os.getenv("MT5_PASSWORD", "")
    env_server   = os.getenv("MT5_SERVER", "Exness-MT5Trial15")
    env_symbol   = os.getenv("SYMBOL", "XAUUSD")
    env_risk     = os.getenv("RISK_PER_TRADE", "1")
    env_daily    = os.getenv("DAILY_MAX_RISK", "3")
    env_maxpos   = os.getenv("MAX_POSITIONS", "3")
    api_key      = os.getenv("CLAUDE_API_KEY", "")

    auto_mode = bool(env_login and env_password)

    if auto_mode:
        print("  .env topildi — avtomatik sozlamalar ishlatilmoqda")
        print()
        login_str = env_login
        password  = env_password
        server    = env_server
        symbol    = env_symbol.upper()
        try:
            risk_pct    = float(env_risk)
            daily_pct   = float(env_daily)
            max_pos_int = int(env_maxpos)
        except ValueError:
            risk_pct, daily_pct, max_pos_int = 1.0, 3.0, 3
    else:
        print("Step 1: MetaTrader 5 Credentials")
        print("-" * 40)
        login_str = ask("Account ID (login raqam)")
        password  = ask("Password (parol)")
        server    = ask("Server (masalan: Exness-MT5Trial15)", "Exness-MT5Trial15")
        symbol    = "XAUUSD"
        risk_pct, daily_pct, max_pos_int = 1.0, 3.0, 3

    try:
        login = int(login_str)
    except ValueError:
        print("XATO: Login raqam bo'lishi kerak.")
        sys.exit(1)

    print("MT5 ga ulanilmoqda...")
    tmp_cfg = TradingConfig(mt5_login=login, mt5_password=password, mt5_server=server)
    agent   = TraderAgent(tmp_cfg)

    try:
        info = agent.connect(login, password, server)
    except Exception as e:
        print("XATO: " + str(e))
        sys.exit(1)

    print()
    print("  Ulandi!")
    print("  Account : #" + str(info["login"]))
    print("  Balance : $" + str(round(info["balance"], 2)))
    print("  Leverage: 1:" + str(info["leverage"]))
    print("  Server  : " + str(info["server"]))
    print("  Type    : " + str(info["trade_mode"]))
    print()
    print("=" * 58)
    print("  XULOSA:")
    print("  Symbol   : " + symbol)
    print("  Risk/trade: " + str(risk_pct) + "%")
    print("  Daily max : " + str(daily_pct) + "%")
    print("  Max pos  : " + str(max_pos_int))
    print("  AI Brain : " + ("ON" if api_key else "OFF"))
    print("=" * 58)
    print()

    if not auto_mode:
        confirm = ask("Boshlash uchun 'START' yozing", "")
        if confirm.upper() != "START":
            print("Chiqildi.")
            sys.exit(0)

    return TradingConfig(
        mt5_login=login,
        mt5_password=password,
        mt5_server=server,
        symbol=symbol,
        risk_per_trade=risk_pct,
        daily_max_risk=daily_pct,
        max_positions=max_pos_int,
        claude_api_key=api_key,
    )


# ── Main loop with hot reload ─────────────────────────────────────────────────

async def _run_agent(config: TradingConfig) -> bool:
    """
    Agent ishga tushirib uning run() ni kutadi.
    Returns True  → fayl o'zgardi, qayta yuklash kerak
    Returns False → Ctrl+C, to'xtatish kerak
    """
    # Har safar fresh import (reload dan keyin yangi class talab qilinadi)
    from src.agents.trader import TraderAgent as _Agent

    agent        = _Agent(config, health_state=_HEALTH_STATE)
    restart_flag = [False]

    def _on_change(filename: str):
        logger.warning(f"[HOT RELOAD] {filename} o'zgardi — agent to'xtatilmoqda...")
        restart_flag[0] = True
        agent.running   = False   # run() loopini to'xtatadi

    # Watcher faqat papka HAQIQATAN mavjud bo'lsa quriladi. Yo'l noto'g'ri
    # bo'lsa endi jimgina o'tib ketmaydi — ogohlantirish yoziladi (aynan shu
    # jimlik bugni oylab yashirgan).
    watcher = None
    if not _hot_reload_enabled():
        logger.info("[HOT RELOAD] o'chirilgan (HOT_RELOAD=false)")
    elif not _WATCH_DIR.is_dir():
        logger.warning(f"[HOT RELOAD] kuzatuv papkasi topilmadi: {_WATCH_DIR} — o'chirildi")
    else:
        watcher = _FileWatcher(_WATCH_DIR, _on_change)

    try:
        agent.connect(config.mt5_login, config.mt5_password, config.mt5_server)
    except Exception as e:
        logger.error(f"MT5 ulanish xatosi: {e}")
        return False

    # F3 wire-up: /status /positions /pause /resume + inbound polling thread.
    # No-op if TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are missing.
    try:
        if _tg_wire_up(agent):
            logger.info("[telegram] inbound commands ulandi (/status /positions /pause /resume)")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[telegram] wire-up xato: {e}")

    # Xabar HAQIQIY holatni aytsin — avval watcher o'lik bo'lsa ham
    # "yoqilgan" deb yozilardi va shu yolg'on bugni yashirgan.
    _hr = (
        "HOT RELOAD yoqilgan — fayl saqlansang agent o'zi qayta ishga tushadi"
        if watcher is not None else "HOT RELOAD o'chiq"
    )
    logger.info(
        f"Trading boshlandi! {config.symbol} "
        f"har {config.scan_interval}s scan. [{_hr}]"
    )

    if watcher is not None:
        watcher.start()
    try:
        await agent.run()
    except KeyboardInterrupt:
        agent.running = False
    finally:
        if watcher is not None:
            watcher.stop()
        try:
            _tg_stop_polling()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[telegram] stop_polling xato: {e}")

    return restart_flag[0]


async def main():
    logger.info(f"[logging] fayl log yoqildi: {_LOG_FILE}  (xatolar: {_LOG_FILE.parent / 'errors.log'})")
    config = onboarding()

    # F1-4: healthcheck endpoint'ni daemon thread'da ishga tushirish
    # (asosiy bot loop blokrlanmaydi; port 8080)
    _HEALTH_STATE.scan_interval_sec = int(config.scan_interval)
    try:
        start_health_server_in_thread(_HEALTH_STATE)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[healthcheck] thread start xato: {e}")

    while True:
        should_reload = await _run_agent(config)

        if not should_reload:
            print("\nAgent to'xtatildi.")
            break

        # Hot reload
        print()
        logger.info("[HOT RELOAD] Modullar qayta yuklanmoqda...")
        await asyncio.sleep(0.3)
        _reload_modules()
        await asyncio.sleep(0.3)
        logger.info("[HOT RELOAD] Agent qayta ishga tushmoqda...\n")


if __name__ == "__main__":
    asyncio.run(main())
