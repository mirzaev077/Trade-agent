"""
TelegramBot — asinxron xabar yuborish + inbound komandalar polling.
BOT_TOKEN va CHAT_ID .env dan o'qiladi.
Xato bo'lsa jimgina o'tkazib yuboradi (trading to'xtatilmaydi).

F3 (2026-05-23): inbound command polling.
  • `register_command("status", fn)` — `/status` keladi → `fn(args)` qaytargan
    matnni javob qiladi.
  • `start_polling(interval=2.0)` — daemon thread'da getUpdates loop.
  • `stop_polling()` — graceful.
  • Auth: faqat _CHAT_ID ga teng `from.id` dan keladigan xabarlar qabul qilinadi.
"""
import os
import json
import threading
import time
import urllib.request
import urllib.error
from typing import Callable
from loguru import logger

try:
    import aiohttp
    _OK = True
except ImportError:
    _OK = False

_TOKEN   = ""
_CHAT_ID = ""
_BASE    = "https://api.telegram.org/bot{token}/sendMessage"
_ENABLED = False

# F3: inbound command infrastructure
_HANDLERS: dict[str, Callable[[str], str]] = {}
_LAST_UPDATE_ID: int = 0
_POLL_STOP: threading.Event = threading.Event()
_POLL_THREAD: threading.Thread | None = None


def init(token: str, chat_id: str):
    global _TOKEN, _CHAT_ID, _ENABLED
    _TOKEN   = token.strip()
    _CHAT_ID = chat_id.strip()
    _ENABLED = bool(_TOKEN and _CHAT_ID and _OK)
    if _ENABLED:
        logger.info(f"Telegram bot yoqildi (chat={_CHAT_ID})")
    else:
        logger.debug("Telegram bot o'chirilgan (token yoki chat_id yo'q)")


async def send(text: str, parse_mode: str = "HTML"):
    if not _ENABLED:
        return
    url = _BASE.format(token=_TOKEN)
    payload = {"chat_id": _CHAT_ID, "text": text, "parse_mode": parse_mode}
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5)
        ) as s:
            await s.post(url, json=payload)
    except Exception as e:
        logger.debug(f"Telegram send error: {e}")


async def notify_order(action: str, direction: str, symbol: str,
                       entry: float, sl: float, tp1: float,
                       lot: float, label: str, mode: str, pnl: float = 0.0):
    """Limit/Market order ochildi yoki yopildi."""
    icons = {"LIMIT": "📋", "MARKET": "⚡", "FILLED": "✅", "CLOSED": "🔒",
             "WIN": "💰", "LOSS": "🔴", "BE": "⚪"}
    icon = icons.get(action, "📊")
    dir_icon = "🟢" if direction.lower() == "buy" else "🔴"

    if action in ("WIN", "LOSS", "BE"):
        pnl_str = f"{'+'if pnl>=0 else ''}{pnl:.2f}$"
        msg = (
            f"{icon} <b>{action}</b> {dir_icon} {symbol}\n"
            f"PnL: <b>{pnl_str}</b>\n"
            f"Zone: {label}"
        )
    elif action == "FILLED":
        msg = (
            f"{icon} <b>FILLED</b> {dir_icon} {direction.upper()} {symbol}\n"
            f"Entry: <b>{entry:.2f}</b>  SL: {sl:.2f}  TP: {tp1:.2f}\n"
            f"Lot: {lot}  Zone: {label} [{mode}]"
        )
    else:
        msg = (
            f"{icon} <b>{action}</b> {dir_icon} {direction.upper()} {symbol}\n"
            f"Entry: <b>{entry:.2f}</b>  SL: {sl:.2f}  TP1: {tp1:.2f}\n"
            f"Lot: {lot}  Zone: {label} [{mode}]"
        )
    await send(msg)


async def notify_news(title: str, mins: int):
    await send(f"📰 <b>NEWS BLACKOUT</b>\n{title}\n<i>{mins:+d} daqiqa</i>")


async def notify_dd_breaker(dd_pct: float, balance: float):
    await send(
        f"🚨 <b>DRAWDOWN CIRCUIT BREAKER</b>\n"
        f"Drawdown: <b>-{dd_pct:.1f}%</b>\n"
        f"Balans: ${balance:.2f}\n"
        f"<i>Trading to'xtatildi!</i>"
    )


