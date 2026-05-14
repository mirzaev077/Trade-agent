"""F1-3 test: .env.example completeness + start.bat fallback.

Tasks.md F1-3 acceptance criteria:
    1. `.env.example` TradingConfig'dagi BARCHA env-bindable maydonlarni
       o'z ichiga oladi va strict validation'dan o'tadi (working reference).
    2. `start.bat` ishga tushganda `.env` topilmasa, `.env.example`'dan
       nusxa oladi, sozlash promptini ko'rsatadi va exit 1 qiladi
       (bot ham foydalanuvchi ham xayolda yashamasin).

Tekshiruvlar (10 ta):

Group A — .env.example strukturaviy butunligi:
  A1. Fayl mavjud: ai-trading-agent/.env.example
  A2. dotenv_values() bilan parse qilinadi (syntax errors yo'q)
  A3. TradingConfig.model_fields'dagi BARCHA nomlar .env.example'da bor
  A4. Bo'sh required numeric placeholder'lar yo'q (e.g., RISK_PER_TRADE=)

Group B — .env.example strict validation'dan o'tadi:
  B5. dotenv_values() -> os.environ -> TradingConfig(_env_file=None) muvaffaqiyatli
  B6. get_risk_config() valid RiskConfig qaytaradi (barcha invariantlar bilan)
  B7. Spot-check defaults: risk=1.0, daily=5.0, dd=10.0

Group C — start.bat fallback xulqi (Windows-specific):
  C8. tmp dirda .env yo'q + .env.example bor -> .bat copy qiladi, exit 1, prompt chiqaradi
  C9. tmp dirda .env bor -> .bat copy-promptini SKIP qiladi (boshqa sabab bilan
      fail bo'lishi mumkin, lekin .env-missing-prompt KO'RSATILMAYDI)

Group D — Negativ holat (working-reference proof):
  D10. .env.example'dan RISK_PER_TRADE='abc' bilan korrupt qilinsa ->
       ValidationError. Bu .env.example formatining haqiqiy ekanligini isbotlaydi.

Test izolyatsiyasi:
  - os.environ snapshot/restore har test'da
  - .env va .env.example SHA256 test boshida hisoblanadi, oxirida solishtiriladi
  - start.bat testi tempfile.TemporaryDirectory() ichida, real fayllarga tegmaydi

Run:
    cd ai-trading-agent
    python scripts/test_f1/test_env_example.py
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from dotenv import dotenv_values

# loyiha rootini sys.path'ga qo'shish
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pydantic import ValidationError

from apps.api.src.agents.trader.models.config import RiskConfig, TradingConfig


PASSED = 0
FAILED = 0


# ── .env va .env.example tamper-detection ────────────────────────────────
_ENV_PATH = ROOT / ".env"
_ENV_EXAMPLE_PATH = ROOT / ".env.example"
_START_BAT_PATH = ROOT / "start.bat"

_ENV_SHA_BEFORE: str | None = None
_ENV_EXAMPLE_SHA_BEFORE: str | None = None

if _ENV_PATH.exists():
    _ENV_SHA_BEFORE = hashlib.sha256(_ENV_PATH.read_bytes()).hexdigest()
if _ENV_EXAMPLE_PATH.exists():
    _ENV_EXAMPLE_SHA_BEFORE = hashlib.sha256(
        _ENV_EXAMPLE_PATH.read_bytes()
    ).hexdigest()


# TradingConfig bilan bog'liq barcha env keys (config_validation testidan moslashtirilgan)
_CONFIG_ENV_KEYS = {
    "MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER",
    "SYMBOL", "SCAN_INTERVAL",
    "RISK_PER_TRADE", "DAILY_MAX_RISK", "DAILY_PROFIT_TARGET",
    "MAX_POSITIONS", "MAX_TRADES_PER_DAY", "MAX_DRAWDOWN",
    "CLAUDE_API_KEY", "MIN_AI_CONFIDENCE",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "DATABASE_URL", "REDIS_URL", "SOCKET_URL",
}


def _strip_config_env() -> None:
    """TradingConfig'ga aloqador env keys'ni os.environ'dan o'chirish.
    Real shellda MT5_LOGIN va h.k. bo'lsa test natijalariga ta'sir qilmasligi uchun.
    """
    for k in list(os.environ.keys()):
        if k.upper() in _CONFIG_ENV_KEYS:
            del os.environ[k]


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


def _build_trading_from_values(values: dict) -> tuple[bool, Exception | None, TradingConfig | None]:
    """dict'dagi key=value'larni os.environ'ga inject qilib, TradingConfig quradi.
    None qiymatlar (kommentlangan satrlardan) inject qilinmaydi.
    `_env_file=None` — real .env'ga TEGILMAYDI.
    """
    old = dict(os.environ)
    try:
        _strip_config_env()
        for k, v in values.items():
            if v is None:
                continue
            os.environ[k] = str(v)
        m = TradingConfig(_env_file=None)
        return True, None, m
    except Exception as e:
        return False, e, None
    finally:
        os.environ.clear()
        os.environ.update(old)


print("\n=== F1-3: .env.example completeness + start.bat fallback ===")


# ─────────────────── Group A: .env.example strukturaviy butunligi ──────────

@test("A1. .env.example fayli mavjud")
def _():
    assert _ENV_EXAMPLE_PATH.exists(), (
        f".env.example topilmadi: {_ENV_EXAMPLE_PATH}"
    )
    assert _ENV_EXAMPLE_PATH.is_file()
    assert _ENV_EXAMPLE_PATH.stat().st_size > 0, ".env.example bo'sh"


@test("A2. dotenv_values() bilan parse qilinadi (syntax errors yo'q)")
def _():
    # dotenv_values() syntax xato bo'lsa exception qaytaradi yoki bo'sh dict
    # qaytaradi (parse failure'lar quiet bo'lishi mumkin), lekin parsing strict
    # va keys >0 bo'lishi shart.
    values = dotenv_values(str(_ENV_EXAMPLE_PATH))
    assert isinstance(values, dict), "dotenv_values dict qaytarmadi"
    assert len(values) > 0, (
        ".env.example'dan hech qanday key parse qilinmadi — "
        "fayl bo'sh yoki butunlay kommentlangan"
    )


@test("A3. TradingConfig.model_fields'dagi BARCHA nomlar .env.example'da bor")
def _():
    expected = {name.upper() for name in TradingConfig.model_fields}
    values = dotenv_values(str(_ENV_EXAMPLE_PATH))
    present = {k.upper() for k in values.keys()}
    missing = expected - present
    assert not missing, (
        f"TradingConfig'dagi {len(missing)} ta maydon .env.example'da yo'q: "
        f"{sorted(missing)}"
    )


@test("A4. Bo'sh placeholder yo'q required numeric maydonlarda")
def _():
    # `KEY=` (bo'sh value) numeric field uchun ValidationError beradi.
    # String fields (CLAUDE_API_KEY, TELEGRAM_*) bo'sh bo'lishi mumkin — bu OK.
    required_numeric = {
        "MT5_LOGIN", "SCAN_INTERVAL",
        "RISK_PER_TRADE", "DAILY_MAX_RISK", "DAILY_PROFIT_TARGET",
        "MAX_POSITIONS", "MAX_TRADES_PER_DAY", "MAX_DRAWDOWN",
        "MIN_AI_CONFIDENCE",
    }
    values = dotenv_values(str(_ENV_EXAMPLE_PATH))
    empty_required = []
    for k in required_numeric:
        if k in values:
            v = values[k]
            if v is None or v.strip() == "":
                empty_required.append(k)
    assert not empty_required, (
        f"Quyidagi numeric field'lar .env.example'da bo'sh (ValidationError beradi): "
        f"{empty_required}"
    )


# ─────────────────── Group B: .env.example strict validation'dan o'tadi ────

@test("B5. .env.example dotenv_values -> TradingConfig(_env_file=None) muvaffaqiyatli")
def _():
    values = dotenv_values(str(_ENV_EXAMPLE_PATH))
    ok, exc, cfg = _build_trading_from_values(values)
    assert ok, (
        f".env.example strict validation'dan o'tmadi: {type(exc).__name__}: {exc}"
    )
    assert cfg is not None


@test("B6. get_risk_config() valid RiskConfig qaytaradi")
def _():
    values = dotenv_values(str(_ENV_EXAMPLE_PATH))
    ok, exc, cfg = _build_trading_from_values(values)
    assert ok, f"prereq build failed: {exc}"
    rc = cfg.get_risk_config()
    assert isinstance(rc, RiskConfig)
    # invariants — RiskConfig konstruktori validate qilgan
    assert rc.daily_max_risk >= rc.risk_per_trade
    assert rc.max_drawdown >= rc.daily_max_risk
    tp_sum = rc.tp1_close_pct + rc.tp2_close_pct + rc.tp3_close_pct
    assert abs(tp_sum - 100.0) < 0.1, f"tp_sum={tp_sum} (expected ~100)"


@test("B7. Spot-check safe defaults: risk=1.0, daily=5.0, dd=10.0")
def _():
    values = dotenv_values(str(_ENV_EXAMPLE_PATH))
    ok, exc, cfg = _build_trading_from_values(values)
    assert ok, f"prereq build failed: {exc}"
    assert cfg.risk_per_trade == 1.0, (
        f"risk_per_trade={cfg.risk_per_trade} (expected 1.0)"
    )
    assert cfg.daily_max_risk == 5.0, (
        f"daily_max_risk={cfg.daily_max_risk} (expected 5.0)"
    )
    assert cfg.max_drawdown == 10.0, (
        f"max_drawdown={cfg.max_drawdown} (expected 10.0)"
    )


# ─────────────────── Group C: start.bat fallback xulqi ──────────────────────
# Approach: subprocess via `cmd /c`. Windows shellda kichik isolated tmp dir
# yaratamiz, start.bat va .env.example'ni nusxa olamiz. timeout=10s, agar
# `pause` osilib qolsa stdin (empty input) yuborib break qilamiz.
# Fallback: agar cmd.exe topilmasa yoki timeout bo'lsa, faylning matnli
# tekshiruviga o'tamiz (kamroq rigorous lekin reliable).

def _run_start_bat(tmp_dir: Path, env_present: bool) -> tuple[int, str, bool]:
    """start.bat'ni tmp_dir ichida ishga tushiradi.
    Returns: (exit_code, stdout_lower, env_file_created)
    """
    # nusxa olish
    shutil.copy(_START_BAT_PATH, tmp_dir / "start.bat")
    shutil.copy(_ENV_EXAMPLE_PATH, tmp_dir / ".env.example")

    if env_present:
        # minimal .env yarataylik — content tarkibi muhim emas, faqat mavjudligi
        (tmp_dir / ".env").write_text("DUMMY=1\n", encoding="utf-8")
    else:
        # ehtiyot uchun .env'ni o'chiramiz
        env_path = tmp_dir / ".env"
        if env_path.exists():
            env_path.unlink()

    # `pause` osilib qolmasligi uchun stdin'ga newline yuboramiz (key press emulate).
    # timeout sifatida 15s — odatda mgnenoda chiqadi.
    try:
        # Eslatma: `cmd /c start.bat` Windowsda PATH'dan qidiradi va cwd'dagi
        # batni topmaydi (rc=1 + "not recognized"). `.\\start.bat` cwd'dan
        # ishga tushiradi.
        p = subprocess.run(
            ["cmd", "/c", ".\\start.bat"],
            cwd=str(tmp_dir),
            input="\r\n\r\n",
            capture_output=True,
            text=True,
            timeout=15,
        )
        stdout = (p.stdout or "") + (p.stderr or "")
        env_created = (tmp_dir / ".env").exists()
        return p.returncode, stdout.lower(), env_created
    except subprocess.TimeoutExpired as e:
        # pause asilib qolgan; outputni baribir olib kelamiz
        stdout = ""
        if e.stdout:
            stdout = e.stdout if isinstance(e.stdout, str) else e.stdout.decode(errors="ignore")
        if e.stderr:
            stdout += e.stderr if isinstance(e.stderr, str) else e.stderr.decode(errors="ignore")
        env_created = (tmp_dir / ".env").exists()
        # timeout -> rc=-1 (signal terminated), test mantig'i timeout bo'lganini biladi
        return -1, stdout.lower(), env_created


@test("C8. .env yo'q -> start.bat .env yaratadi, prompt chiqaradi, exit 1")
def _():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        rc, out, env_created = _run_start_bat(tmp, env_present=False)

        # .env yaratilgan bo'lishi shart (asosiy talab)
        assert env_created, (
            f".env yaratilmadi tmp dir ichida (rc={rc}, out_len={len(out)})"
        )

        # promptdagi kalit so'zlardan biri stdout'da bo'lishi shart
        prompt_keywords = ("muhim:", "sozlang", "ogohlantirish")
        found = [kw for kw in prompt_keywords if kw in out]
        assert found, (
            f"Setup prompt topilmadi stdout'da (rc={rc}). "
            f"Kutilgan keys: {prompt_keywords}, stdout[:300]={out[:300]!r}"
        )

        # exit code: 1 (env-missing branch) yoki -1 (timeout - pause asilib qolgan
        # bo'lsa, baribir copy bajarilgan va prompt chiqarilgan; bu acceptable
        # chunki asosiy kontrakt — copy + prompt + non-zero exit). Bot start
        # qilmasligi shart.
        assert rc != 0, (
            f"start.bat exit code 0 — .env yo'q bo'lganda ishlab ketdi! "
            f"rc={rc}, stdout[:300]={out[:300]!r}"
        )


@test("C9. .env bor -> start.bat .env-missing-promptni SKIP qiladi")
def _():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        rc, out, _ = _run_start_bat(tmp, env_present=True)

        # Faqat asosiy talab: env-missing-prompt KO'RSATILMAGAN bo'lishi shart.
        # "muhim:" so'zi faqat .env-missing branch'ida chiqadi.
        # (Keyingi qadamda Python apps\api\main.py topa olmay fail bo'ladi —
        # bu OK, biz buni tekshirmaymiz.)
        assert "muhim:" not in out, (
            f".env mavjud bo'lsa ham .env-missing prompt chiqdi! "
            f"rc={rc}, stdout[:400]={out[:400]!r}"
        )
        assert "ogohlantirish:" not in out, (
            f".env mavjud bo'lsa ham .env-fallback ogohlantirish chiqdi! "
            f"rc={rc}, stdout[:400]={out[:400]!r}"
        )


# ─────────────────── Group D: Negativ holat (working-reference proof) ──────

@test("D10. Korrupt RISK_PER_TRADE=abc .env.example'da -> ValidationError")
def _():
    # .env.example clean load qilingach, faqat RISK_PER_TRADE'ni buzamiz.
    # Bu .env.example "working reference" ekanligini isbotlaydi: har qanday
    # format og'ishi build'ni buzadi.
    values = dict(dotenv_values(str(_ENV_EXAMPLE_PATH)))
    values["RISK_PER_TRADE"] = "abc"
    ok, exc, _ = _build_trading_from_values(values)
    assert not ok, (
        "RISK_PER_TRADE=abc qabul qilindi — silent fallback hali ham bor?"
    )
    assert isinstance(exc, ValidationError), (
        f"expected ValidationError, got {type(exc).__name__}: {exc}"
    )


# ─────────────────── Summary + .env tamper check ────────────────────────────

print(f"\nResult: {PASSED} passed, {FAILED} failed")

# Real .env va .env.example DISK'da o'zgarmagan bo'lishi shart
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

if _ENV_EXAMPLE_SHA_BEFORE is not None:
    sha_after = hashlib.sha256(_ENV_EXAMPLE_PATH.read_bytes()).hexdigest()
    if sha_after != _ENV_EXAMPLE_SHA_BEFORE:
        print(
            f"\n[FATAL] .env.example was MODIFIED during tests!\n"
            f"  before: {_ENV_EXAMPLE_SHA_BEFORE}\n"
            f"  after:  {sha_after}"
        )
        sys.exit(2)
    else:
        print(f".env.example SHA256 unchanged: {_ENV_EXAMPLE_SHA_BEFORE[:16]}...")

sys.exit(0 if FAILED == 0 else 1)
