"""F1-2 test: TradingConfig + RiskConfig pydantic v2 strict validation

Tasks.md F1-2 acceptance criteria, verbatim:
    "Silent fallback olib tashlandi. Noto'g'ri env qiymat (masalan
     RISK_PER_TRADE=abc) startupda ValidationError'ga olib keladi."

Tekshiruvlar (15 ta):

Group A — bad-type rejection (silent-fallback removal; F1-2 headline):
  1. RISK_PER_TRADE=abc                     -> ValidationError
  2. MAX_POSITIONS=2.5 (float for int)      -> ValidationError
  3. SCAN_INTERVAL=not_a_number             -> ValidationError

Group B — out-of-bounds rejection:
  4. RISK_PER_TRADE=10.0 (> le=5.0)         -> ValidationError
  5. RISK_PER_TRADE=0    (not gt=0)         -> ValidationError
  6. MAX_POSITIONS=0     (< ge=1)           -> ValidationError
  7. MAX_POSITIONS=11    (> le=10)          -> ValidationError
  8. MIN_AI_CONFIDENCE=1.5 (> le=1.0)       -> ValidationError

Group C — cross-field invariants:
  9.  RISK_PER_TRADE=3.0 + DAILY_MAX_RISK=2.0   -> invariant (daily<risk)
  10. MAX_DRAWDOWN=3.0  + DAILY_MAX_RISK=5.0    -> invariant (dd<daily)
  11. RiskConfig(tp1=50, tp2=30, tp3=30)        -> tp_sum != 100

Group D — happy path + edge cases:
  12. Default construction succeeds, get_risk_config() yields valid RiskConfig
  13. Boundary values: risk_per_trade=5.0 (upper) + daily_max_risk=5.0 (==risk)
  14. TradingConfig.extra='ignore' — unknown env vars don't blow up
  15. RiskConfig.extra='forbid' — unknown kwargs raise

Test izolyatsiyasi:
  - os.environ snapshot/restore har test'da
  - TradingConfig(_env_file=None) — .env DISK'ga TEGILMAYDI, qiymat
    leak qilinmaydi (real .env'da MT5_LOGIN, CLAUDE_API_KEY bor)
  - .env SHA256 test boshida hisoblanadi va oxirida solishtiriladi

Run:
    cd ai-trading-agent
    python scripts/test_f1/test_config_validation.py
"""
import hashlib
import os
import sys
from pathlib import Path

# loyiha rootini sys.path'ga qo'shish
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pydantic import ValidationError

from apps.api.src.agents.trader.models.config import RiskConfig, TradingConfig


PASSED = 0
FAILED = 0


# ── .env tamper-detection (real .env tegmasligi shart) ──────────────────────
_ENV_PATH = ROOT / ".env"
_ENV_SHA_BEFORE: str | None = None
if _ENV_PATH.exists():
    _ENV_SHA_BEFORE = hashlib.sha256(_ENV_PATH.read_bytes()).hexdigest()


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


def _try_build_trading(env_overrides: dict):
    """TradingConfig'ni env vars bilan quradi.

    `_env_file=None` — pydantic-settings real .env'ni o'qimasligi shart,
    aks holda .env'dagi MT5_LOGIN, CLAUDE_API_KEY va boshqalar leak qiladi.

    Returns: (ok: bool, exc: Exception|None, model: TradingConfig|None)
    """
    old = dict(os.environ)
    # Loyiha .env env vars'ni shadow qiladi degan ishonchsizlik bo'lmasin —
    # _env_file=None ham, lekin os.environ'da bo'lsa pydantic-settings o'qiydi.
    # Shu sabab har testda CONTROL qilingan envni o'rnatamiz va boshqalarni
    # tegmaymiz (snapshot restore qaytaradi). Eslatma: os.environ.clear()
    # qilsak Windows muhitida BAT skriptlar va boshqa import'lar shikastlanishi
    # mumkin, shuning uchun maqsadli pop+update yondashuvi.
    try:
        # Test qiluvchi env keys (config maydonlari) — har test boshida
        # tozalanishi shart, aks holda oldingi testdan leak qoladi.
        _strip_config_env()
        os.environ.update({k: str(v) for k, v in env_overrides.items()})
        m = TradingConfig(_env_file=None)
        return True, None, m
    except Exception as e:
        return False, e, None
    finally:
        os.environ.clear()
        os.environ.update(old)


def _strip_config_env():
    """TradingConfig'ga aloqador barcha env keys'ni os.environ'dan o'chirish.

    Bu real shell environment'da MT5_LOGIN va h.k. bo'lsa, test natijalariga
    ta'sir qilmasligi uchun. case-insensitive — yuqori/kichik harflarni
    e'tibordan chiqarmasdan.
    """
    keys = {
        "MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER",
        "SYMBOL", "SCAN_INTERVAL",
        "RISK_PER_TRADE", "DAILY_MAX_RISK", "DAILY_PROFIT_TARGET",
        "MAX_POSITIONS", "MAX_TRADES_PER_DAY", "MAX_DRAWDOWN",
        "CLAUDE_API_KEY", "MIN_AI_CONFIDENCE",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "DATABASE_URL", "REDIS_URL", "SOCKET_URL",
    }
    for k in list(os.environ.keys()):
        if k.upper() in keys:
            del os.environ[k]


