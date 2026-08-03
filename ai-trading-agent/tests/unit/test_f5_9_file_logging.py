"""
F5-9: faylga log yozish — crash sabablarini diskda saqlash.

MUAMMO (2026-08-03): loyihada birorta ham doimiy `logger.add(<fayl>)` yo'q edi.
Butun log `sys.stderr` ga, ya'ni `cmd /k` oynasining ichiga ketardi. Oyna
yopilsa yoki bot crash bo'lsa — dalil yo'qolardi. Shu sabab B3 taskini
("nega bot 63 kun ichida faqat 5 kun ishlagan") tekshirib bo'lmasdi:
`apps/data/logs/` papkasi 2026-07-23 dan beri bo'sh turardi.

F5-5 uchun bot ~30 kun uzluksiz ishlashi kerak. Log bo'lmasa har uzilish
qayta-qayta noma'lum sabab bo'lib qoladi — aylanma muammo.

Bu fayl quyidagilarni qotiradi:
  • Group A — yo'l prioriteti (OPENCLAW_LOG_DIR > LOG_DIR > default)
  • Group B — yozuv haqiqatan faylga tushishi, daraja filtri, UTF-8
  • Group C — XAVFSIZLIK: traceback lokal o'zgaruvchilarni (parol!) sizdirmasin
  • Group D — main.py haqiqatan setup_logging chaqirishi (source guard)
"""
from __future__ import annotations

from pathlib import Path

import pytest
from loguru import logger

from apps.api.src.agents.trader.utils import logging_setup


@pytest.fixture
def log_dir(tmp_path):
    """Izolyatsiyalangan log papkasi; testdan keyin sink'lar yopiladi.

    `shutdown_logging()` MAJBURIY: sink'lar `enqueue=True` bilan qo'shiladi,
    ya'ni yozuv fon thread orqali ketadi. Yopmasdan faylni o'qish flaky
    bo'lardi. Yopilish esa navbatni flush qiladi.
    """
    d = tmp_path / "logs"
    yield d
    logging_setup.shutdown_logging()


# ── Group A: yo'l prioriteti ─────────────────────────────────────────────────

def test_get_log_dir_honors_openclaw_env(tmp_path, monkeypatch):
    """`OPENCLAW_LOG_DIR` o'rnatilgan bo'lsa shu papka ishlatiladi."""
    target = tmp_path / "custom_logs"
    monkeypatch.setenv("OPENCLAW_LOG_DIR", str(target))
    assert logging_setup.get_log_dir() == target
    assert target.is_dir()   # yo'q bo'lsa yaratiladi


def test_get_log_dir_honors_log_dir_env(tmp_path, monkeypatch):
    """`.env` dagi `LOG_DIR` ham hurmat qilinadi."""
    monkeypatch.delenv("OPENCLAW_LOG_DIR", raising=False)
    target = tmp_path / "env_logs"
    monkeypatch.setenv("LOG_DIR", str(target))
    assert logging_setup.get_log_dir() == target


def test_openclaw_log_dir_beats_log_dir(tmp_path, monkeypatch):
    """Test override (`OPENCLAW_LOG_DIR`) `.env` dagi `LOG_DIR` dan ustun.

    Aks holda ishlab turgan `.env` testni jonli papkaga yo'naltirib yuborardi —
    aynan A2 hodisasining takrori bo'lardi.
    """
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "from_env"))
    monkeypatch.setenv("OPENCLAW_LOG_DIR", str(tmp_path / "from_test"))
    assert logging_setup.get_log_dir() == tmp_path / "from_test"


def test_explicit_argument_beats_env(tmp_path, monkeypatch):
    """Aniq argument har qanday env'dan ustun."""
    monkeypatch.setenv("OPENCLAW_LOG_DIR", str(tmp_path / "env"))
    explicit = tmp_path / "explicit"
    assert logging_setup.get_log_dir(explicit) == explicit


