"""F1-1 test: admin_cli subprocess integration

Tekshiruvlar:
  1. `list` bo'sh DB da exit 0, "(none)" yoki "0 pending" chiqaradi
  2. propose'dan keyin `list` 1 ta row, exit 0
  3. `approve <prefix>` exit 0, tmp learned.json'ga yozadi
  4. approve'dan keyin `list` bo'sh, `list --active` 1 ta row
  5. `reject <id> --reason ...` exit 0, row rejected
  6. `reject` --reason'siz exit != 0 (argparse error)
  7. `auto-reject` exit 0, count chiqaradi

Test izolyatsiyasi:
  - OPENCLAW_STATE_DIR env -> tmp dir (DB tmp ichida)
  - OPENCLAW_LEARNED_JSON env -> tmp learned.json
  Production fayllariga TEGILMAYDI.

Run:
    cd ai-trading-agent
    python scripts/test_f1/test_admin_cli.py
"""
import json
import os
import sys
import subprocess
import tempfile
from pathlib import Path

# loyiha rootini sys.path'ga qo'shish (propose qilish uchun db'ga kerak)
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from apps.api.src.agents.trader.state import db


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


def _cli(args: list[str], state_dir: Path, learned_json: Path):
    """admin_cli ni subprocess'da chaqirib (stdout, stderr, returncode) qaytaradi."""
    env = os.environ.copy()
    env["OPENCLAW_STATE_DIR"] = str(state_dir)
    env["OPENCLAW_LEARNED_JSON"] = str(learned_json)
    # ANSI ranglarni o'chirish — assertion'lar oson
    env["NO_COLOR"] = "1"
    # Python output bufferini o'chirish
    env["PYTHONUNBUFFERED"] = "1"
    # ai-trading-agent/ ni cwd qilish
    proc = subprocess.run(
        [sys.executable, "-m", "apps.api.src.tools.admin_cli", *args],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    return proc.stdout, proc.stderr, proc.returncode


print("\n=== F1-1: admin_cli subprocess ===")


@test("list on empty DB -> exit 0, '(none)' shown")
def _():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        learned = d / "learned.json"
        out, err, rc = _cli(["list"], d, learned)
        assert rc == 0, f"rc={rc}, stderr={err!r}"
        assert "(none)" in out, f"'(none)' yo'q. stdout:\n{out}"
        # 0 ta row
        assert "PENDING ADJUSTMENTS (0)" in out, \
            f"header noto'g'ri. stdout:\n{out}"


@test("after propose -> list shows 1 row, exit 0")
def _():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        learned = d / "learned.json"
        db.init_db(state_dir=d)
        # propose programmatically via db (state_dir parametri orqali)
        adj_id = db.propose_adjustment(
            "setup_weights.OB", 1.0, 1.3, "test prop", state_dir=d
        )
        out, err, rc = _cli(["list"], d, learned)
        assert rc == 0, f"rc={rc}, stderr={err!r}"
        assert adj_id[:8] in out, f"id prefix yo'q. stdout:\n{out}"
        assert "setup_weights.OB" in out
        assert "PENDING ADJUSTMENTS (1)" in out


@test("approve <prefix> -> exit 0, writes tmp learned.json")
def _():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        learned = d / "learned.json"
        # Bo'sh learned.json yaratamiz — admin_cli undagi setup_weights ga
        # yozishi kerak
        learned.write_text(json.dumps({
            "trades": [],
            "adapted": {
                "setup_weights": {"OB": 1.0},
                "entry_pct": 0.30,
                "sl_multiplier": 1.0,
                "disabled": [],
                "session_weights": {},
            },
            "stats": {"total": 0, "last_analysis_at": 0, "last_evolution_at": 0},
            "session_day_stats": {},
        }), encoding="utf-8")
        db.init_db(state_dir=d)
        adj_id = db.propose_adjustment(
            "setup_weights.OB", 1.0, 1.5, "approve test", state_dir=d
        )
        prefix = adj_id[:8]
        out, err, rc = _cli(["approve", prefix], d, learned)
        assert rc == 0, f"rc={rc}, stderr={err!r}, stdout={out!r}"
        assert "APPROVED" in out, f"APPROVED yo'q. stdout:\n{out}"
        # learned.json yangilandi
        data = json.loads(learned.read_text(encoding="utf-8"))
        assert data["adapted"]["setup_weights"]["OB"] == 1.5, \
            f"learned.json yangilanmadi: {data['adapted']['setup_weights']}"


@test("after approve -> list shows none, list --active shows 1")
def _():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        learned = d / "learned.json"
        learned.write_text(json.dumps({
            "trades": [],
            "adapted": {
                "setup_weights": {"OB": 1.0},
                "entry_pct": 0.30,
                "sl_multiplier": 1.0,
                "disabled": [],
                "session_weights": {},
            },
            "stats": {"total": 0, "last_analysis_at": 0, "last_evolution_at": 0},
            "session_day_stats": {},
        }), encoding="utf-8")
        db.init_db(state_dir=d)
        adj_id = db.propose_adjustment(
            "setup_weights.OB", 1.0, 1.6, "x", state_dir=d
        )
        _cli(["approve", adj_id[:8]], d, learned)

        out_p, _, rc_p = _cli(["list"], d, learned)
        assert rc_p == 0
        assert "(none)" in out_p
        assert "PENDING ADJUSTMENTS (0)" in out_p, \
            f"approve'dan keyin pending bor: {out_p}"

        out_a, _, rc_a = _cli(["list", "--active"], d, learned)
        assert rc_a == 0
        assert "ACTIVE ADJUSTMENTS (1)" in out_a, \
            f"active 1 ta bo'lishi kerak: {out_a}"
        assert "setup_weights.OB" in out_a


@test("reject <id> --reason ... -> exit 0, row rejected")
def _():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        learned = d / "learned.json"
        db.init_db(state_dir=d)
        adj_id = db.propose_adjustment(
            "entry_pct", 0.30, 0.50, "reject me", state_dir=d
        )
        out, err, rc = _cli(
            ["reject", adj_id[:8], "--reason", "too aggressive"],
            d, learned,
        )
        assert rc == 0, f"rc={rc}, stderr={err!r}"
        assert "REJECTED" in out, f"REJECTED yo'q: {out}"
        # DB verification
        row = db.get_pending(adj_id, state_dir=d)
        assert row["status"] == "rejected"
        assert row["rejected_reason"] == "too aggressive"


@test("reject without --reason -> non-zero exit (argparse error)")
def _():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        learned = d / "learned.json"
        db.init_db(state_dir=d)
        adj_id = db.propose_adjustment(
            "entry_pct", 0.30, 0.50, "x", state_dir=d
        )
        out, err, rc = _cli(["reject", adj_id[:8]], d, learned)
        assert rc != 0, \
            f"--reason'siz reject 0 qaytarmasligi kerak. rc={rc}, stdout={out!r}"
        # argparse error normally to stderr
        combined = (out + err).lower()
        assert "reason" in combined, \
            f"error message'da 'reason' bo'lishi kerak: {err!r} / {out!r}"


@test("auto-reject -> exit 0, prints count")
def _():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        learned = d / "learned.json"
        # Bo'sh DB: count = 0
        out, err, rc = _cli(["auto-reject"], d, learned)
        assert rc == 0, f"rc={rc}, stderr={err!r}"
        assert "auto-rejected" in out.lower(), \
            f"output: {out!r}"
        assert "0" in out


print(f"\nResult: {PASSED} passed, {FAILED} failed")
sys.exit(0 if FAILED == 0 else 1)
