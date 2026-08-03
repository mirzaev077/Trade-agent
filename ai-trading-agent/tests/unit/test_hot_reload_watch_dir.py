"""
Hot reload — kuzatuv papkasi bugi (2026-08-03 da topildi va tuzatildi).

BUG: `main.py` da
    _WATCH_DIR = Path(__file__).parent.parent / "src" / "agents" / "trader"
`main.py` esa `apps/api/` ichida yotadi, ya'ni yo'l `apps/src/agents/trader`
ga chiqardi — bunday papka MAVJUD EMAS. `_FileWatcher._run()` ichida
`self._dir.rglob("*.py")` bo'sh ro'yxat qaytargan (mavjud bo'lmagan papkada
rglob xato bermaydi), shuning uchun watcher hech qachon o'zgarish ko'rmagan.

Nega uzoq sezilmadi: startupda baribir "HOT RELOAD yoqilgan — fayl
saqlansang agent o'zi qayta ishga tushadi" deb yozilardi. Xabar holatni
emas, niyatni aytardi.

TUZATISH: bitta `parent`, plus watcher endi papka mavjudligini tekshiradi
va topmasa OGOHLANTIRADI, jimgina o'tib ketmaydi. Qo'shimcha: `HOT_RELOAD`
env kaliti (F5-5 davomida o'chirib qo'yish uchun).

Testlar main.py ni SUBPROCESS'da import qiladi. Sabab: main.py modul
darajasida `load_dotenv(override=True)`, `setup_logging()` va
`sys.excepthook` almashtirishni bajaradi — ularni pytest jarayoniga
kiritish boshqa testlarga ta'sir qilardi.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]          # ai-trading-agent/
_MAIN_PY = _ROOT / "apps" / "api" / "main.py"

# main.py ni import qilib, hot-reload holatini JSON bo'lib qaytaradi.
#
# HOT_RELOAD qiymati IMPORTDAN KEYIN o'rnatiladi. Sabab: main.py modul
# darajasida `load_dotenv(_root / ".env", override=True)` chaqiradi va u
# jarayon env'ini jonli `.env` bilan USTIDAN yozadi. Agar qiymatni oldindan
# bersak, `.env` da HOT_RELOAD paydo bo'lgan kuni test sindirilardi.
# `_hot_reload_enabled()` env'ni chaqiruv paytida o'qiydi (loyiha
# konvensiyasi) — shuning uchun keyin qo'yish to'g'ri va barqaror.
_PROBE = r"""
import json, os, sys, importlib
from pathlib import Path
root = Path(sys.argv[1])
sentinel = sys.argv[2]          # "__UNSET__" yoki aniq qiymat
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "apps" / "api"))
m = importlib.import_module("apps.api.main")
if sentinel == "__UNSET__":
    os.environ.pop("HOT_RELOAD", None)
else:
    os.environ["HOT_RELOAD"] = sentinel