def test_env_read_at_call_time_not_import_time(tmp_path, monkeypatch):
    """Env CHAQIRUV paytida o'qilsin — modul importidan keyin ham ta'sir qilsin.

    A2 darsi: import paytida o'qilsa `monkeypatch.setenv` kech qolardi.
    """
    first = tmp_path / "first"
    monkeypatch.setenv("OPENCLAW_LOG_DIR", str(first))
    assert logging_setup.get_log_dir() == first

    second = tmp_path / "second"
    monkeypatch.setenv("OPENCLAW_LOG_DIR", str(second))
    assert logging_setup.get_log_dir() == second


def test_default_dir_is_apps_data_logs():
    """Default — `apps/data/logs`, ya'ni state papkasi bilan yonma-yon."""
    assert logging_setup._DEFAULT_LOG_DIR.parts[-2:] == ("data", "logs")
    # persistence.py bilan bir xil ildiz (apps/)
    from apps.api.src.agents.trader.state import persistence
    assert logging_setup._DEFAULT_LOG_DIR.parent == persistence._DEFAULT_STATE_DIR.parent


# ── Group B: yozuv haqiqatan faylga tushadi ──────────────────────────────────

def test_setup_creates_both_files(log_dir):
    """`agent.log` va `errors.log` yaratiladi."""
    agent_log = logging_setup.setup_logging(log_dir=log_dir, console=False)
    assert agent_log == log_dir / "agent.log"
    logger.info("startup")
    logger.error("nosozlik")
    logging_setup.shutdown_logging()

    assert (log_dir / "agent.log").is_file()
    assert (log_dir / "errors.log").is_file()


def test_message_lands_in_agent_log(log_dir):
    """Oddiy INFO yozuvi diskda qoladi — asosiy maqsad."""
    logging_setup.setup_logging(log_dir=log_dir, console=False)
    logger.info("MT5 connected: #260847403")
    logging_setup.shutdown_logging()

    text = (log_dir / "agent.log").read_text(encoding="utf-8")
    assert "MT5 connected: #260847403" in text
    assert "INFO" in text


def test_errors_log_only_has_errors(log_dir):
    """`errors.log` — faqat ERROR+; INFO shovqini u yerga tushmaydi."""
    logging_setup.setup_logging(log_dir=log_dir, console=False)
    logger.info("oddiy tick")
    logger.warning("ogohlantirish")
    logger.error("MT5 uzildi")
    logging_setup.shutdown_logging()

    errors = (log_dir / "errors.log").read_text(encoding="utf-8")
    assert "MT5 uzildi" in errors
    assert "oddiy tick" not in errors
    assert "ogohlantirish" not in errors

    # agent.log da esa hammasi bor
    agent = (log_dir / "agent.log").read_text(encoding="utf-8")
    assert "oddiy tick" in agent
    assert "MT5 uzildi" in agent


def test_level_from_env(log_dir, monkeypatch):
    """`LOG_LEVEL` env darajani belgilaydi."""
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    logging_setup.setup_logging(log_dir=log_dir, console=False)
    logger.info("ko'rinmasin")
    logger.warning("ko'rinsin")
    logging_setup.shutdown_logging()

    text = (log_dir / "agent.log").read_text(encoding="utf-8")
    assert "ko'rinmasin" not in text
    assert "ko'rinsin" in text


def test_utf8_uzbek_and_emoji(log_dir):
    """O'zbekcha matn va emoji buzilmasin.

    Windows default kodlash cp1251/cp1252 — `encoding="utf-8"` bo'lmasa
    bu yozuv UnicodeEncodeError beradi yoki krakozyabra bo'lib qoladi.
    Jonli log'da aynan shunday satrlar bor: "🌪 HIGH-VOL regime ... bloklanadi".
    """
    logging_setup.setup_logging(log_dir=log_dir, console=False)
    logger.info("🌪 HIGH-VOL regime: reversion setuplar bloklanadi — o'chirildi")
    logging_setup.shutdown_logging()

    text = (log_dir / "agent.log").read_text(encoding="utf-8")
    assert "🌪 HIGH-VOL regime" in text
    assert "bloklanadi — o'chirildi" in text


def test_setup_is_idempotent(log_dir):
    """Ikki marta chaqirilsa satr ikki marta yozilmasin."""
    logging_setup.setup_logging(log_dir=log_dir, console=False)
    logging_setup.setup_logging(log_dir=log_dir, console=False)
    logger.info("YAGONA_SATR")
    logging_setup.shutdown_logging()

    text = (log_dir / "agent.log").read_text(encoding="utf-8")
    assert text.count("YAGONA_SATR") == 1


