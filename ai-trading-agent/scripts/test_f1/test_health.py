"""F1-4 test: Healthcheck endpoint integration tests.

Tasks.md F1-4 acceptance criteria, summarized:
    "Bot ichki holatini /health, /health/live, /health/ready endpoint'lari
     orqali ochib beradi. HealthState dataclass agent va FastAPI thread
     orasida shared. Uvicorn daemon thread'da main loop'ni bloklamasdan."

Tekshiruvlar (10 ta, 4 ta guruh):

Group A — endpoint shapes (fastapi.testclient.TestClient):
  A1. GET /health on fresh HealthState() -> 200, 7 expected keys, status="initializing"
  A2. GET /health/live on initializing -> 200, status="initializing"
  A3. GET /health/ready when mt5_connected=False -> 503, status="not-ready"

Group B — status transitions (TestClient):
  B4. mt5_connected=True + last_tick_at=now -> /health status="ok"
  B5. mt5_connected=True + last_tick_at=now-10min -> /health/live 503, status="dead"
  B6. mt5_connected=False + recent tick -> /health status="degraded"
  B7. is_paused=True + healthy tick -> /health status="degraded"

Group C — agent integration (sim mode / mocked mt5, no real MT5 connection):
  C8. TraderAgent with HealthState + sim-mode mt5 -> _update_health_state()
      populates last_tick_at, mt5_connected, open_positions.
  C9. TraderAgent(config) without health_state -> _update_health_state() returns
      silently (no crash, back-compat).

Group D — threaded uvicorn server smoke (real HTTP, OS-assigned port):
  D10. start_health_server_in_thread() -> GET via urllib succeeds, mutating
       state.mt5_connected reflects in subsequent request (proves shared state).

Test izolyatsiyasi:
  - HealthState() in-memory only — diskka tegmaydi
  - .env, .env.example, data/state/* SHA256 boshida va oxirida solishtiriladi
  - apps/api/main.py BIR MARTA HAM import qilinmaydi (interactive onboarding'ni
    qo'zg'atmaslik uchun). Faqat health.py + agent.py + mt5_connector.py
  - Real MT5 connect() chaqirilmaydi: agent.mt5._sim_mode = True qo'lda yoqamiz

Run:
    cd ai-trading-agent
    python scripts/test_f1/test_health.py
"""
import hashlib
import json
import socket
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

# loyiha rootini sys.path'ga qo'shish
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from apps.api.src.agents.trader.health import (
    HealthState,
    _compute_status,
    _is_tick_stale,
    create_app,
    start_health_server_in_thread,
)


PASSED = 0
FAILED = 0


# ── Tamper-detection: state fayllar va .env tegmasligi shart ────────────────
def _sha256_of(p: Path) -> str | None:
    if not p.exists():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()


_GUARDED = {
    ".env":               ROOT / ".env",
    ".env.example":       ROOT / ".env.example",
    "trade_meta.json":    ROOT / "apps" / "data" / "state" / "trade_meta.json",
    "openclaw.db":        ROOT / "apps" / "data" / "state" / "openclaw.db",
}
_SHA_BEFORE: dict[str, str | None] = {k: _sha256_of(p) for k, p in _GUARDED.items()}


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


# ── Expected /health JSON keys ──────────────────────────────────────────────
_EXPECTED_HEALTH_KEYS = {
    "status",
    "uptime_sec",
    "mt5_connected",
    "last_tick_age_sec",
    "open_positions",
    "daily_pnl_pct",
    "is_paused",
}


print("\n=== F1-4: Healthcheck endpoint integration ===")


# ─────────────────── Group A — endpoint shapes ──────────────────────────────

@test("A1. GET /health on fresh HealthState() -> 200, all 7 keys, status=initializing")
def _():
    state = HealthState()
    app = create_app(state)
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200, f"status code != 200: {r.status_code}"
    body = r.json()
    actual_keys = set(body.keys())
    missing = _EXPECTED_HEALTH_KEYS - actual_keys
    assert not missing, f"missing keys: {missing}"
    extra = actual_keys - _EXPECTED_HEALTH_KEYS
    assert not extra, f"unexpected extra keys: {extra}"
    assert body["status"] == "initializing", (
        f"status='{body['status']}', expected 'initializing'"
    )
    # last_tick_at None bo'lgani uchun age None bo'lishi kerak
    assert body["last_tick_age_sec"] is None, (
        f"last_tick_age_sec={body['last_tick_age_sec']}, expected None"
    )