def _assert_validation_error(exc, where: str):
    assert exc is not None, f"{where}: expected ValidationError, got success"
    assert isinstance(exc, ValidationError), (
        f"{where}: expected ValidationError, got {type(exc).__name__}: {exc}"
    )


print("\n=== F1-2: TradingConfig / RiskConfig validation ===")


# ─────────────────── Group A: bad-type rejection ────────────────────────────

@test("A1. RISK_PER_TRADE=abc -> ValidationError (silent fallback removed)")
def _():
    ok, exc, _ = _try_build_trading({"RISK_PER_TRADE": "abc"})
    assert not ok, "build unexpectedly succeeded — silent fallback NOT removed"
    _assert_validation_error(exc, "RISK_PER_TRADE=abc")


@test("A2. MAX_POSITIONS=2.5 -> ValidationError (strict int, no float coercion)")
def _():
    ok, exc, model = _try_build_trading({"MAX_POSITIONS": "2.5"})
    # Note: pydantic v2 strict-int defaults to rejecting float-strings like "2.5".
    # We DO accept int-strings like "2" (lax string->int). The bar here is:
    # "2.5" must NOT silently become 2.
    if ok:
        raise AssertionError(
            f"MAX_POSITIONS='2.5' was silently coerced to {model.max_positions}; "
            f"expected ValidationError (strict numeric)"
        )
    _assert_validation_error(exc, "MAX_POSITIONS=2.5")


@test("A3. SCAN_INTERVAL=not_a_number -> ValidationError")
def _():
    ok, exc, _ = _try_build_trading({"SCAN_INTERVAL": "not_a_number"})
    assert not ok, "build unexpectedly succeeded with garbage SCAN_INTERVAL"
    _assert_validation_error(exc, "SCAN_INTERVAL=not_a_number")


# ─────────────────── Group B: out-of-bounds rejection ───────────────────────

@test("B4. RISK_PER_TRADE=10.0 (> le=5.0) -> ValidationError")
def _():
    ok, exc, _ = _try_build_trading({"RISK_PER_TRADE": "10.0"})
    assert not ok, "RISK_PER_TRADE=10.0 accepted (upper bound le=5.0 not enforced)"
    _assert_validation_error(exc, "RISK_PER_TRADE=10.0")


@test("B5. RISK_PER_TRADE=0 (not gt=0) -> ValidationError")
def _():
    ok, exc, _ = _try_build_trading({"RISK_PER_TRADE": "0"})
    assert not ok, "RISK_PER_TRADE=0 accepted (gt=0 not enforced)"
    _assert_validation_error(exc, "RISK_PER_TRADE=0")


@test("B6. MAX_POSITIONS=0 (< ge=1) -> ValidationError")
def _():
    ok, exc, _ = _try_build_trading({"MAX_POSITIONS": "0"})
    assert not ok, "MAX_POSITIONS=0 accepted (ge=1 not enforced)"
    _assert_validation_error(exc, "MAX_POSITIONS=0")


@test("B7. MAX_POSITIONS=11 (> le=10) -> ValidationError")
def _():
    ok, exc, _ = _try_build_trading({"MAX_POSITIONS": "11"})
    assert not ok, "MAX_POSITIONS=11 accepted (le=10 not enforced)"
    _assert_validation_error(exc, "MAX_POSITIONS=11")


@test("B8. MIN_AI_CONFIDENCE=1.5 (> le=1.0) -> ValidationError")
def _():
    ok, exc, _ = _try_build_trading({"MIN_AI_CONFIDENCE": "1.5"})
    assert not ok, (
        "MIN_AI_CONFIDENCE=1.5 accepted — env binding missing OR upper bound "
        "le=1.0 not enforced. F1-2 fix: this field MUST now be env-bindable."
    )
    _assert_validation_error(exc, "MIN_AI_CONFIDENCE=1.5")


# ─────────────────── Group C: cross-field invariants ────────────────────────

@test("C9. RISK_PER_TRADE=3.0 + DAILY_MAX_RISK=2.0 -> invariant (daily<risk)")
def _():
    ok, exc, _ = _try_build_trading({
        "RISK_PER_TRADE": "3.0",
        "DAILY_MAX_RISK": "2.0",
    })
    assert not ok, "daily_max_risk < risk_per_trade was accepted"
    _assert_validation_error(exc, "daily<risk invariant")
    # Xato matnida tegishli field tilga olinishi kerak
    assert "daily_max_risk" in str(exc).lower() or "risk_per_trade" in str(exc).lower(), (
        f"invariant error message uninformative: {exc}"
    )


