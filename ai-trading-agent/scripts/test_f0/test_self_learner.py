"""F0-5 test: self_learner.py lessons.json hard-override priority

Tekshiruvlar:
  - lessons.json `[{"setup": "RB", "action": "disable"}]` -> get_setup_weight("H4_RB") == 0.0
  - Disable entry yo'q -> odatdagi weight (default ~1.0)
  - Malformed JSON -> warning + bo'sh disabled set, crash yo'q
  - Warning-only entries (backward compat) -> disabled set'ga ta'sir qilmaydi
  - mtime cache: fayl o'zgarmasa qayta o'qilmaydi
  - mtime o'zgarsa reload (yangi disable entry effect ko'rsatadi)
  - Case insensitivity: "rb", "Rb", "RB" hammasi mos kelishi shart

Production lessons.json / learned.json fayllariga TEGILMAYDI — har test
tempdir ishlatadi (lessons_path va path constructor args).

Run:
    cd ai-trading-agent
    python scripts/test_f0/test_self_learner.py
"""
import sys
import os
import json
import time
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch

# loyiha rootini sys.path'ga qo'shish
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from apps.api.src.agents.trader.brain.self_learner import SelfLearner


PASSED = 0
FAILED = 0


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


def _make_learner(tmpdir: Path, lessons_payload: dict | str | None = None,
                  learned_payload: dict | None = None) -> SelfLearner:
    """Tempdir ichida izolyatsiyalangan SelfLearner yaratish."""
    lessons_path = tmpdir / "lessons.json"
    learned_path = tmpdir / "learned.json"

    if lessons_payload is not None:
        if isinstance(lessons_payload, str):
            lessons_path.write_text(lessons_payload, encoding="utf-8")
        else:
            lessons_path.write_text(json.dumps(lessons_payload), encoding="utf-8")

    if learned_payload is not None:
        learned_path.write_text(json.dumps(learned_payload), encoding="utf-8")

    # Constructor: (path: str = None, lessons_path: str = None)
    return SelfLearner(path=str(learned_path), lessons_path=str(lessons_path))


print("\n=== F0-5 self_learner lessons.json hard-override ===")


# --- Asosiy disable scenariy --------------------------------------------------

@test("disable entry -> weight 0.0")
def _():
    with tempfile.TemporaryDirectory() as td:
        sl = _make_learner(
            Path(td),
            lessons_payload={
                "lessons": [{"setup": "RB", "action": "disable", "reason": "test"}]
            },
        )
        w = sl.get_setup_weight("H4_RB")
        assert w == 0.0, f"Expected 0.0 (disabled), got {w}"


@test("no disable entry -> default weight ~ 1.0")
def _():
    with tempfile.TemporaryDirectory() as td:
        sl = _make_learner(
            Path(td),
            lessons_payload={"lessons": []},
        )
        w = sl.get_setup_weight("H4_OB")
        # OB default weight = 1.0 (per _DEFAULT_WEIGHTS)
        assert w == 1.0, f"Expected ~1.0, got {w}"


@test("lessons.json yo'q -> default weight, crash yo'q")
def _():
    with tempfile.TemporaryDirectory() as td:
        # lessons fayli yaratmaymiz
        sl = _make_learner(Path(td), lessons_payload=None)
        w = sl.get_setup_weight("H4_OB")
        assert w == 1.0, f"Expected 1.0, got {w}"


@test("disable RB -> H4_OB ta'sirlanmaydi (faqat RB)")
def _():
    with tempfile.TemporaryDirectory() as td:
        sl = _make_learner(
            Path(td),
            lessons_payload={
                "lessons": [{"setup": "RB", "action": "disable"}]
            },
        )
        assert sl.get_setup_weight("H4_RB") == 0.0
        assert sl.get_setup_weight("H4_OB") == 1.0
        assert sl.get_setup_weight("M15_FVG") == 1.0


# --- Malformed JSON ----------------------------------------------------------

@test("malformed JSON -> empty disabled set, no crash")
def _():
    with tempfile.TemporaryDirectory() as td:
        sl = _make_learner(
            Path(td),
            lessons_payload="{not valid json}}}",
        )
        # crash bo'lmasligi shart, disabled set bo'sh
        w = sl.get_setup_weight("H4_RB")
        assert w == 1.0, f"Malformed JSON da default weight kutildi, got {w}"


