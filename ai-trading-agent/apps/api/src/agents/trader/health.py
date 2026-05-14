"""
F1-4: Healthcheck endpoint — bot ichki holatini HTTP orqali ochib beradi.

Uchta endpoint:
  GET /health          → to'liq holat JSON (status, uptime, mt5, positions, pnl)
  GET /health/live     → Kubernetes liveness — main loop hayotmi? (last_tick_age)
  GET /health/ready    → Kubernetes readiness — MT5 ulanganmi?

`HealthState` dataclass agent va uvicorn thread o'rtasida reference orqali
ulashiladi (primitive atomik write — alohida lock kerak emas).

Watchdog (scripts/watchdog.ps1) /health/live'ni har 60s'da chaqiradi.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Thread
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from loguru import logger
import uvicorn


DEFAULT_PORT = 8080
DEFAULT_HOST = "127.0.0.1"   # faqat lokal — production'da reverse proxy orqali


@dataclass
class HealthState:
    """
    Bot holati — agent yangilaydi, FastAPI handler o'qiydi.

    Primitive maydonlar (bool, int, float, datetime) CPython GIL ostida atomik
    yoziladi, alohida lock kerak emas.
    """
    started_at:        datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_tick_at:      Optional[datetime] = None
    mt5_connected:     bool  = False
    open_positions:    int   = 0
    daily_pnl_pct:     float = 0.0
    is_paused:         bool  = False   # consecutive loss / DD breaker / news blackout
    scan_interval_sec: int   = 30      # _compute_status() chegarasi uchun


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _tick_age_sec(state: HealthState) -> Optional[float]:
    if state.last_tick_at is None:
        return None
    return (_now_utc() - state.last_tick_at).total_seconds()


def _is_tick_stale(state: HealthState) -> bool:
    """Last tick juda eskimi? Threshold = max(60, 4 * scan_interval)."""
    age = _tick_age_sec(state)
    if age is None:
        return False  # hali boshlanmagan — stale emas, initializing
    threshold = max(60, state.scan_interval_sec * 4)
    return age > threshold


def _compute_status(state: HealthState) -> str:
    """
      initializing → bot endi ishga tushdi, hali tick yo'q
      down         → main loop o'lgan (last_tick juda eski)
      degraded     → MT5 uzilgan yoki pause'da
      ok           → hammasi yaxshi
    """
    if state.last_tick_at is None:
        return "initializing"
    if _is_tick_stale(state):
        return "down"
    if not state.mt5_connected or state.is_paused:
        return "degraded"
    return "ok"


def create_app(state: HealthState) -> FastAPI:
    app = FastAPI(
        title="OpenClaw Healthcheck",
        version="1.0.0",
        docs_url=None,    # Swagger UI o'chirilgan (yengil endpoint)
        redoc_url=None,
    )
    app.state.health = state

    @app.get("/health")
    def health():
        s: HealthState = app.state.health
        now = _now_utc()
        age = _tick_age_sec(s)
        return {
            "status":             _compute_status(s),
            "uptime_sec":         int((now - s.started_at).total_seconds()),
            "mt5_connected":      bool(s.mt5_connected),
            "last_tick_age_sec":  int(age) if age is not None else None,
            "open_positions":     int(s.open_positions),
            "daily_pnl_pct":      round(float(s.daily_pnl_pct), 2),
            "is_paused":          bool(s.is_paused),
        }

    @app.get("/health/live")
    def live():
        """Kubernetes liveness: main loop hayotmi?"""
        s: HealthState = app.state.health
        age = _tick_age_sec(s)
        if age is None:
            # Hali ishga tushgan — initializing, lekin alive
            return {"status": "initializing", "last_tick_age_sec": None}
        if _is_tick_stale(s):
            return JSONResponse(
                {"status": "dead", "last_tick_age_sec": int(age)},
                status_code=503,
            )
        return {"status": "alive", "last_tick_age_sec": int(age)}

    @app.get("/health/ready")
    def ready():
        """Kubernetes readiness: yangi trafikni qabul qila olamizmi? (MT5 ulanganmi?)"""
        s: HealthState = app.state.health
        if not s.mt5_connected:
            return JSONResponse(
                {"status": "not-ready", "mt5_connected": False},
                status_code=503,
            )
        return {"status": "ready", "mt5_connected": True}

    return app


def start_health_server_in_thread(
    state: HealthState,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> Thread:
    """
    FastAPI'ni daemon thread'da ishga tushiradi. Asosiy bot loop blokrlanmaydi.
    Returns: thread objekti (caller .join() qila oladi, lekin daemon=True'da kerakmas).
    """
    app = create_app(state)

    def _run() -> None:
        try:
            cfg = uvicorn.Config(
                app, host=host, port=port,
                log_level="warning",
                access_log=False,
                lifespan="off",       # FastAPI lifespan eventi kerak emas, oddiy thread
            )
            server = uvicorn.Server(cfg)
            server.run()
        except Exception as e:  # noqa: BLE001
            logger.error(f"[healthcheck] uvicorn crash: {e}")

    t = Thread(target=_run, daemon=True, name="healthcheck")
    t.start()
    logger.info(f"[healthcheck] listening on http://{host}:{port}")
    return t
