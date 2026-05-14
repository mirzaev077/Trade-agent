"""
F1-1: SQLite-based admin approval gate for self-learner parameter changes.

Stores pending and active adjustments. Idempotent migration on first use.

Tables (see migrations/002_pending_adjustments.sql):
    - schema_migrations    : version tracking
    - pending_adjustments  : proposed param changes awaiting admin decision
    - active_adjustments   : applied changes (audit trail + current-value lookup)

Note on persistence ownership: this module is a SHORTHAND audit / lookup layer.
The self-learner still owns authoritative state in learned.json. Use
get_active_value() for verification, not as primary state.
"""
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from loguru import logger

# Path anchors:
#   parents[5] -> apps/  (same as state/persistence.py, where data/state/ lives)
#   parents[6] -> ai-trading-agent/  (where migrations/ lives, sibling of apps/)
_DEFAULT_STATE_DIR = Path(__file__).resolve().parents[5] / "data" / "state"
_DEFAULT_DB_NAME = "openclaw.db"
_MIGRATIONS_DIR = Path(__file__).resolve().parents[6] / "migrations"
_PENDING_TTL_HOURS = 24


def get_db_path(state_dir: Optional[Path] = None) -> Path:
    # F1-1 test isolation: when no explicit state_dir is passed, allow the
    # OPENCLAW_STATE_DIR environment variable to override the default. This
    # lets subprocess-based tests (admin_cli) redirect the DB to a tmp dir
    # without monkey-patching. Production use is unaffected (env var unset).
    if state_dir is None:
        env_dir = os.getenv("OPENCLAW_STATE_DIR")
        if env_dir:
            state_dir = Path(env_dir)
    d = state_dir or _DEFAULT_STATE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / _DEFAULT_DB_NAME


@contextmanager
def get_connection(state_dir: Optional[Path] = None):
    """Context manager. Always closes. Foreign keys enabled. Row factory = sqlite3.Row.

    isolation_level=None -> autocommit. For multi-statement transactions, callers
    must explicitly BEGIN / COMMIT (or ROLLBACK).
    """
    path = get_db_path(state_dir)
    conn = sqlite3.connect(str(path), isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
    finally:
        conn.close()


def init_db(state_dir: Optional[Path] = None) -> None:
    """Run all numbered *.sql migrations in lexicographic order, idempotently.

    Only files whose stem starts with a digit are applied. This excludes the
    legacy Postgres `schema.sql` which would fail under SQLite (uses pgcrypto,
    pgvector, JSONB, gen_random_uuid, etc.).

    Uses schema_migrations to skip already-applied versions. Each migration
    file's name (sans .sql extension) is the version. Migrations themselves
    use CREATE TABLE IF NOT EXISTS so re-running is safe.
    """
    if not _MIGRATIONS_DIR.exists():
        logger.warning(f"init_db: migrations dir not found at {_MIGRATIONS_DIR}")
        return

    with get_connection(state_dir) as conn:
        for sql_file in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            version = sql_file.stem
            # Skip non-numbered (e.g. legacy Postgres schema.sql)
            if not version or not version[0].isdigit():
                logger.debug(f"init_db: skip non-numbered migration {sql_file.name}")
                continue
            # Check if already applied; gracefully handle missing table on first run
            try:
                cur = conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=?",
                    (version,),
                )
                if cur.fetchone():
                    logger.debug(f"init_db: migration {version} already applied, skip")
                    continue
            except sqlite3.OperationalError:
                # schema_migrations table doesn't exist yet — first migration creates it
                pass
            logger.info(f"init_db: applying migration {version}")
            try:
                conn.executescript(sql_file.read_text(encoding="utf-8"))
            except sqlite3.Error as e:
                logger.error(f"init_db: migration {version} failed — {e}")
                raise


# --- Repository functions ---------------------------------------------------