@test("lessons.json root list (dict emas) -> empty disabled set")
def _():
    with tempfile.TemporaryDirectory() as td:
        sl = _make_learner(
            Path(td),
            lessons_payload="[1, 2, 3]",
        )
        w = sl.get_setup_weight("H4_RB")
        assert w == 1.0, f"Got {w}"


@test("lessons field list emas -> empty disabled set")
def _():
    with tempfile.TemporaryDirectory() as td:
        sl = _make_learner(
            Path(td),
            lessons_payload={"lessons": "not a list"},
        )
        w = sl.get_setup_weight("H4_RB")
        assert w == 1.0, f"Got {w}"


# --- Warning-only entries (backward compat) ----------------------------------

@test("warning-only entry (action yo'q) -> disabled set'ga ta'sir qilmaydi")
def _():
    with tempfile.TemporaryDirectory() as td:
        sl = _make_learner(
            Path(td),
            lessons_payload={
                "lessons": [
                    {
                        "warning": "BUY in PREMIUM zone — past losses",
                        "conditions": {"direction": "buy", "pd_zone": "premium"},
                        "count": 9,
                        "created": "2026-04-17T16:31:00",
                    }
                ]
            },
        )
        # Warning yozuvi disable hisoblanmaydi
        assert sl.get_setup_weight("H4_RB") == 1.0
        assert sl.get_setup_weight("H4_OB") == 1.0


@test("warning + disable aralash -> faqat disable ishlaydi")
def _():
    with tempfile.TemporaryDirectory() as td:
        sl = _make_learner(
            Path(td),
            lessons_payload={
                "lessons": [
                    {"warning": "x", "conditions": {}, "count": 3},
                    {"setup": "RB", "action": "disable"},
                ]
            },
        )
        assert sl.get_setup_weight("H4_RB") == 0.0
        assert sl.get_setup_weight("H4_OB") == 1.0


# --- mtime cache -------------------------------------------------------------

@test("mtime cache: fayl o'zgarmasa qayta o'qilmaydi")
def _():
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        lessons_path = tdp / "lessons.json"
        lessons_path.write_text(json.dumps({
            "lessons": [{"setup": "RB", "action": "disable"}]
        }), encoding="utf-8")

        sl = SelfLearner(path=str(tdp / "learned.json"), lessons_path=str(lessons_path))

        # SelfLearner.__init__ ichida _refresh_disabled_setups bir marta chaqirildi.
        # Endi keyingi chaqiruvlarda fayl o'qilmasligi shart (mtime bir xil).

        original_load = sl._load_disabled_setups
        call_count = {"n": 0}

        def counting_load():
            call_count["n"] += 1
            return original_load()

        sl._load_disabled_setups = counting_load

        # 5 marta get_setup_weight chaqiramiz — mtime o'zgarmagan
        for _ in range(5):
            assert sl.get_setup_weight("H4_RB") == 0.0

        assert call_count["n"] == 0, \
            f"mtime cache buzilgan — _load_disabled_setups {call_count['n']} marta chaqirildi"


@test("mtime cache: fayl o'zgarsa reload")
def _():
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        lessons_path = tdp / "lessons.json"
        lessons_path.write_text(json.dumps({
            "lessons": []
        }), encoding="utf-8")

        sl = SelfLearner(path=str(tdp / "learned.json"), lessons_path=str(lessons_path))
        assert sl.get_setup_weight("H4_RB") == 1.0, "Boshlanishida RB enabled"

        # Faylni o'zgartirib mtime'ni oshirish
        time.sleep(0.05)  # mtime resolution
        lessons_path.write_text(json.dumps({
            "lessons": [{"setup": "RB", "action": "disable"}]
        }), encoding="utf-8")
        # Ba'zi FS'larda mtime resolution past — majburiy oshiramiz
        now = time.time() + 5
        os.utime(lessons_path, (now, now))

        # Endi RB disabled bo'lishi shart
        assert sl.get_setup_weight("H4_RB") == 0.0, \
            "mtime o'zgardi — reload bo'lishi va RB disable bo'lishi shart"


# --- Case insensitivity ------------------------------------------------------

@test("case insensitive disable: 'rb' (lowercase) matches H4_RB")
def _():
    with tempfile.TemporaryDirectory() as td:
        sl = _make_learner(
            Path(td),
            lessons_payload={
                "lessons": [{"setup": "rb", "action": "disable"}]
            },
        )
        assert sl.get_setup_weight("H4_RB") == 0.0