@test("C10. MAX_DRAWDOWN=3.0 + DAILY_MAX_RISK=5.0 -> invariant (dd<daily)")
def _():
    # Eslatma: risk_per_trade default 1.0 (DAILY_MAX_RISK=5.0'ga mos)
    ok, exc, _ = _try_build_trading({
        "DAILY_MAX_RISK": "5.0",
        "MAX_DRAWDOWN":   "3.0",
    })
    assert not ok, "max_drawdown < daily_max_risk was accepted"
    _assert_validation_error(exc, "dd<daily invariant")
    assert "max_drawdown" in str(exc).lower() or "daily_max_risk" in str(exc).lower(), (
        f"invariant error message uninformative: {exc}"
    )


@test("C11. RiskConfig(tp1=50, tp2=30, tp3=30) -> tp_sum != 100")
def _():
    try:
        RiskConfig(tp1_close_pct=50.0, tp2_close_pct=30.0, tp3_close_pct=30.0)
    except ValidationError as e:
        msg = str(e).lower()
        assert "100" in msg or "tp1" in msg or "tp2" in msg or "tp3" in msg, (
            f"tp_sum error message uninformative: {e}"
        )
        return
    except Exception as e:
        raise AssertionError(
            f"expected ValidationError, got {type(e).__name__}: {e}"
        )
    raise AssertionError(
        "RiskConfig(tp1=50, tp2=30, tp3=30) accepted; tp_sum invariant not enforced"
    )


# ─────────────────── Group D: happy path + edge cases ───────────────────────

@test("D12. Default TradingConfig builds; get_risk_config() returns valid RiskConfig")
def _():
    ok, exc, cfg = _try_build_trading({})
    assert ok, f"default TradingConfig failed: {exc}"
    rc = cfg.get_risk_config()
    assert isinstance(rc, RiskConfig)
    # default'lar TradingConfig <-> RiskConfig orasida mos
    assert rc.risk_per_trade == cfg.risk_per_trade == 1.0
    assert rc.daily_max_risk == cfg.daily_max_risk == 5.0
    assert rc.max_drawdown   == cfg.max_drawdown   == 10.0
    assert rc.max_positions  == cfg.max_positions  == 2
    # tp sum default ham 100
    tp_sum = rc.tp1_close_pct + rc.tp2_close_pct + rc.tp3_close_pct
    assert abs(tp_sum - 100.0) < 0.1, f"default tp_sum={tp_sum}"


@test("D13. Boundary values: risk=5.0 (le upper) + daily=5.0 (>=risk exactly)")
def _():
    # max_drawdown default 10.0 — daily=5.0'dan katta, OK
    ok, exc, cfg = _try_build_trading({
        "RISK_PER_TRADE": "5.0",
        "DAILY_MAX_RISK": "5.0",
    })
    assert ok, f"boundary values rejected: {exc}"
    assert cfg.risk_per_trade == 5.0
    assert cfg.daily_max_risk == 5.0


@test("D14. extra='ignore' on TradingConfig — unknown env vars accepted silently")
def _():
    ok, exc, cfg = _try_build_trading({
        "AUTO_APPLY_LEARNING":  "false",
        "OPENCLAW_STATE_DIR":   "/tmp/whatever",
        "SOME_RANDOM_UNUSED":   "xyz",
    })
    assert ok, (
        f"unknown env vars caused failure (extra='ignore' not honored): {exc}"
    )
    # Asosiy maydonlar default'da qoldi
    assert cfg.risk_per_trade == 1.0


@test("D15. extra='forbid' on RiskConfig — unknown kwarg rejected")
def _():
    try:
        RiskConfig(unknown_field_xyz=42)
    except ValidationError as e:
        # pydantic v2: "Extra inputs are not permitted" yoki shunga o'xshash
        msg = str(e).lower()
        assert "extra" in msg or "unknown_field_xyz" in msg or "not permitted" in msg, (
            f"extra='forbid' error message unexpected: {e}"
        )
        return
    except Exception as e:
        raise AssertionError(
            f"expected ValidationError, got {type(e).__name__}: {e}"
        )
    raise AssertionError(
        "RiskConfig(unknown_field_xyz=42) accepted; extra='forbid' not honored"
    )


# ─────────────────── Summary + .env tamper check ────────────────────────────

print(f"\nResult: {PASSED} passed, {FAILED} failed")

# .env DISK haqiqatan ham tegmaganini tasdiqlash
if _ENV_SHA_BEFORE is not None:
    sha_after = hashlib.sha256(_ENV_PATH.read_bytes()).hexdigest()
    if sha_after != _ENV_SHA_BEFORE:
        print(
            f"\n[FATAL] .env was MODIFIED during tests!\n"
            f"  before: {_ENV_SHA_BEFORE}\n"
            f"  after:  {sha_after}"
        )
        sys.exit(2)
    else:
        print(f".env SHA256 unchanged: {_ENV_SHA_BEFORE[:16]}...")

sys.exit(0 if FAILED == 0 else 1)