def propose_adjustment(
    param: str,
    old_value: Optional[float],
    new_value: Optional[float],
    reason: str,
    state_dir: Optional[Path] = None,
) -> str:
    """Insert a new pending adjustment. Returns its UUID4 string ID.

    expires_at = now + 24h (UTC, ISO8601).
    """
    adj_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    proposed_at = now.isoformat()
    expires_at = (now + timedelta(hours=_PENDING_TTL_HOURS)).isoformat()
    with get_connection(state_dir) as conn:
        conn.execute(
            """
            INSERT INTO pending_adjustments
                (id, proposed_at, param, old_value, new_value, reason, status, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (adj_id, proposed_at, param, old_value, new_value, reason, expires_at),
        )
    logger.info(
        f"propose_adjustment: {param} {old_value!r} -> {new_value!r} "
        f"(id={adj_id}, expires={expires_at})"
    )
    return adj_id


def list_pending(
    include_expired: bool = False,
    state_dir: Optional[Path] = None,
) -> list[dict]:
    """Return all pending_adjustments (status='pending'). If include_expired=False
    (default), also exclude rows where expires_at < now. Each row is a plain dict
    with all columns. Ordered by proposed_at DESC.
    """
    with get_connection(state_dir) as conn:
        if include_expired:
            cur = conn.execute(
                "SELECT * FROM pending_adjustments WHERE status='pending' "
                "ORDER BY proposed_at DESC"
            )
        else:
            now = _utc_now_iso()
            cur = conn.execute(
                "SELECT * FROM pending_adjustments "
                "WHERE status='pending' AND expires_at >= ? "
                "ORDER BY proposed_at DESC",
                (now,),
            )
        return [dict(row) for row in cur.fetchall()]


def get_pending(adjustment_id: str, state_dir: Optional[Path] = None) -> Optional[dict]:
    """Return one pending row by id, or None."""
    with get_connection(state_dir) as conn:
        cur = conn.execute(
            "SELECT * FROM pending_adjustments WHERE id=?",
            (adjustment_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def approve_adjustment(
    adjustment_id: str,
    applied_by: str = "admin",
    state_dir: Optional[Path] = None,
) -> Optional[dict]:
    """Mark pending as approved AND insert into active_adjustments.

    Returns the active record dict on success, None if pending_id not found
    or already decided. Wrapped in a single transaction.
    """
    now_iso = _utc_now_iso()
    active_id = str(uuid.uuid4())
    with get_connection(state_dir) as conn:
        try:
            conn.execute("BEGIN")
            cur = conn.execute(
                "SELECT * FROM pending_adjustments WHERE id=?",
                (adjustment_id,),
            )
            pending = cur.fetchone()
            if pending is None:
                conn.execute("ROLLBACK")
                logger.warning(f"approve_adjustment: id={adjustment_id} not found")
                return None
            if pending["status"] != "pending":
                conn.execute("ROLLBACK")
                logger.warning(
                    f"approve_adjustment: id={adjustment_id} already decided "
                    f"(status={pending['status']})"
                )
                return None

            conn.execute(
                "UPDATE pending_adjustments "
                "SET status='approved', decided_at=?, decided_by=? "
                "WHERE id=?",
                (now_iso, applied_by, adjustment_id),
            )
            conn.execute(
                """
                INSERT INTO active_adjustments
                    (id, pending_id, approved_at, param, value, applied_by)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    active_id,
                    adjustment_id,
                    now_iso,
                    pending["param"],
                    pending["new_value"],
                    applied_by,
                ),
            )
            conn.execute("COMMIT")
        except sqlite3.Error as e:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            logger.error(f"approve_adjustment: failed — {e}")
            raise

        cur = conn.execute(
            "SELECT * FROM active_adjustments WHERE id=?",
            (active_id,),
        )
        row = cur.fetchone()
        result = dict(row) if row else None

    logger.info(
        f"approve_adjustment: id={adjustment_id} -> active_id={active_id} "
        f"(param={pending['param']}, value={pending['new_value']!r}, by={applied_by})"
    )
    return result


def reject_adjustment(
    adjustment_id: str,
    reason: str,
    decided_by: str = "admin",
    state_dir: Optional[Path] = None,
) -> bool:
    """Mark pending as rejected. Sets rejected_reason, decided_at, decided_by.

    Returns True if row was updated, False if not found or already decided.
    """
    now_iso = _utc_now_iso()
    with get_connection(state_dir) as conn:
        cur = conn.execute(
            "UPDATE pending_adjustments "
            "SET status='rejected', rejected_reason=?, decided_at=?, decided_by=? "
            "WHERE id=? AND status='pending'",
            (reason, now_iso, decided_by, adjustment_id),
        )
        changed = cur.rowcount > 0
    if changed:
        logger.info(
            f"reject_adjustment: id={adjustment_id} rejected by {decided_by} "
            f"(reason={reason!r})"
        )
    else:
        logger.warning(
            f"reject_adjustment: id={adjustment_id} not updated "
            f"(not found or already decided)"
        )
    return changed


def auto_reject_expired(state_dir: Optional[Path] = None) -> int:
    """For all pending rows where expires_at < now, set status='expired',
    decided_at=now, decided_by='auto-reject'. Return count of rows changed.
    """
    now_iso = _utc_now_iso()
    with get_connection(state_dir) as conn:
        cur = conn.execute(
            "UPDATE pending_adjustments "
            "SET status='expired', decided_at=?, decided_by='auto-reject' "
            "WHERE status='pending' AND expires_at < ?",
            (now_iso, now_iso),
        )
        n = cur.rowcount
    if n:
        logger.info(f"auto_reject_expired: expired {n} pending adjustment(s)")
    return n


def get_active_value(param: str, state_dir: Optional[Path] = None) -> Optional[float]:
    """Return value from the most-recent active_adjustments row matching param
    (ordered by approved_at DESC). None if no active adjustment.

    Note: this is a SHORTHAND — callers (self_learner) still own authoritative
    state in learned.json. This is for audit / verification, not primary state.
    """
    with get_connection(state_dir) as conn:
        cur = conn.execute(
            "SELECT value FROM active_adjustments "
            "WHERE param=? ORDER BY approved_at DESC LIMIT 1",
            (param,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        v = row["value"]
        return float(v) if v is not None else None


def list_active(
    param: Optional[str] = None,
    state_dir: Optional[Path] = None,
) -> list[dict]:
    """List active adjustments, newest first. Filter by param if given."""
    with get_connection(state_dir) as conn:
        if param is None:
            cur = conn.execute(
                "SELECT * FROM active_adjustments ORDER BY approved_at DESC"
            )
        else:
            cur = conn.execute(
                "SELECT * FROM active_adjustments WHERE param=? "
                "ORDER BY approved_at DESC",
                (param,),
            )
        return [dict(row) for row in cur.fetchall()]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
