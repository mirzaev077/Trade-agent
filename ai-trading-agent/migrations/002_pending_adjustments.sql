-- F1-1: SQLite admin-approval gate for self-learner parameter changes.
-- Idempotent: safe to re-run.

-- schema_migrations: version tracking
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- pending_adjustments: proposed param changes awaiting admin decision
CREATE TABLE IF NOT EXISTS pending_adjustments (
    id              TEXT PRIMARY KEY,                  -- UUID4 string
    proposed_at     TEXT NOT NULL DEFAULT (datetime('now')),
    param           TEXT NOT NULL,                     -- e.g. "setup_weights.OB", "entry_pct", "sl_multiplier", "disabled.RB"
    old_value       REAL,                              -- nullable (e.g. enabling a previously disabled setup)
    new_value       REAL,                              -- nullable (e.g. disabling a setup)
    reason          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',   -- pending|approved|rejected|expired
    expires_at      TEXT NOT NULL,                     -- ISO8601 UTC, proposed_at + 24h
    rejected_reason TEXT,
    decided_at      TEXT,                              -- when status changed from pending
    decided_by      TEXT                               -- "admin", "auto-reject", etc.
);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_adjustments(status);
CREATE INDEX IF NOT EXISTS idx_pending_expires ON pending_adjustments(expires_at);

-- active_adjustments: applied changes (audit trail + current-value lookup)
CREATE TABLE IF NOT EXISTS active_adjustments (
    id          TEXT PRIMARY KEY,                       -- UUID4 string (own id, not pending_id)
    pending_id  TEXT,                                   -- FK to pending_adjustments.id (nullable for AUTO_APPLY records)
    approved_at TEXT NOT NULL DEFAULT (datetime('now')),
    param       TEXT NOT NULL,
    value       REAL,
    applied_by  TEXT NOT NULL,                          -- "admin", "auto-apply"
    FOREIGN KEY (pending_id) REFERENCES pending_adjustments(id)
);
CREATE INDEX IF NOT EXISTS idx_active_param ON active_adjustments(param);

INSERT OR IGNORE INTO schema_migrations(version) VALUES ('002_pending_adjustments');