print("___JSON___" + json.dumps({
    "watch_dir": str(m._WATCH_DIR),
    "exists":    m._WATCH_DIR.is_dir(),
    "has_agent": (m._WATCH_DIR / "agent.py").is_file(),
    "enabled":   m._hot_reload_enabled(),
}))
"""


def _probe(hot_reload: str | None = "__UNSET__", tmp_log_dir: Path | None = None) -> dict:
    """main.py ni alohida jarayonda import qilib holatini o'qiydi.

    Args:
        hot_reload: `HOT_RELOAD` qiymati; "__UNSET__" → umuman o'rnatilmaydi.
        tmp_log_dir: import paytidagi `setup_logging()` shu yerga yozsin
                     (jonli `apps/data/logs/` ga tegmasin).
    """
    env = dict(os.environ)
    env["OPENCLAW_LOG_DIR"] = str(tmp_log_dir or (_ROOT / "data" / "logs"))
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, str(_ROOT), "__UNSET__" if hot_reload is None else hot_reload],
        capture_output=True, text=True, timeout=180, env=env,
    )
    if "___JSON___" not in proc.stdout:
        pytest.fail(f"probe muvaffaqiyatsiz.\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr[-3000:]}")
    return json.loads(proc.stdout.split("___JSON___", 1)[1].splitlines()[0])


# ── Group A: ILDIZ BUG — papka haqiqatan mavjudmi ───────────────────────────

def test_watch_dir_exists(tmp_path):
    """ASOSIY REGRESSIYA TESTI: kuzatuv papkasi diskda bo'lishi shart.

    Bug paytida bu `apps/src/agents/trader` edi va `is_dir()` False qaytarardi.
    """
    info = _probe(tmp_log_dir=tmp_path)
    assert info["exists"], f"kuzatuv papkasi mavjud emas: {info['watch_dir']}"


def test_watch_dir_is_the_real_trader_package(tmp_path):
    """To'g'ri papka ekanini tasdiqlash — ichida `agent.py` bo'lsin.

    Faqat "papka bor" yetarli emas: tasodifan boshqa mavjud papkaga
    ishora qilsa ham test o'tib ketardi.
    """
    info = _probe(tmp_log_dir=tmp_path)
    assert info["has_agent"], f"agent.py topilmadi: {info['watch_dir']}"
    assert Path(info["watch_dir"]) == _ROOT / "apps" / "api" / "src" / "agents" / "trader"


def test_source_no_longer_uses_double_parent():
    """`parent.parent` qaytib kelmasin — aynan shu bir so'z bugni yaratgan."""
    line = next(
        ln for ln in _MAIN_PY.read_text(encoding="utf-8").splitlines()
        if ln.strip().startswith("_WATCH_DIR")
    )
    assert "parent.parent" not in line, f"eski buggy yo'l qaytdi: {line}"


# ── Group B: HOT_RELOAD kaliti ──────────────────────────────────────────────

def test_hot_reload_on_by_default(tmp_path):
    """`HOT_RELOAD` umuman qo'yilmasa — yoqilgan (eski xulq saqlanadi)."""
    info = _probe(None, tmp_log_dir=tmp_path)
    assert info["enabled"] is True


def test_hot_reload_on_when_empty(tmp_path):
    """Bo'sh qiymat ham o'chirmaydi — faqat aniq 'false/0/no/off' o'chiradi."""
    info = _probe("", tmp_log_dir=tmp_path)
    assert info["enabled"] is True


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "off", " Off "])
def test_hot_reload_can_be_disabled(value, tmp_path):
    """F5-5 davomida o'chirib qo'yish uchun — turli yozilishlar qabul qilinadi."""
    info = _probe(value, tmp_log_dir=tmp_path)
    assert info["enabled"] is False, f"HOT_RELOAD={value!r} o'chirmadi"


@pytest.mark.parametrize("value", ["true", "1", "yes", "anything-else"])
def test_hot_reload_stays_on_for_other_values(value, tmp_path):
    """Faqat aniq 'o'chirish' so'zlari o'chiradi; qolgani yoqiq qoladi."""
    info = _probe(value, tmp_log_dir=tmp_path)
    assert info["enabled"] is True


# ── Group C: watcher endi jimgina o'tib ketmaydi (source guard) ─────────────

def test_watcher_is_none_guarded():
    """`watcher.start()` shartsiz chaqirilmasin — aks holda None'da AttributeError."""
    src = _MAIN_PY.read_text(encoding="utf-8")
    body = src.split("async def _run_agent")[1].split("\nasync def ")[0]
    assert "if watcher is not None:" in body
    assert "\n    watcher.start()" not in body, "shartsiz watcher.start() qoldi"


def test_missing_watch_dir_logs_warning():
    """Papka topilmasa OGOHLANTIRISH yozilsin — jimlik bugni yashirgan edi."""
    src = _MAIN_PY.read_text(encoding="utf-8")
    body = src.split("async def _run_agent")[1].split("\nasync def ")[0]
    assert "_WATCH_DIR.is_dir()" in body, "papka mavjudligi tekshirilmayapti"
    assert "logger.warning" in body


