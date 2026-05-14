"""F1-1 test: state/db.py round-trip smoke

Tekshiruvlar:
  1. init_db() idempotent — ikki marta xatosiz ishlaydi
  2. propose_adjustment() UUID qaytaradi, list_pending() da paydo bo'ladi
  3. expires_at ~ proposed_at + 24h (1 daqiqa tolerance)
  4. approve_adjustment() active dict qaytaradi, pending'dan o'chiriladi, active row paydo bo'ladi
  5. reject_adjustment(reason="X") status=rejected, rejected_reason="X", True qaytaradi
  6. Allaqachon decided bo'lganni approve None, reject False qaytaradi
  7. auto_reject_expired() — qo'lda eski expires_at bilan pending qo'shamiz, expired bo'lishi shart
  8. get_active_value(param) approve'dan keyin qiymat qaytaradi

Test izolyatsiyasi: har test tempfile.TemporaryDirectory() ichida ishlaydi
(state_dir parametri orqali). Production fayllariga TEGILMAYDI.

Run:
    cd ai-trading-agent
    python scripts/test_f1/test_db_layer.py
"""
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# loyiha rootini sys.path'ga qo'shish
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


print("\n=== F1-1: db.py round-trip ===")


@test("init_db idempotent (twice without error)")
def _():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        db.init_db(state_dir=d)
        db.init_db(state_dir=d)  # again
        # schema_migrations row must exist
        with db.get_connection(state_dir=d) as conn:
            cur = conn.execute("SELECT version FROM schema_migrations")
            versions = [r["version"] for r in cur.fetchall()]
        assert "002_pending_adjustments" in versions, \
            f"migration not recorded: {versions}"


@test("propose_adjustment returns UUID, appears in list_pending")
def _():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        db.init_db(state_dir=d)
        adj_id = db.propose_adjustment(
            "setup_weights.OB", 1.0, 1.2, "smoke test", state_dir=d
        )
        assert isinstance(adj_id, str) and len(adj_id) == 36, \
            f"expected UUID4 string, got {adj_id!r}"
        rows = db.list_pending(state_dir=d)
        assert len(rows) == 1, f"expected 1 pending, got {len(rows)}"
        assert rows[0]["id"] == adj_id
        assert rows[0]["param"] == "setup_weights.OB"
        assert rows[0]["new_value"] == 1.2
        assert rows[0]["status"] == "pending"


@test("expires_at ~ proposed_at + 24h (within 1 min tolerance)")
def _():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        db.init_db(state_dir=d)
        before = datetime.now(timezone.utc)
        adj_id = db.propose_adjustment(
            "entry_pct", 0.30, 0.35, "expiry check", state_dir=d
        )
        row = db.get_pending(adj_id, state_dir=d)
        proposed = datetime.fromisoformat(row["proposed_at"])
        expires = datetime.fromisoformat(row["expires_at"])
        delta = expires - proposed
        # exactly 24h expected
        diff_seconds = abs(delta.total_seconds() - 24 * 3600)
        assert diff_seconds < 60, \
            f"expires_at - proposed_at = {delta} (expected ~24h, off by {diff_seconds}s)"
        # also sanity: proposed_at is recent
        assert (proposed - before.replace(microsecond=0)).total_seconds() < 5, \
            "proposed_at not close to now"


@test("approve_adjustment returns active dict, removes from pending, inserts active row")
def _():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        db.init_db(state_dir=d)
        adj_id = db.propose_adjustment(
            "setup_weights.FVG", 1.0, 1.5, "approve test", state_dir=d
        )
        active = db.approve_adjustment(adj_id, applied_by="admin", state_dir=d)
        assert active is not None, "approve_adjustment returned None"
        assert active["param"] == "setup_weights.FVG"
        assert active["value"] == 1.5
        assert active["applied_by"] == "admin"
        # No longer in pending
        pending = db.list_pending(state_dir=d)
        assert all(p["id"] != adj_id for p in pending), \
            f"approved id still listed pending: {pending}"
        # Active list has it
        actives = db.list_active(state_dir=d)
        assert len(actives) == 1
        assert actives[0]["pending_id"] == adj_id