@test("A2. GET /health/live on initializing -> 200, status=initializing")
def _():
    state = HealthState()
    app = create_app(state)
    client = TestClient(app)
    r = client.get("/health/live")
    assert r.status_code == 200, f"status code != 200: {r.status_code}"
    body = r.json()
    assert body["status"] == "initializing", f"status={body['status']}"
    assert body["last_tick_age_sec"] is None, f"age={body['last_tick_age_sec']}"


@test("A3. GET /health/ready when mt5_connected=False -> 503, status=not-ready")
def _():
    state = HealthState()  # mt5_connected default = False
    app = create_app(state)
    client = TestClient(app)
    r = client.get("/health/ready")
    assert r.status_code == 503, f"status code != 503: {r.status_code}"
    body = r.json()
    assert body["status"] == "not-ready", f"status={body['status']}"
    assert body["mt5_connected"] is False


# ─────────────────── Group B — status transitions ───────────────────────────

@test("B4. mt5_connected=True + last_tick_at=now -> /health status=ok")
def _():
    state = HealthState()
    state.mt5_connected = True
    state.last_tick_at = datetime.now(timezone.utc)
    app = create_app(state)
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok", f"status={body['status']}, expected 'ok'"
    assert body["mt5_connected"] is True


@test("B5. mt5_connected=True + tick 10min old -> /health/live 503, status=dead")
def _():
    state = HealthState()
    state.mt5_connected = True
    state.scan_interval_sec = 30  # threshold = max(60, 30*4) = 120s
    state.last_tick_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    # sanity: _is_tick_stale should agree
    assert _is_tick_stale(state), "_is_tick_stale must return True at 10min old"
    app = create_app(state)
    client = TestClient(app)
    r = client.get("/health/live")
    assert r.status_code == 503, f"expected 503, got {r.status_code}"
    body = r.json()
    assert body["status"] == "dead", f"status={body['status']}, expected 'dead'"
    assert body["last_tick_age_sec"] is not None
    assert body["last_tick_age_sec"] >= 600 - 5, (
        f"age={body['last_tick_age_sec']}, expected ~600s"
    )


@test("B6. mt5_connected=False + recent tick -> /health status=degraded")
def _():
    state = HealthState()
    state.mt5_connected = False
    state.last_tick_at = datetime.now(timezone.utc)
    # sanity via private helper
    assert _compute_status(state) == "degraded", (
        f"_compute_status returned '{_compute_status(state)}', expected 'degraded'"
    )
    app = create_app(state)
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded", f"status={body['status']}"


@test("B7. is_paused=True + healthy tick -> /health status=degraded")
def _():
    state = HealthState()
    state.mt5_connected = True
    state.last_tick_at = datetime.now(timezone.utc)
    state.is_paused = True
    app = create_app(state)
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded", (
        f"status={body['status']}, is_paused must trigger 'degraded'"
    )
    assert body["is_paused"] is True


# ─────────────────── Group C — agent integration ────────────────────────────

def _build_minimal_agent(health_state):
    """
    TraderAgent'ni real MT5 connect() chaqirmasdan quradi.
    config faqat env vars / defaultlar bilan, .env'ni o'qimaslik uchun
    _env_file=None.
    """
    from apps.api.src.agents.trader.agent import TraderAgent
    from apps.api.src.agents.trader.models.config import TradingConfig

    cfg = TradingConfig(_env_file=None)
    agent = TraderAgent(cfg, health_state=health_state)

    # MT5'ni "sim mode"ga keltirish: is_connected() True qaytaradi,
    # get_open_positions() bo'sh ro'yxat. Real terminalga ulanish yo'q.
    agent.mt5 = MagicMock()
    agent.mt5.is_connected.return_value = True
    agent.mt5.get_open_positions.return_value = []
    return agent


