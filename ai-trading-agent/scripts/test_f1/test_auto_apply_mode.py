"""F1-1 test: legacy AUTO_APPLY_LEARNING=true behavior

50 ta trade simulate qilamiz, ammo bu safar AUTO_APPLY_LEARNING=true. Tekshiruv:
  1. learned.json'dagi adapted.setup_weights.OB DEFAULT (1.0) dan O'ZGARDI
  2. db.list_pending() bo'sh — hech narsa pending'ga tushmagan
  3. db.list_active() >= 1 ta `applied_by="auto-apply"` row (audit trail)

Env tozalash: try/finally orqali AUTO_APPLY_LEARNING'ni har holatda olib
tashlaymiz, hatto exception bo'lsa ham.

Test izolyatsiyasi: tempdir + db._DEFAULT_STATE_DIR monkey-patch.

Run:
    cd ai-trading-agent
    python scripts/test_f1/test_auto_apply_mode.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

# loyiha rootini sys.path'ga qo'shish
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from apps.api.src.agents.trader.state import db
from apps.api.src.agents.trader.brain.self_learner import SelfLearner, _DEFAULT_WEIGHTS


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


def _run_auto_apply(tmpdir: Path) -> tuple[SelfLearner, dict]:
    """50 trade'ni AUTO_APPLY_LEARNING=true ostida ishga tushirish."""
    db._DEFAULT_STATE_DIR = tmpdir  # type: ignore[attr-defined]

    learned_path = tmpdir / "learned.json"
    sl = SelfLearner(
        path=str(learned_path),
        lessons_path=str(tmpdir / "lessons.json"),
    )

    for i in range(50):
        if i < 40:
            entry_type = "OB"
            result = "win" if (i % 3) != 0 else "loss"
            pnl = 20.0 if result == "win" else -10.0
        else:
            entry_type = "FVG"
            result = "win" if (i % 10) != 0 else "loss"
            pnl = 25.0 if result == "win" else -10.0
        sl.log_trade(
            entry_type=entry_type,
            timeframe="H1",
            session="london",
            direction="buy",
            sl_pips=10.0,
            tp_pips=20.0,
            result=result,
            pnl=pnl,
            mode="FLOW",
        )

    with open(learned_path, "r", encoding="utf-8") as f:
        on_disk = json.load(f)
    return sl, on_disk


print("\n=== F1-1: SelfLearner AUTO_APPLY_LEARNING=true (legacy direct-mutate) ===")


@test("learned.json's adapted.setup_weights changed from defaults (AUTO_APPLY=true)")
def _():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        os.environ["AUTO_APPLY_LEARNING"] = "true"
        try:
            _sl, on_disk = _run_auto_apply(d)
        finally:
            os.environ.pop("AUTO_APPLY_LEARNING", None)

        default_ob = _DEFAULT_WEIGHTS["OB"]  # 1.0
        weights = on_disk["adapted"]["setup_weights"]
        # OB yoki FVG yoki ikkalasi o'zgargan bo'lishi shart
        ob = weights.get("OB", default_ob)
        fvg = weights.get("FVG", 1.0)
        changed = (ob != default_ob) or (fvg != 1.0)
        assert changed, (
            f"AUTO_APPLY=true bo'lsa ham hech bir setup weight o'zgarmadi! "
            f"OB={ob}, FVG={fvg}. Gate noto'g'ri ishlamoqda."
        )


@test("db.list_pending() returns 0 — nothing left pending under AUTO_APPLY")
def _():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        os.environ["AUTO_APPLY_LEARNING"] = "true"
        try:
            _run_auto_apply(d)
        finally:
            os.environ.pop("AUTO_APPLY_LEARNING", None)

        pending = db.list_pending(state_dir=d)
        # AUTO_APPLY rejimida self_learner propose_adjustment + approve_adjustment
        # ketma-ket chaqiradi — natijada status='approved' bo'ladi, pending list
        # bo'sh bo'lishi shart.
        assert len(pending) == 0, \
            f"AUTO_APPLY rejimida pending bo'sh bo'lishi shart, lekin: {pending}"


@test("db.list_active() >= 1 row with applied_by='auto-apply' (audit trail)")
def _():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        os.environ["AUTO_APPLY_LEARNING"] = "true"
        try:
            _run_auto_apply(d)
        finally:
            os.environ.pop("AUTO_APPLY_LEARNING", None)

        active = db.list_active(state_dir=d)
        assert len(active) >= 1, \
            "AUTO_APPLY audit yo'q — active list bo'sh"
        auto_rows = [a for a in active if a["applied_by"] == "auto-apply"]
        assert auto_rows, (
            f"applied_by='auto-apply' bo'lgan row yo'q. "
            f"Mavjudlar: {[a['applied_by'] for a in active]}"
        )


print(f"\nResult: {PASSED} passed, {FAILED} failed")
sys.exit(0 if FAILED == 0 else 1)
