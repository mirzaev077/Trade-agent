"""
NewsFilter — high-impact economic news blackout.
Forex Factory public calendar JSON (no API key needed).
Cache: 1 hour. Fallback: allow trading (fail-open).
"""
from datetime import datetime, timedelta, timezone
from loguru import logger

try:
    import aiohttp
    _AIOHTTP_OK = True
except ImportError:
    _AIOHTTP_OK = False

_cache: dict = {"data": [], "fetched_at": None}
_CACHE_TTL_SEC = 3600          # 1 soat
_FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Faqat USD va XAU impactli eventlar
_HIGH_CURRENCIES = {"USD", "XAU"}


async def _fetch_calendar() -> list:
    if not _AIOHTTP_OK:
        return []
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5)
        ) as s:
            async with s.get(_FF_URL) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
    except Exception as e:
        logger.debug(f"NewsFilter fetch error: {e}")
    return []


def _parse_event_time(date_str: str) -> datetime | None:
    """ISO 8601 yoki 'YYYY-MM-DDTHH:MM:SSZ' formatini UTC datetime ga aylantir."""
    if not date_str:
        return None
    try:
        # Python 3.11+ fromisoformat Z suffix qo'llab-quvvatlaydi
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        # tz-aware UTC sifatida saqlash (F2-1: DTZ-clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except Exception:
        return None


async def has_high_impact_news(window_min: int = 30) -> bool:
    """
    Joriy vaqtdan ±window_min daqiqada high-impact USD/XAU news bormi?
    Ha → True (trade o'tkazib yubor).
    Xato yoki ma'lumot yo'q → False (trading davom etadi).

    Eslatma: news fetch async + real-time API call. Backtest'da bu funksiya
    chaqirilmaydi (tarixiy ma'lumot ko'chirib o'tkazadi). Shuning uchun
    real wall-clock'dan foydalanish xavfsiz — clock injection kerakmas.
    """
    global _cache
    now = datetime.now(timezone.utc)

    # Cache yangilash
    if (
        _cache["fetched_at"] is None
        or (now - _cache["fetched_at"]).total_seconds() > _CACHE_TTL_SEC
    ):
        data = await _fetch_calendar()
        _cache = {"data": data, "fetched_at": now}
        logger.debug(f"NewsFilter: {len(data)} events cached")

    window = timedelta(minutes=window_min)

    for event in _cache.get("data", []):
        # Country filtr
        if event.get("country", "").upper() not in _HIGH_CURRENCIES:
            continue
        # Impact: faqat "High" / "red" (FF da "red" = high)
        impact = event.get("impact", "").lower()
        if impact not in ("high", "red"):
            continue
        title = event.get("title", "N/A")
        event_dt = _parse_event_time(event.get("date", ""))
        if event_dt is None:
            continue
        diff_sec = abs((event_dt - now).total_seconds())
        if diff_sec <= window.total_seconds():
            logger.warning(
                f"NEWS BLACKOUT: '{title}' "
                f"{(event_dt - now).total_seconds()/60:+.0f}min UTC"
            )
            return True

    return False


def get_next_news_str() -> str:
    """Log uchun: keyingi high-impact event nomini qaytaradi."""
    now = datetime.now(timezone.utc)
    upcoming = []
    for ev in _cache.get("data", []):
        if ev.get("country", "").upper() not in _HIGH_CURRENCIES:
            continue
        if ev.get("impact", "").lower() not in ("high", "red"):
            continue
        dt = _parse_event_time(ev.get("date", ""))
        if dt and dt > now:
            upcoming.append((dt, ev.get("title", "?")))
    if not upcoming:
        return "no upcoming news"
    upcoming.sort()
    dt, title = upcoming[0]
    mins = int((dt - now).total_seconds() / 60)
    return f"next={title!r} in {mins}m"