def test_shutdown_removes_sinks(log_dir):
    """`shutdown_logging()` dan keyin fayl o'smaydi."""
    logging_setup.setup_logging(log_dir=log_dir, console=False)
    logger.info("oldin")
    logging_setup.shutdown_logging()
    size_after_shutdown = (log_dir / "agent.log").stat().st_size

    logger.info("keyin — bu yozuv faylga tushmasligi kerak")
    assert (log_dir / "agent.log").stat().st_size == size_after_shutdown
    assert logging_setup._SINK_IDS == []


# ── Group C: XAVFSIZLIK — traceback maxfiy ma'lumot sizdirmasin ─────────────

def test_traceback_is_written(log_dir):
    """Exception traceback'i `errors.log` ga tushadi — B3 uchun asosiy narsa."""
    logging_setup.setup_logging(log_dir=log_dir, console=False)
    try:
        raise RuntimeError("MT5 terminal javob bermadi")
    except RuntimeError:
        logger.opt(exception=True).error("[crash] kutilmagan xato")
    logging_setup.shutdown_logging()

    errors = (log_dir / "errors.log").read_text(encoding="utf-8")
    assert "MT5 terminal javob bermadi" in errors
    assert "RuntimeError" in errors
    assert "Traceback" in errors


def test_traceback_does_not_leak_local_variables(log_dir):
    """XAVFSIZLIK QOTIRUVI: `diagnose=False` — lokal o'zgaruvchilar yozilmasin.

    Loguru `diagnose=True` bilan har frame'ning lokallarini traceback'ga
    qo'shadi. Bizning stack'da `TradingConfig.mt5_password` va
    `claude_api_key` bor — ular diskdagi log faylga tushsa, log ulashish
    (masalan debug uchun yuborish) parolni oshkor qilardi.

    Bu test `diagnose` ni tasodifan `True` qilib qo'yishdan himoya qiladi.
    """
    logging_setup.setup_logging(log_dir=log_dir, console=False)
    secret = "P4rol_MT5_maxfiy_9times"

    def _connect(mt5_password):        # noqa: ARG001 — ataylab ishlatilmaydi
        raise ConnectionError("ulanish uzildi")

    try:
        _connect(secret)
    except ConnectionError:
        logger.opt(exception=True).error("[crash] ulanish xatosi")
    logging_setup.shutdown_logging()

    errors = (log_dir / "errors.log").read_text(encoding="utf-8")
    assert "ulanish uzildi" in errors      # traceback bor
    assert secret not in errors            # lekin parol YO'Q


# ── Group D: main.py haqiqatan ulangan (source guard) ────────────────────────
#
# F5-1 darsi: kod `.env` dan emas, konstantadan o'qiyotgani faqat jonli
# ishlaganda ma'lum bo'lgan. Shundan beri "sozlama ulanganmi" degan savolga
# manba matni bo'yicha javob beradigan test yozamiz.

def _main_py_source() -> str:
    main_py = Path(logging_setup.__file__).resolve().parents[4] / "main.py"
    assert main_py.is_file(), f"main.py topilmadi: {main_py}"
    return main_py.read_text(encoding="utf-8")


def test_main_py_imports_and_calls_setup_logging():
    """`main.py` fayl log'ini yoqmasa — butun tuzatish behuda."""
    src = _main_py_source()
    assert "logging_setup import setup_logging" in src
    assert "setup_logging()" in src


def test_main_py_logs_crash_to_disk():
    """Crash handler Telegram'dan OLDIN diskka yozsin.

    Telegram tarmoq xatosi yoki token muammosi crash izini yo'qotmasligi kerak.
    """
    src = _main_py_source()
    crash_fn = src.split("def _crash_handler")[1].split("def ")[0]
    assert "logger.opt(exception=" in crash_fn, "crash traceback diskka yozilmayapti"
    # tartib: logger.opt(...) chaqiruvi _tg_notify_crash dan oldin bo'lsin
    assert crash_fn.index("logger.opt(exception=") < crash_fn.index("_tg_notify_crash")
