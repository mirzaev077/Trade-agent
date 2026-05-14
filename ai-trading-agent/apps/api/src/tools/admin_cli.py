"""
F1-1 Admin Approval Gate CLI.

Inspect, approve, or reject pending parameter adjustments proposed by the
self-learner. Approval also mutates ``learned.json`` so the change takes
effect immediately; rejection / expiry are audit-only.

Run from ``ai-trading-agent/`` (the directory where ``apps/`` lives):

    python -m apps.api.src.tools.admin_cli list
    python -m apps.api.src.tools.admin_cli list --all
    python -m apps.api.src.tools.admin_cli list --active
    python -m apps.api.src.tools.admin_cli show <id-or-prefix>
    python -m apps.api.src.tools.admin_cli approve <id-or-prefix> [--by alice]
    python -m apps.api.src.tools.admin_cli reject  <id-or-prefix> --reason "..." [--by alice]
    python -m apps.api.src.tools.admin_cli auto-reject

Either the full UUID or any unique prefix (>=4 chars) of an adjustment ID is
accepted by ``show``, ``approve`` and ``reject``.

Stdlib only — no third-party deps. ANSI colors are auto-detected from
``stdout.isatty()`` and stripped otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from apps.api.src.agents.trader.state import db


# ── learned.json path resolution (mirrors self_learner._DEFAULT_PATH) ─────────
# self_learner.py:  base = os.path.dirname(__file__)  → .../agents/trader/brain/
#                   path = base / "learned.json"
# This file lives at .../apps/api/src/tools/admin_cli.py, so:
# F1-1 test isolation: OPENCLAW_LEARNED_JSON env var overrides the path so
# subprocess tests can redirect to a tmp file without monkey-patching.
_LEARNED_JSON_PATH: Path = Path(
    os.environ.get(
        "OPENCLAW_LEARNED_JSON",
        str(
            Path(__file__).resolve().parents[1]
            / "agents"
            / "trader"
            / "brain"
            / "learned.json"
        ),
    )
)


# ── ANSI color helpers ────────────────────────────────────────────────────────

def _use_color() -> bool:
    """Color only when stdout is a real TTY. NO_COLOR env disables it."""
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


_C_RESET = "\033[0m"
_C_GREEN = "\033[32m"
_C_RED = "\033[31m"
_C_YELLOW = "\033[33m"
_C_BOLD = "\033[1m"
_C_DIM = "\033[2m"


def _c(text: str, color: str) -> str:
    return f"{color}{text}{_C_RESET}" if _use_color() else text


# ── Unicode-vs-ASCII glyph helpers ────────────────────────────────────────────
# Windows default consoles often use cp1251 / cp1252 which can't render box-
# drawing or arrows. Detect at startup and downgrade gracefully.

def _stdout_can_encode(s: str) -> bool:
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        s.encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


_UNICODE_OK: bool = _stdout_can_encode("─→✓✗")

_HR_CHAR: str = "─" if _UNICODE_OK else "-"
_ARROW: str = "→" if _UNICODE_OK else "->"
_CHECK: str = "✓" if _UNICODE_OK else "OK"
_CROSS: str = "✗" if _UNICODE_OK else "X"


def _hr(width: int = 70) -> str:
    return _HR_CHAR * width


# ── Time / formatting helpers ─────────────────────────────────────────────────

def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        # Python's fromisoformat handles +00:00 in 3.11+. Defensive: strip Z.
        s2 = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s2)
    except (ValueError, TypeError):
        return None


def _humanize_delta(seconds: float) -> str:
    """``12m``, ``1h23m``, ``2d3h``. Always non-negative cosmetic output."""
    s = int(abs(seconds))
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d{h:02d}h"


def _age(row: dict) -> str:
    proposed = _parse_iso(row.get("proposed_at"))
    if proposed is None:
        return "?"
    now = datetime.now(timezone.utc)
    if proposed.tzinfo is None:
        proposed = proposed.replace(tzinfo=timezone.utc)
    return _humanize_delta((now - proposed).total_seconds())


def _expires_in(row: dict) -> str:
    exp = _parse_iso(row.get("expires_at"))
    if exp is None:
        return "?"
    now = datetime.now(timezone.utc)
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    delta = (exp - now).total_seconds()
    if delta < 0:
        return _c(f"-{_humanize_delta(delta)}", _C_RED)
    return _humanize_delta(delta)


def _fmt_value(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, float):
        # Show enough precision for typical weights/multipliers without trailing junk.
        if abs(v) >= 1:
            return f"{v:.4f}".rstrip("0").rstrip(".") or "0"
        return f"{v:.4f}"
    return str(v)


# ── Prefix resolution ─────────────────────────────────────────────────────────

def _resolve_id(prefix: str, pool: list[dict]) -> dict:
    """Find a single row whose ``id`` starts with ``prefix``.

    Raises ``SystemExit(1)`` with a helpful message on no-match or ambiguity.
    Exact-match wins even if it would also be a prefix of others.
    """
    if not prefix:
        print(_c("error: empty id", _C_RED), file=sys.stderr)
        sys.exit(1)

    # Exact match first.
    for r in pool:
        if r["id"] == prefix:
            return r

    matches = [r for r in pool if r["id"].startswith(prefix)]
    if not matches:
        print(
            _c(f"error: no adjustment matches '{prefix}'", _C_RED),
            file=sys.stderr,
        )
        sys.exit(1)
    if len(matches) > 1:
        print(
            _c(
                f"error: prefix '{prefix}' is ambiguous, matches {len(matches)} rows:",
                _C_RED,
            ),
            file=sys.stderr,
        )
        for r in matches:
            print(f"  {r['id']}  ({r.get('param', '?')})", file=sys.stderr)
        sys.exit(1)
    return matches[0]


def _resolve_pending(prefix: str) -> dict:
    """Resolve from currently-pending rows. Pulls fresh from DB."""
    pool = db.list_pending(include_expired=True)
    return _resolve_id(prefix, pool)


def _resolve_any(prefix: str) -> dict:
    """Resolve from pending + all active rows (for ``show``)."""
    # Pending (incl. expired & decided isn't returned by list_pending — but for
    # ``show`` we want to also reach decided rows. Get them via a direct lookup
    # too: try full-id get_pending first if it looks like a full uuid.
    pool: list[dict] = list(db.list_pending(include_expired=True))
    seen: set[str] = {r["id"] for r in pool}
    # Active list rows have different schema; include them too with a flag.
    for a in db.list_active():
        if a["id"] not in seen:
            pool.append(a)
            seen.add(a["id"])
    # Also: pending rows that are already decided (rejected/expired/approved).
    # list_pending only returns status='pending'. Reach decided rows by trying
    # get_pending(prefix) only if prefix looks like a full UUID.
    if len(prefix) >= 32:
        row = db.get_pending(prefix)
        if row and row["id"] not in seen:
            pool.append(row)
    return _resolve_id(prefix, pool)


# ── learned.json mutation ─────────────────────────────────────────────────────

def _atomic_write_json(path: Path, data: dict) -> None:
    """Match state/persistence.py's atomic write idiom."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".learned_", suffix=".json", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _apply_to_learned_json(
    param: str, new_value: Optional[float]
) -> tuple[bool, str]:
    """Mutate ``learned.json`` according to the param naming convention.

    Returns ``(applied, message)``. ``applied=False`` is non-fatal — the DB
    row is still a valid audit trail. Caller should still print the message.
    """
    if not _LEARNED_JSON_PATH.exists():
        return False, (
            f"learned.json not found at {_LEARNED_JSON_PATH} — "
            f"audit row written, but no live state to update"
        )

    try:
        with open(_LEARNED_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return False, f"learned.json read failed ({e}) — audit-only"

    adapted = data.setdefault("adapted", {})

    # --- Routing on param name ---
    if param.startswith("setup_weights."):
        setup = param.split(".", 1)[1]
        weights = adapted.setdefault("setup_weights", {})
        if new_value is None:
            return False, (
                f"refusing to set setup_weights.{setup} to NULL "
                f"(use 'disabled.{setup}' to disable instead)"
            )
        weights[setup] = float(new_value)

    elif param.startswith("disabled."):
        setup = param.split(".", 1)[1]
        disabled = adapted.setdefault("disabled", [])
        if not isinstance(disabled, list):
            disabled = []
            adapted["disabled"] = disabled
        if new_value is None:
            # Disable the setup
            if setup not in disabled:
                disabled.append(setup)
        else:
            # Re-enable: remove from disabled list
            if setup in disabled:
                disabled.remove(setup)
            # Also restore its weight if requested
            weights = adapted.setdefault("setup_weights", {})
            weights[setup] = float(new_value)

    elif param in ("entry_pct", "sl_multiplier"):
        if new_value is None:
            return False, f"refusing to set {param} to NULL"
        adapted[param] = float(new_value)

    elif param.startswith("session_weights."):
        key = param.split(".", 1)[1]
        sw = adapted.setdefault("session_weights", {})
        if new_value is None:
            sw.pop(key, None)
        else:
            sw[key] = float(new_value)

    else:
        return False, f"unknown param convention '{param}' — audit-only"

    try:
        _atomic_write_json(_LEARNED_JSON_PATH, data)
    except OSError as e:
        return False, f"learned.json write failed ({e}) — audit-only"

    return True, f"learned.json updated ({_LEARNED_JSON_PATH})"


# ── Subcommand: list ──────────────────────────────────────────────────────────

def _cmd_list(args: argparse.Namespace) -> int:
    db.init_db()

    if args.active:
        rows = db.list_active()
        header = f"ACTIVE ADJUSTMENTS ({len(rows)})"
        print(_c(header, _C_BOLD))
        print(_hr())
        if not rows:
            print(_c("(none)", _C_DIM))
            return 0
        print(f"{'ID':<10} {'PARAM':<24} {'VALUE':<14} {'APPROVED':<22} BY")
        for r in rows:
            short = r["id"][:8]
            print(
                f"{short:<10} "
                f"{(r.get('param') or ''):<24} "
                f"{_fmt_value(r.get('value')):<14} "
                f"{(r.get('approved_at') or ''):<22} "
                f"{r.get('applied_by') or ''}"
            )
        return 0

    rows = db.list_pending(include_expired=args.all)
    title = "ALL PENDING (incl. expired)" if args.all else "PENDING ADJUSTMENTS"
    print(_c(f"{title} ({len(rows)})", _C_BOLD))
    print(_hr())
    if not rows:
        print(_c("(none)", _C_DIM))
        return 0

    print(
        f"{'ID':<10} {'PARAM':<24} {'OLD ' + _ARROW + ' NEW':<22} {'AGE':<8} EXPIRES"
    )
    for r in rows:
        short = r["id"][:8]
        old = _fmt_value(r.get("old_value"))
        new = _fmt_value(r.get("new_value"))
        change = f"{old} {_ARROW} {new}"
        print(
            f"{short:<10} "
            f"{(r.get('param') or ''):<24} "
            f"{change:<22} "
            f"{_age(r):<8} "
            f"{_expires_in(r)}"
        )

    print(_hr())
    print(_c("Reasons:", _C_DIM))
    for r in rows:
        short = r["id"][:8]
        reason = (r.get("reason") or "").strip() or "(no reason given)"
        print(f"  {short}  {reason}")
    return 0


# ── Subcommand: show ──────────────────────────────────────────────────────────

def _cmd_show(args: argparse.Namespace) -> int:
    db.init_db()
    row = _resolve_any(args.id)
    print(_c(f"Adjustment {row['id']}", _C_BOLD))
    print(_hr())
    for k, v in row.items():
        if k == "id":
            continue
        label = k.replace("_", " ")
        print(f"  {label:<18} {v}")
    return 0


# ── Subcommand: approve ───────────────────────────────────────────────────────

def _cmd_approve(args: argparse.Namespace) -> int:
    db.init_db()
    row = _resolve_pending(args.id)
    if row["status"] != "pending":
        print(
            _c(
                f"error: adjustment {row['id'][:8]} is already "
                f"{row['status']!r} — cannot approve",
                _C_RED,
            ),
            file=sys.stderr,
        )
        return 1

    active = db.approve_adjustment(row["id"], applied_by=args.by)
    if active is None:
        print(
            _c(
                f"error: approve_adjustment returned None for {row['id'][:8]} "
                f"(race or missing row)",
                _C_RED,
            ),
            file=sys.stderr,
        )
        return 1

    applied, msg = _apply_to_learned_json(row["param"], row["new_value"])

    short = row["id"][:8]
    mark = _c(_CHECK, _C_GREEN)
    print(f"{mark} APPROVED {short}")
    print(f"  Param: {row['param']}")
    print(f"  Old:   {_fmt_value(row.get('old_value'))}")
    print(f"  New:   {_fmt_value(row.get('new_value'))}")
    learned_line = "yes" if applied else _c(f"no — {msg}", _C_YELLOW)
    print(f"  Applied to learned.json: {learned_line}")
    if applied:
        print(_c(f"  ({msg})", _C_DIM))
    return 0


# ── Subcommand: reject ────────────────────────────────────────────────────────

def _cmd_reject(args: argparse.Namespace) -> int:
    db.init_db()
    row = _resolve_pending(args.id)
    if row["status"] != "pending":
        print(
            _c(
                f"error: adjustment {row['id'][:8]} is already "
                f"{row['status']!r} — cannot reject",
                _C_RED,
            ),
            file=sys.stderr,
        )
        return 1
    ok = db.reject_adjustment(row["id"], reason=args.reason, decided_by=args.by)
    if not ok:
        print(
            _c(
                f"error: reject_adjustment failed for {row['id'][:8]} "
                f"(not found or already decided)",
                _C_RED,
            ),
            file=sys.stderr,
        )
        return 1
    short = row["id"][:8]
    mark = _c(_CROSS, _C_RED)
    print(f"{mark} REJECTED {short}")
    print(f"  Reason: {args.reason}")
    return 0


# ── Subcommand: auto-reject ───────────────────────────────────────────────────

def _cmd_auto_reject(args: argparse.Namespace) -> int:
    db.init_db()
    n = db.auto_reject_expired()
    print(f"auto-rejected {n} expired pending adjustment(s)")
    return 0


# ── Argparse wiring ───────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="admin_cli",
        description=(
            "F1-1 admin approval gate for the OpenClaw self-learner. "
            "Inspect, approve, or reject parameter adjustments. "
            "Approval mutates learned.json so the change is live immediately."
        ),
        epilog=(
            "Examples:\n"
            "  python -m apps.api.src.tools.admin_cli list\n"
            "  python -m apps.api.src.tools.admin_cli list --all\n"
            "  python -m apps.api.src.tools.admin_cli list --active\n"
            "  python -m apps.api.src.tools.admin_cli show 5ca35e79\n"
            "  python -m apps.api.src.tools.admin_cli approve 5ca35e79 --by abdulaziz\n"
            "  python -m apps.api.src.tools.admin_cli reject  5ca35e79 "
            "--reason 'too few samples'\n"
            "  python -m apps.api.src.tools.admin_cli auto-reject\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", metavar="<subcommand>", required=True)

    p_list = sub.add_parser(
        "list",
        help="Show pending adjustments (default: non-expired only).",
        description="Show pending or active adjustments.",
    )
    grp = p_list.add_mutually_exclusive_group()
    grp.add_argument(
        "--all",
        action="store_true",
        help="Include expired pending rows.",
    )
    grp.add_argument(
        "--active",
        action="store_true",
        help="Show currently-applied active_adjustments instead.",
    )
    p_list.set_defaults(func=_cmd_list)

    p_show = sub.add_parser(
        "show",
        help="Show full details of one adjustment (pending or active).",
    )
    p_show.add_argument("id", help="Full UUID or unique prefix.")
    p_show.set_defaults(func=_cmd_show)

    p_appr = sub.add_parser(
        "approve",
        help="Approve a pending adjustment and apply it to learned.json.",
    )
    p_appr.add_argument("id", help="Full UUID or unique prefix.")
    p_appr.add_argument(
        "--by",
        default="admin",
        help="Operator name recorded as decided_by/applied_by (default: admin).",
    )
    p_appr.set_defaults(func=_cmd_approve)

    p_rej = sub.add_parser(
        "reject",
        help="Reject a pending adjustment (audit-only, learned.json untouched).",
    )
    p_rej.add_argument("id", help="Full UUID or unique prefix.")
    p_rej.add_argument(
        "--reason",
        required=True,
        help="Why the proposal is being rejected (required).",
    )
    p_rej.add_argument(
        "--by",
        default="admin",
        help="Operator name (default: admin).",
    )
    p_rej.set_defaults(func=_cmd_reject)

    p_auto = sub.add_parser(
        "auto-reject",
        help="Mark all expired pending rows as status='expired'.",
    )
    p_auto.set_defaults(func=_cmd_auto_reject)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SystemExit:
        raise
    except Exception:
        print(
            _c("unhandled exception:", _C_RED),
            file=sys.stderr,
        )
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