@test("reject_adjustment marks status=rejected, sets rejected_reason, returns True")
def _():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        db.init_db(state_dir=d)
        adj_id = db.propose_adjustment(
            "sl_multiplier", 1.0, 0.9, "reject test", state_dir=d
        )
        ok = db.reject_adjustment(adj_id, reason="too risky", state_dir=d)
        assert ok is True, f"reject returned {ok!r}"
        row = db.get_pending(adj_id, state_dir=d)
        assert row["status"] == "rejected"
        assert row["rejected_reason"] == "too risky"
        assert row["decided_by"] == "admin"
        # Not in active pending list
        pending = db.list_pending(state_dir=d)
        assert all(p["id"] != adj_id for p in pending)


@test("approving already-decided returns None; rejecting returns False")
def _():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        db.init_db(state_dir=d)
        # First case: approve then re-approve
        adj_id = db.propose_adjustment(
            "entry_pct", 0.30, 0.40, "x", state_dir=d
        )
        first = db.approve_adjustment(adj_id, state_dir=d)
        assert first is not None
        second = db.approve_adjustment(adj_id, state_dir=d)
        assert second is None, f"expected None on re-approve, got {second!r}"
        # Reject of already-approved -> False
        rej = db.reject_adjustment(adj_id, reason="late", state_dir=d)
        assert rej is False
        # Second case: reject then re-reject
        adj_id2 = db.propose_adjustment(
            "entry_pct", 0.30, 0.50, "y", state_dir=d
        )
        assert db.reject_adjustment(adj_id2, reason="nope", state_dir=d) is True
        assert db.reject_adjustment(adj_id2, reason="nope2", state_dir=d) is False
        # And re-approve of rejected -> None
        assert db.approve_adjustment(adj_id2, state_dir=d) is None


@test("auto_reject_expired marks past-expires rows as expired")
def _():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        db.init_db(state_dir=d)
        # propose normally
        fresh_id = db.propose_adjustment(
            "setup_weights.OB", 1.0, 1.1, "fresh", state_dir=d
        )
        # Manually insert an old pending with expires_at in the past
        old_id = "11111111-1111-4111-8111-111111111111"
        past = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        past2 = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
        with db.get_connection(state_dir=d) as conn:
            conn.execute(
                "INSERT INTO pending_adjustments "
                "(id, proposed_at, param, old_value, new_value, reason, "
                "status, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                (old_id, past2, "setup_weights.RB", 1.0, 0.5, "stale", past),
            )
        n = db.auto_reject_expired(state_dir=d)
        assert n == 1, f"expected 1 expired, got {n}"
        # Fresh one still pending
        fresh = db.get_pending(fresh_id, state_dir=d)
        assert fresh["status"] == "pending"
        # Old one is expired
        old = db.get_pending(old_id, state_dir=d)
        assert old["status"] == "expired", f"old status={old['status']!r}"
        assert old["decided_by"] == "auto-reject"


@test("get_active_value returns approved value")
def _():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        db.init_db(state_dir=d)
        # No active yet
        assert db.get_active_value("setup_weights.OB", state_dir=d) is None
        # Approve one
        adj_id = db.propose_adjustment(
            "setup_weights.OB", 1.0, 1.25, "x", state_dir=d
        )
        db.approve_adjustment(adj_id, state_dir=d)
        v = db.get_active_value("setup_weights.OB", state_dir=d)
        assert v == 1.25, f"expected 1.25, got {v!r}"
        # Approve a second, more-recent one — most-recent should win
        adj_id2 = db.propose_adjustment(
            "setup_weights.OB", 1.25, 1.40, "y", state_dir=d
        )
        db.approve_adjustment(adj_id2, state_dir=d)
        v2 = db.get_active_value("setup_weights.OB", state_dir=d)
        assert v2 == 1.40, f"expected 1.40 (most recent), got {v2!r}"


print(f"\nResult: {PASSED} passed, {FAILED} failed")
sys.exit(0 if FAILED == 0 else 1)
