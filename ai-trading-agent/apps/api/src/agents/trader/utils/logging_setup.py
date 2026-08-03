"""Faylga log yozish — crash sabablarini diskda saqlash.

MUAMMO (2026-08-03 da aniqlangan): butun loyihada birorta ham doimiy
`logger.add(<fayl>)` yo'q edi. Loguru default sink'i faqat `sys.stderr` ga
yozadi, ya'ni butun log `cmd /k` oynasining ichida yashaydi. Oyna yopilsa
yoki bot crash bo'lsa — hamma dalil yo'qoladi. Aynan shu sabab B3 taskini
("nega bot 63 kun ichida faqat 5 kun ishlagan") javobsiz qoldirgan:
tekshirishga log yo'q edi.

F5-5 uchun bot ~30 kun uzluksiz ishlashi kerak. Log bo'lmasa har uzilish
qayta-qayta noma'lum sabab bo'lib qoladi.

YECHIM: ikkita fayl sink:
  • `agent.log`  — LOG_LEVEL dan yuqori hammasi, rotatsiya bilan
  • `errors.log` — faqat ERROR+, uzoqroq saqlanadi (crash arxivi)

Yo'l prioriteti `state/persistence.py:get_state_dir()` bilan bir xil:
    aniq argument → OPENCLAW_LOG_DIR (test override) → LOG_DIR → default
Env CHAQIRUV paytida o'qiladi (import paytida emas) — aks holda
`monkeypatch.setenv` importdan keyin ishlamay qolardi (A2 darsi).

XAVFSIZLIK: `diagnose=False` MAJBURIY. Loguru `diagnose=True` bilan
traceback'ga har bir frame'ning lokal o'zgaruvchilarini yozadi — u yerda
`TradingConfig.mt5_password` va `claude_api_key` bor. Ular diskdagi log
faylga tushmasligi kerak. `test_f5_9_file_logging.py` buni tekshiradi.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from loguru import logger

# Log papkasi — persistence.py bilan bir xil hisob: parents[5] == apps/
_DEFAULT_LOG_DIR = Path(__file__).resolve().parents[5] / "data" / "logs"

_AGENT_LOG = "agent.log"
_ERROR_LOG = "errors.log"

# Default qiymatlar — .env dagi LOG_* o'zgaruvchilari ustidan yozadi
_DEFAULT_LEVEL = "INFO"
_DEFAULT_ROTATION = "20 MB"
_DEFAULT_RETENTION = "30 days"
_ERROR_RETENTION = "180 days"

# Bu modul qo'shgan sink id'lari. Faqat shularni olib tashlaymiz —
# tashqarida (masalan pytest) qo'shilgan sink'larga tegmaymiz.
_SINK_IDS: list[int] = []
_default_sink_removed = False

_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
    "{name}:{function}:{line} | {message}"
)


def get_log_dir(log_dir: Optional[Path] = None) -> Path:
    """Log papkasi. Prioritet: argument → OPENCLAW_LOG_DIR → LOG_DIR → default.

    `OPENCLAW_LOG_DIR` — test override (conftest `_isolate_production_files`
    uni har test uchun tmp papkaga qo'yadi). `LOG_DIR` — foydalanuvchi uchun
    `.env` sozlamasi. Papka mavjud bo'lmasa yaratiladi.
    """
    if log_dir is None:
        env_dir = os.getenv("OPENCLAW_LOG_DIR") or os.getenv("LOG_DIR")
        if env_dir:
            log_dir = Path(env_dir)
    d = log_dir or _DEFAULT_LOG_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def setup_logging(
    log_dir: Optional[Path] = None,
    level: Optional[str] = None,
    rotation: Optional[str] = None,
    retention: Optional[str] = None,
    *,
    console: bool = True,
) -> Path:
    """Fayl sink'larini o'rnatadi va `agent.log` yo'lini qaytaradi.

    Idempotent: qayta chaqirilsa avvalgi sink'lar olib tashlanadi, satrlar
    ikki marta yozilmaydi. `console=True` bo'lsa loguru'ning default stderr
    handler'i bizniki bilan almashtiriladi (bir marta) — shunda konsol
    chiqishi va fayl formati mos bo'ladi.

    Args:
        log_dir:   papka (None → get_log_dir prioriteti)
        level:     eng past daraja (None → LOG_LEVEL env → INFO)
        rotation:  fayl aylanish chegarasi (None → LOG_ROTATION env → 20 MB)
        retention: saqlash muddati (None → LOG_RETENTION env → 30 days)
        console:   stderr sink ham qo'shilsinmi

    Returns:
        `agent.log` faylining to'liq yo'li.
    """
    global _default_sink_removed

    d = get_log_dir(log_dir)
    lvl = (level or os.getenv("LOG_LEVEL") or _DEFAULT_LEVEL).upper()
    rot = rotation or os.getenv("LOG_ROTATION") or _DEFAULT_ROTATION
    ret = retention or os.getenv("LOG_RETENTION") or _DEFAULT_RETENTION

    # Avvalgi chaqiruvdan qolgan sink'lar (idempotentlik)
    for sid in _SINK_IDS:
        try:
            logger.remove(sid)
        except ValueError:
            pass  # allaqachon olib tashlangan
    _SINK_IDS.clear()

    if console and not _default_sink_removed:
        # Loguru default handler'i har doim id=0. U bo'lmasa ValueError —
        # bu normal (kimdir avval logger.remove() chaqirgan).
        try:
            logger.remove(0)
        except ValueError:
            pass
        _default_sink_removed = True

    if console:
        _SINK_IDS.append(
            logger.add(sys.stderr, level=lvl, backtrace=True, diagnose=False)
        )

    agent_log = d / _AGENT_LOG
    _SINK_IDS.append(
        logger.add(
            agent_log,
            level=lvl,
            format=_FILE_FORMAT,
            rotation=rot,
            retention=ret,
            compression="zip",
            encoding="utf-8",   # Windows: o'zbekcha matn + emoji cp1251'da buziladi
            enqueue=True,       # health server / telegram polling / watcher threadlari
            backtrace=True,
            diagnose=False,     # XAVFSIZLIK: parol va API kalit lokal o'zgaruvchilarda
        )
    )

    error_log = d / _ERROR_LOG
    _SINK_IDS.append(
        logger.add(
            error_log,
            level="ERROR",
            format=_FILE_FORMAT,
            rotation=rot,
            retention=_ERROR_RETENTION,
            compression="zip",
            encoding="utf-8",
            enqueue=True,
            backtrace=True,
            diagnose=False,
        )
    )

    return agent_log


def shutdown_logging() -> None:
    """Bu modul qo'shgan sink'larni yopadi (queue'ni flush qiladi).

    `enqueue=True` bo'lgani uchun yozuv fon thread orqali ketadi. Faylni
    darhol o'qish kerak bo'lsa (test yoki clean shutdown) avval shu chaqiriladi.
    """
    for sid in _SINK_IDS:
        try:
            logger.remove(sid)
        except ValueError:
            pass
    _SINK_IDS.clear()