async def notify_daily_target(pnl_pct: float, pnl_usd: float):
    await send(
        f"🎯 <b>DAILY TARGET HIT!</b>\n"
        f"Foyda: <b>+{pnl_pct:.1f}%</b> (${pnl_usd:.2f})\n"
        f"<i>Bugungi trading yakunlandi.</i>"
    )


async def notify_pending_adjustment(
    param: str,
    old_value,
    new_value,
    reason: str,
    adjustment_id: str,
):
    """Self-learner parametr o'zgartirishni taklif qildi — admin approval kerak."""
    def _fmt(v):
        if v is None:
            return "—"
        try:
            return f"{float(v):.4f}".rstrip("0").rstrip(".")
        except (TypeError, ValueError):
            return str(v)

    reason_short = (reason or "")[:200]
    msg = (
        f"⏳ <b>PENDING ADJUSTMENT</b>\n"
        f"Param: <b>{_html_escape(str(param))}</b>\n"
        f"Old: <b>{_fmt(old_value)}</b> → New: <b>{_fmt(new_value)}</b>\n"
        f"Sabab: <i>{_html_escape(reason_short)}</i>\n"
        f"ID: <code>{adjustment_id}</code>\n"
        f"<i>24 soat ichida approve/reject qiling:</i>\n"
        f"<code>python -m apps.api.src.tools.admin_cli approve {adjustment_id}</code>"
    )
    await send(msg)


async def notify_status(symbol: str, mode: str, want: str,
                        w1: str, d1: str, h4: str, h1: str,
                        pending: int, wins: int, losses: int, balance: float):
    wr = f"{wins/(wins+losses)*100:.0f}%" if wins + losses > 0 else "N/A"
    dir_icon = "🟢" if want == "buy" else ("🔴" if want == "sell" else "⚪")
    await send(
        f"📊 <b>{symbol}</b> [{mode}] {dir_icon} {want.upper()}\n"
        f"W1={w1} D1={d1} H4={h4} H1={h1}\n"
        f"Pending: {pending} | WR: {wr} ({wins}W/{losses}L)\n"
        f"Balance: ${balance:.2f}"
    )


# ── SYNC notify funksiyalari (sys.excepthook / signal handler uchun) ──────────
#
# sys.excepthook va signal handler async kontekstda chaqirilmaydi, shu sababli
# bu funksiyalar urllib.request orqali sync HTTP POST yuboradi.
# Telegram unavailable bo'lsa — logger.error qiladi, exception otmaydi.

_TG_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
_MAX_TG_LEN = 4000  # Telegram limit 4096, biroz buffer qoldiramiz


def _resolve_token_chat() -> tuple[str, str]:
    """
    Token va chat_id ni topish — avval module globals, keyin env vars.
    Crash handler agent init() dan oldin ham ishlashi uchun env fallback shart.
    """
    token = _TOKEN or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = _CHAT_ID or os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return token, chat_id