@test("case insensitive disable: 'Rb' (mixed) matches H4_RB")
def _():
    with tempfile.TemporaryDirectory() as td:
        sl = _make_learner(
            Path(td),
            lessons_payload={
                "lessons": [{"setup": "Rb", "action": "disable"}]
            },
        )
        assert sl.get_setup_weight("H4_RB") == 0.0


@test("case insensitive: 'fvg' disable matches M15_FVG")
def _():
    with tempfile.TemporaryDirectory() as td:
        sl = _make_learner(
            Path(td),
            lessons_payload={
                "lessons": [{"setup": "fvg", "action": "disable"}]
            },
        )
        assert sl.get_setup_weight("M15_FVG") == 0.0
        # OB hali ham ishlaydi
        assert sl.get_setup_weight("H4_OB") == 1.0


# --- Boundary / robust -------------------------------------------------------

@test("setup bo'sh string -> disabled set'ga qo'shilmaydi")
def _():
    with tempfile.TemporaryDirectory() as td:
        sl = _make_learner(
            Path(td),
            lessons_payload={
                "lessons": [{"setup": "", "action": "disable"}]
            },
        )
        # Bo'sh string — disabled set bo'sh bo'lishi shart
        assert sl._disabled_setups == set(), \
            f"Bo'sh setup qabul qilingan: {sl._disabled_setups}"
        assert sl.get_setup_weight("H4_OB") == 1.0


@test("action != 'disable' -> ignor")
def _():
    with tempfile.TemporaryDirectory() as td:
        sl = _make_learner(
            Path(td),
            lessons_payload={
                "lessons": [
                    {"setup": "OB", "action": "enable"},
                    {"setup": "FVG", "action": "warn"},
                ]
            },
        )
        # Hech qaysi disable emas
        assert sl.get_setup_weight("H4_OB") == 1.0
        assert sl.get_setup_weight("M15_FVG") == 1.0


@test("Multiple disable entries -> hammasi ishlaydi")
def _():
    with tempfile.TemporaryDirectory() as td:
        sl = _make_learner(
            Path(td),
            lessons_payload={
                "lessons": [
                    {"setup": "RB", "action": "disable"},
                    {"setup": "BB", "action": "disable"},
                    {"setup": "MB", "action": "disable"},
                ]
            },
        )
        assert sl.get_setup_weight("H4_RB") == 0.0
        assert sl.get_setup_weight("H1_BB") == 0.0
        assert sl.get_setup_weight("M15_MB") == 0.0
        assert sl.get_setup_weight("H4_OB") == 1.0


@test("learned.json adapted.disabled (auto-disable) ham ishlaydi")
def _():
    with tempfile.TemporaryDirectory() as td:
        sl = _make_learner(
            Path(td),
            lessons_payload={"lessons": []},
            learned_payload={
                "trades": [],
                "adapted": {
                    "setup_weights": {"OB": 1.0, "RB": 1.0},
                    "entry_pct": 0.3,
                    "sl_multiplier": 1.0,
                    "disabled": ["RB"],  # auto-disable
                    "session_weights": {},
                },
                "stats": {"total": 0, "last_analysis_at": 0, "last_evolution_at": 0},
                "session_day_stats": {},
            },
        )
        assert sl.get_setup_weight("H4_RB") == 0.0, "auto-disable ham 0.0 berishi shart"
        assert sl.get_setup_weight("H4_OB") == 1.0


@test("lessons.json priority > learned.json weight (qayta yoqilmaydi)")
def _():
    with tempfile.TemporaryDirectory() as td:
        sl = _make_learner(
            Path(td),
            lessons_payload={
                "lessons": [{"setup": "RB", "action": "disable"}]
            },
            learned_payload={
                "trades": [],
                "adapted": {
                    "setup_weights": {"OB": 1.0, "RB": 1.8},  # RB boost qilingan
                    "entry_pct": 0.3,
                    "sl_multiplier": 1.0,
                    "disabled": [],   # auto-disable yo'q
                    "session_weights": {},
                },
                "stats": {"total": 0, "last_analysis_at": 0, "last_evolution_at": 0},
                "session_day_stats": {},
            },
        )
        # learned.json'da weight 1.8 bo'lsa ham lessons.json hard-override 0.0 berishi shart
        assert sl.get_setup_weight("H4_RB") == 0.0


print(f"\nResult: {PASSED} passed, {FAILED} failed")
sys.exit(0 if FAILED == 0 else 1)