def test_startup_message_reflects_real_state():
    """Xabar niyatni emas, HAQIQIY holatni aytsin.

    Avval watcher o'lik bo'lsa ham "HOT RELOAD yoqilgan" deb yozilardi —
    aynan shu yolg'on xabar bugni oylab ko'rinmas qilgan.
    """
    src = _MAIN_PY.read_text(encoding="utf-8")
    body = src.split("async def _run_agent")[1].split("\nasync def ")[0]
    assert "watcher is not None else" in body, "xabar holatga bog'lanmagan"
    assert "HOT RELOAD o'chiq" in body


# ── Group D: watcher haqiqatan o'zgarishni sezadimi ─────────────────────────

def test_file_watcher_detects_change(tmp_path):
    """`_FileWatcher` xulqi: .py fayl o'zgarsa callback chaqiriladi.

    Bu bugning ikkinchi yarmi — yo'l to'g'rilangani bilan watcher o'zi
    ishlamasa foyda yo'q.
    """
    watch = tmp_path / "pkg"
    watch.mkdir()
    target = watch / "mod.py"
    target.write_text("x = 1\n", encoding="utf-8")

    script = r"""
import sys, time, json
from pathlib import Path
root = Path(sys.argv[1]); watch = Path(sys.argv[2])
sys.path.insert(0, str(root)); sys.path.insert(0, str(root / "apps" / "api"))
import importlib
m = importlib.import_module("apps.api.main")
hits = []
w = m._FileWatcher(watch, lambda name: hits.append(name))
w.start()
time.sleep(0.5)
(watch / "mod.py").write_text("x = 2\n", encoding="utf-8")
deadline = time.time() + 20
while not hits and time.time() < deadline:
    time.sleep(0.3)
w.stop()
print("___JSON___" + json.dumps({"hits": hits}))
"""
    env = dict(os.environ)
    env["OPENCLAW_LOG_DIR"] = str(tmp_path / "logs")
    proc = subprocess.run(
        [sys.executable, "-c", script, str(_ROOT), str(watch)],
        capture_output=True, text=True, timeout=180, env=env,
    )
    assert "___JSON___" in proc.stdout, proc.stderr[-2000:]
    result = json.loads(proc.stdout.split("___JSON___", 1)[1].splitlines()[0])
    assert result["hits"] == ["mod.py"], f"watcher o'zgarishni sezmadi: {result}"


def test_watcher_ignores_untouched_dir(tmp_path):
    """Hech narsa o'zgarmasa callback chaqirilmasin (soxta restart bo'lmasin)."""
    watch = tmp_path / "pkg"
    watch.mkdir()
    (watch / "mod.py").write_text("x = 1\n", encoding="utf-8")

    script = r"""
import sys, time, json
from pathlib import Path
root = Path(sys.argv[1]); watch = Path(sys.argv[2])
sys.path.insert(0, str(root)); sys.path.insert(0, str(root / "apps" / "api"))
import importlib
m = importlib.import_module("apps.api.main")
hits = []
w = m._FileWatcher(watch, lambda name: hits.append(name))
w.start()
time.sleep(5)
w.stop()
print("___JSON___" + json.dumps({"hits": hits}))
"""
    env = dict(os.environ)
    env["OPENCLAW_LOG_DIR"] = str(tmp_path / "logs")
    started = time.time()
    proc = subprocess.run(
        [sys.executable, "-c", script, str(_ROOT), str(watch)],
        capture_output=True, text=True, timeout=180, env=env,
    )
    assert "___JSON___" in proc.stdout, proc.stderr[-2000:]
    result = json.loads(proc.stdout.split("___JSON___", 1)[1].splitlines()[0])
    assert result["hits"] == [], f"soxta o'zgarish signali: {result}"
    assert time.time() - started >= 4   # watcher haqiqatan kuzatib turdi