def _send_sync(text: str, parse_mode: str = "HTML") -> None:
    """
    Sync xabar yuborish (urllib orqali). Hech qachon exception otmaydi.
    """
    token, chat_id = _resolve_token_chat()
    if not (token and chat_id):
        logger.error(
            "[telegram_sync] Token yoki chat_id topilmadi — xabar yuborilmadi:\n"
            f"{text[:200]}"
        )
        return

    if len(text) > _MAX_TG_LEN:
        text = text[: _MAX_TG_LEN - 20] + "\n…(qisqartirildi)"

    url = _TG_API_URL.format(token=token)
    payload = json.dumps(
        {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()  # drain
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        logger.error(f"[telegram_sync] HTTP xato: {e} | matn: {text[:200]}")
    except Exception as e:  # noqa: BLE001 — crash ichida crash bo'lmasligi shart
        logger.error(f"[telegram_sync] Kutilmagan xato: {e}")


def _html_escape(s: str) -> str:
    """Telegram HTML uchun minimal escape."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def notify_crash(exc_type, exc_value, tb_str: str) -> None:
    """
    Uncaught exception — sys.excepthook dan chaqiriladi.
    KATTA EMOJI 🚨🚨🚨 + exception turi + qiymati + traceback oxiri.
    """
    try:
        type_name = getattr(exc_type, "__name__", str(exc_type))
        value_str = _html_escape(str(exc_value))[:500]

        # Traceback oxirgi 30 satrini olish
        tb_lines = tb_str.strip().splitlines()
        if len(tb_lines) > 30:
            tb_lines = tb_lines[-30:]
        tb_short = _html_escape("\n".join(tb_lines))

        msg = (
            f"🚨🚨🚨 <b>BOT CRASH</b> 🚨🚨🚨\n"
            f"<b>{_html_escape(type_name)}</b>: {value_str}\n\n"
            f"<pre>{tb_short}</pre>"
        )
        _send_sync(msg)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[notify_crash] xato: {e}")


def notify_disconnect(duration_min: int) -> None:
    """MT5 uzilganda — qancha vaqt uzilgani (daqiqada)."""
    try:
        msg = (
            f"⚠️ <b>MT5 UZILDI</b> ⚠️\n"
            f"Davomiyligi: <b>{duration_min} daqiqa</b>\n"
            f"<i>Yangi order ochilmaydi — qayta ulanish kutilmoqda.</i>"
        )
        _send_sync(msg)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[notify_disconnect] xato: {e}")


def notify_recovered(downtime_min: int) -> None:
    """MT5 qaytadan ulandi — qancha vaqt downtime bo'lganini ko'rsatadi."""
    try:
        msg = (
            f"✅ <b>MT5 QAYTA ULANDI</b> ✅\n"
            f"Downtime: <b>{downtime_min} daqiqa</b>\n"
            f"<i>Trading davom etmoqda.</i>"
        )
        _send_sync(msg)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[notify_recovered] xato: {e}")


def notify_shutdown(reason: str) -> None:
    """Bot graceful shutdown (SIGTERM/SIGINT)."""
    try:
        reason_safe = _html_escape(str(reason))[:200]
        msg = (
            f"🛑 <b>BOT TO'XTATILDI</b> 🛑\n"
            f"Sabab: <b>{reason_safe}</b>"
        )
        _send_sync(msg)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[notify_shutdown] xato: {e}")


# ── F3: Inbound command polling ───────────────────────────────────────────────

_GETUPDATES_URL = "https://api.telegram.org/bot{token}/getUpdates"


def register_command(name: str, handler: Callable[[str], str]) -> None:
    """Komandani ro'yxatga olish.

    :param name: komanda nomi `/` belgisisiz (masalan: "status")
    :param handler: argument matnini (komandadan keyingi qism) qabul qiladi
                    va Telegram'ga qaytadigan matnni qaytaradi.
    """
    _HANDLERS[name.lower().lstrip("/")] = handler


def unregister_command(name: str) -> None:
    """Komandani olib tashlash (asosan testlar uchun)."""
    _HANDLERS.pop(name.lower().lstrip("/"), None)


def list_commands() -> list[str]:
    """Ro'yxatga olingan komandalar nomlari (asosan testlar uchun)."""
    return sorted(_HANDLERS.keys())


def _parse_command(text: str) -> tuple[str, str] | None:
    """`/cmd args` matnini (cmd, args) ga ajratadi. None — komanda emas."""
    if not text or not text.startswith("/"):
        return None
    body = text[1:].strip()
    if not body:
        return None
    # `/cmd@botname args` formatini ham qabul qilamiz
    head, _, rest = body.partition(" ")
    cmd = head.split("@", 1)[0].lower()
    return cmd, rest.strip()


def dispatch_command(text: str, from_chat_id: str | int) -> str | None:
    """Bitta inbound xabar uchun command dispatch.

    :return: javob matni yoki None (komanda noma'lum / auth fail / matn yo'q).
    """
    # 1) Auth — faqat ma'lum chat_id ruxsat etiladi
    try:
        if str(from_chat_id) != str(_CHAT_ID or "").strip():
            return None
    except Exception:  # noqa: BLE001
        return None

    parsed = _parse_command(text)
    if parsed is None:
        return None

    cmd, args = parsed
    handler = _HANDLERS.get(cmd)
    if handler is None:
        return f"❓ Noma'lum komanda: /{cmd}\nMavjud: " + ", ".join(
            f"/{c}" for c in list_commands()
        )

    try:
        return handler(args)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[telegram dispatch /{cmd}] handler xato: {e}")
        return f"⚠️ Komanda bajarilishda xato: {e}"


def _fetch_updates(timeout: int = 25) -> list[dict]:
    """getUpdates long-poll. _LAST_UPDATE_ID dan keyingi xabarlarni qaytaradi."""
    token, _ = _resolve_token_chat()
    if not token:
        return []
    url = _GETUPDATES_URL.format(token=token)
    params = {
        "offset": _LAST_UPDATE_ID + 1,
        "timeout": timeout,
        "allowed_updates": ["message"],
    }
    payload = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout + 5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        logger.debug(f"[telegram getUpdates] HTTP xato: {e}")
        return []
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[telegram getUpdates] parse xato: {e}")
        return []
    if not isinstance(data, dict) or not data.get("ok"):
        return []
    return data.get("result", []) or []


def _process_update(update: dict) -> None:
    """Bitta getUpdates result element'ini qayta ishlash."""
    global _LAST_UPDATE_ID
    uid = update.get("update_id")
    if isinstance(uid, int):
        _LAST_UPDATE_ID = max(_LAST_UPDATE_ID, uid)

    msg = update.get("message") or {}
    text = (msg.get("text") or "").strip()
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not text or chat_id is None:
        return

    reply = dispatch_command(text, chat_id)
    if reply:
        _send_sync(reply)


def _poll_loop(interval: float) -> None:
    """Daemon thread loop. _POLL_STOP set bo'lsa to'xtaydi."""
    logger.info("[telegram polling] boshlandi")
    while not _POLL_STOP.is_set():
        updates = _fetch_updates(timeout=25)
        for u in updates:
            try:
                _process_update(u)
            except Exception as e:  # noqa: BLE001
                logger.error(f"[telegram polling] process xato: {e}")
        # Long-poll'dan tashqari minimal interval
        _POLL_STOP.wait(timeout=max(0.1, interval))
    logger.info("[telegram polling] to'xtatildi")


def start_polling(interval: float = 2.0) -> None:
    """Background thread'da getUpdates loop'ni boshlash.

    Idempotent: ikkinchi marta chaqirilsa, log emas, no-op.
    """
    global _POLL_THREAD
    if not _ENABLED:
        logger.debug("[telegram polling] bot o'chirilgan — boshlanmaydi")
        return
    if _POLL_THREAD is not None and _POLL_THREAD.is_alive():
        return
    _POLL_STOP.clear()
    _POLL_THREAD = threading.Thread(
        target=_poll_loop, args=(interval,), name="telegram-poll", daemon=True
    )
    _POLL_THREAD.start()


def stop_polling(timeout: float = 5.0) -> None:
    """Polling thread'ni graceful to'xtatish."""
    global _POLL_THREAD
    _POLL_STOP.set()
    if _POLL_THREAD is not None and _POLL_THREAD.is_alive():
        _POLL_THREAD.join(timeout=timeout)
    _POLL_THREAD = None


def notify_pending_adjustment_sync(
    param: str,
    old_value,
    new_value,
    reason: str,
    adjustment_id: str,
) -> None:
    """Sync variant — self_learner (sync) ichidan chaqirish uchun."""
    try:
        def _fmt(v):
            if v is None:
                return "—"
            try:
                return f"{float(v):.4f}".rstrip("0").rstrip(".")
            except (TypeError, ValueError):
                return str(v)

        reason_short = _html_escape((reason or "")[:200])
        msg = (
            f"⏳ <b>PENDING ADJUSTMENT</b>\n"
            f"Param: <b>{_html_escape(str(param))}</b>\n"
            f"Old: <b>{_fmt(old_value)}</b> → New: <b>{_fmt(new_value)}</b>\n"
            f"Sabab: <i>{reason_short}</i>\n"
            f"ID: <code>{adjustment_id}</code>\n"
            f"<i>24 soat ichida approve/reject qiling:</i>\n"
            f"<code>python -m apps.api.src.tools.admin_cli approve {adjustment_id}</code>"
        )
        _send_sync(msg)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[notify_pending_adjustment_sync] xato: {e}")
