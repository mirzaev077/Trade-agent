from datetime import time
import pytz

# F2-1: clock injection — backtest VirtualClock yoki RealClock'dan vaqt o'qiladi
from apps.api.src.agents.trader.core.clock import get_clock


UTC = pytz.UTC

SESSIONS = {
    "sydney":       (time(22, 0), time(23, 59)),
    "asia":         (time(0,  0), time(6,  0)),
    "london_pre":   (time(6,  0), time(8,  0)),
    "london":       (time(8,  0), time(12, 0)),
    "overlap":      (time(12, 0), time(16, 0)),
    "newyork":      (time(13, 0), time(22, 0)),
}

KILL_ZONES = ["asia", "london", "overlap", "newyork"]


def get_current_session() -> str:
    now = get_clock().now().time()
    if time(13, 0) <= now <= time(16, 0):
        return "overlap"
    if time(8, 0) <= now < time(13, 0):
        return "london"
    if time(13, 0) < now <= time(22, 0):
        return "newyork"
    if time(6, 0) <= now < time(8, 0):
        return "london_pre"
    if time(0, 0) <= now < time(6, 0):
        return "asia"
    return "sydney"


def is_kill_zone() -> bool:
    return get_current_session() in KILL_ZONES


def is_weekend_protection() -> bool:
    now = get_clock().now()
    if now.weekday() == 4 and now.hour >= 22:
        return True
    if now.weekday() in (5, 6):
        return True
    return False


def session_min_confluence() -> float:
    """Return minimum confluence score based on current session quality."""
    session = get_current_session()
    if session in ("overlap",):
        return 5.0
    if session in ("london", "newyork"):
        return 5.5
    if session in ("london_pre",):
        return 5.5
    if session in ("asia",):
        return 6.0
    return 6.5


def get_session_name() -> str:
    return get_current_session()
