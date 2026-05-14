"""F1-1 KEY TEST: 50-trade scenario verifies approval gate

Tasks.md F1-1 acceptance criteria, verbatim:
    "50 trade'dan keyin learned.json o'zgarmasligini, lekin
     pending_adjustments ga yozilishini tekshirish."

Translation: AUTO_APPLY_LEARNING=false (default) bo'lganda:
  1. learned.json'dagi adapted.setup_weights.OB DEFAULT (1.0) bo'lib qoladi
  2. db.list_pending() da kamida 1 ta proposal bo'ladi (setup_weights.OB yoki .FVG)
  3. Proposed row paramі setup_weights.* pattern'ga to'g'ri keladi
  4. data["trades"] uzunligi 50 (trade'lar normal log qilinadi)
  5. data["stats"]["total"] == 50

Test izolyatsiyasi:
  - tempfile.TemporaryDirectory() ichida sqlite DB
  - SelfLearner(path=<tmp_learned.json>) — production learned.json'ga tegmaydi
  - db._DEFAULT_STATE_DIR monkey-patch — SelfLearner._propose_or_apply
    state_dir parametrini bermaydi, shuning uchun module-level constant'ni
    o'zgartiramiz

Run:
    cd ai-trading-agent
    python scripts/test_f1/test_self_learner_gate.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

# loyiha rootini sys.path'ga qo'shish
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# AUTO_APPLY_LEARNING ehtimol oldingi testdan qolgan bo'lishi mumkin — tozalash
os.environ.pop("AUTO_APPLY_LEARNING", None)

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


def _run_scenario(tmpdir: Path) -> tuple[SelfLearner, dict]:
    """50 ta trade simulate qilib SelfLearner + on-disk learned.json qaytaradi.

    DB tmpdir ichida bo'lishi uchun db._DEFAULT_STATE_DIR'ni monkey-patch
    qilamiz (SelfLearner state_dir parametrini bermaydi).
    """
    # Tmp DB redirect — monkey-patch
    db._DEFAULT_STATE_DIR = tmpdir  # type: ignore[attr-defined]

    # AUTO_APPLY_LEARNING explicit'ni o'chiramiz — default false
    if "AUTO_APPLY_LEARNING" in os.environ:
        del os.environ["AUTO_APPLY_LEARNING"]

    learned_path = tmpdir / "learned.json"
    sl = SelfLearner(
        path=str(learned_path),
        lessons_path=str(tmpdir / "lessons.json"),  # yo'q fayl — bo'sh
    )

    # 50 trades: 40 OB + 10 FVG. OB ~66% WR, FVG 90% WR (boost trigger).
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

    # Diskdagi learned.json snapshot
    with open(learned_path, "r", encoding="utf-8") as f:
        on_disk = json.load(f)

    return sl, on_disk


print("\n=== F1-1: SelfLearner approval gate (50-trade scenario) ===")


@test("AUTO_APPLY_LEARNING=false (default) — adapted.setup_weights.OB stays at default 1.0")
def _():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _sl, on_disk = _run_scenario(d)
        default_ob = _DEFAULT_WEIGHTS["OB"]  # 1.0
        actual_ob = on_disk["adapted"]["setup_weights"]["OB"]
        assert actual_ob == default_ob, (
            f"OB weight DEGRADATSIYA — analyze()/evolve() learned.json'ga "
            f"yozib ketdi! Expected {default_ob}, got {actual_ob}. "
            f"Gate ishlamayapti."
        )
        # Boshqa adapted/* qiymatlar ham default
        assert on_disk["adapted"]["entry_pct"] == 0.30, \
            f"entry_pct o'zgardi: {on_disk['adapted']['entry_pct']}"
        assert on_disk["adapted"]["sl_multiplier"] == 1.0, \
            f"sl_multiplier o'zgardi: {on_disk['adapted']['sl_multiplier']}"
        assert on_disk["adapted"]["disabled"] == [], \
            f"disabled list o'zgardi: {on_disk['adapted']['disabled']}"


@test("db.list_pending() returns >= 1 row (setup_weights.* proposal expected)")
def _():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _run_scenario(d)
        pending = db.list_pending(state_dir=d)
        assert len(pending) >= 1, (
            f"50 trade'dan keyin pending bo'sh! analyze()/evolve() propose "
            f"qilmagan. pending={pending}"
        )
        # Eng kamida bittasi setup_weights.* bo'lishi kerak
        setup_props = [p for p in pending if p["param"].startswith("setup_weights.")]
        assert setup_props, (
            f"setup_weights.* propose qilinmadi. Propose qilinganlar: "
            f"{[p['param'] for p in pending]}"
        )


@test("proposed row param matches setup_weights.{OB|FVG|...} pattern")
def _():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _run_scenario(d)
        pending = db.list_pending(state_dir=d)
        # Hech qaysi param bo'sh yoki noma'lum pattern bilan bo'lmasligi kerak
        valid_prefixes = (
            "setup_weights.",
            "disabled.",
            "entry_pct",
            "sl_multiplier",
            "session_weights.",
        )
        for p in pending:
            assert any(p["param"].startswith(pre) or p["param"] == pre
                       for pre in valid_prefixes), \
                f"noma'lum param: {p['param']!r}"
        # OB yoki FVG bo'lishi yuqori ehtimol (40 OB + 10 FVG trade'lar)
        params = {p["param"] for p in pending}
        likely_targets = {"setup_weights.OB", "setup_weights.FVG"}
        assert params & likely_targets, \
            f"OB/FVG propose qilinishi kutilgan, lekin: {params}"


@test("data['trades'] length == 50 (trades logged normally — only adapted gated)")
def _():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        sl, on_disk = _run_scenario(d)
        # In-memory
        assert len(sl.data["trades"]) == 50, \
            f"in-memory trades count: {len(sl.data['trades'])}"
        # On-disk
        assert len(on_disk["trades"]) == 50, \
            f"on-disk trades count: {len(on_disk['trades'])}"


@test("data['stats']['total'] == 50")
def _():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        sl, on_disk = _run_scenario(d)
        assert sl.data["stats"]["total"] == 50, \
            f"in-memory stats.total: {sl.data['stats']['total']}"
        assert on_disk["stats"]["total"] == 50, \
            f"on-disk stats.total: {on_disk['stats']['total']}"


print(f"\nResult: {PASSED} passed, {FAILED} failed")
sys.exit(0 if FAILED == 0 else 1)