@test("C8. TraderAgent._update_health_state() populates fields (with mocked mt5)")
def _():
    state = HealthState()
    before = datetime.now(timezone.utc)
    agent = _build_minimal_agent(state)
    agent._update_health_state()

    assert state.last_tick_at is not None, "last_tick_at still None after update"
    # last_tick_at >= before (vaqt o'tibdi)
    assert state.last_tick_at >= before - timedelta(seconds=1), (
        f"last_tick_at={state.last_tick_at} < before={before}"
    )
    assert state.mt5_connected is True, (
        f"mt5_connected={state.mt5_connected}, expected True (mock)"
    )
    assert isinstance(state.open_positions, int), (
        f"open_positions type={type(state.open_positions).__name__}, expected int"
    )
    assert state.open_positions == 0, f"open_positions={state.open_positions}"


@test("C9. TraderAgent(config) without health_state -> _update_health_state silent")
def _():
    from apps.api.src.agents.trader.agent import TraderAgent
    from apps.api.src.agents.trader.models.config import TradingConfig

    cfg = TradingConfig(_env_file=None)
    agent = TraderAgent(cfg)  # NO health_state kwarg
    assert agent.health_state is None, "health_state must default to None"

    # Hech qanday exception bo'lmasligi shart
    try:
        agent._update_health_state()
    except Exception as e:  # noqa: BLE001
        raise AssertionError(
            f"_update_health_state() crashed with health_state=None: "
            f"{type(e).__name__}: {e}"
        )


# ─────────────────── Group D — threaded uvicorn smoke ───────────────────────

def _allocate_free_port() -> int:
    """OS'dan bo'sh portni so'rab olish (kichik race window — testlar uchun OK)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@test("D10. threaded uvicorn: shared HealthState mutation visible via real HTTP")
def _():
    state = HealthState()
    state.mt5_connected = False
    port = _allocate_free_port()
    start_health_server_in_thread(state, host="127.0.0.1", port=port)
    # Server start ozgina vaqt oladi
    time.sleep(1.5)

    url = f"http://127.0.0.1:{port}/health"

    # Birinchi so'rov: mt5_connected=False bo'lishi kerak
    with urllib.request.urlopen(url, timeout=3.0) as r:
        assert r.status == 200, f"first GET status={r.status}"
        body1 = json.loads(r.read().decode("utf-8"))
    assert body1["mt5_connected"] is False, (
        f"first GET mt5_connected={body1['mt5_connected']}, expected False"
    )
    # Shape ham real serverda to'g'ri bo'lishi kerak
    missing = _EXPECTED_HEALTH_KEYS - set(body1.keys())
    assert not missing, f"real-server response missing keys: {missing}"

    # State'ni mutatsiya qilamiz — shared reference orqali handler ham
    # darhol yangi qiymatni ko'rishi shart
    state.mt5_connected = True
    time.sleep(0.1)

    with urllib.request.urlopen(url, timeout=3.0) as r:
        assert r.status == 200, f"second GET status={r.status}"
        body2 = json.loads(r.read().decode("utf-8"))
    assert body2["mt5_connected"] is True, (
        f"shared state mutation NOT visible — second GET mt5_connected="
        f"{body2['mt5_connected']}, expected True"
    )
    # Daemon thread o'z-o'zidan exit qiladi process tugaganda


# ─────────────────── Summary + tamper check ─────────────────────────────────

print(f"\nResult: {PASSED} passed, {FAILED} failed")

# Asosiy state fayllar tegmaganmi?
_TAMPERED: list[str] = []
for name, p in _GUARDED.items():
    before = _SHA_BEFORE[name]
    after = _sha256_of(p)
    if before != after:
        _TAMPERED.append(f"{name}: {before} -> {after}")

if _TAMPERED:
    print("\n[FATAL] state files were MODIFIED during tests:")
    for line in _TAMPERED:
        print(f"  {line}")
    sys.exit(2)
else:
    summary_bits = []
    for name, sha in _SHA_BEFORE.items():
        if sha is None:
            summary_bits.append(f"{name}=<absent>")
        else:
            summary_bits.append(f"{name}={sha[:8]}")
    print("State files unchanged: " + ", ".join(summary_bits))

sys.exit(0 if FAILED == 0 else 1)
