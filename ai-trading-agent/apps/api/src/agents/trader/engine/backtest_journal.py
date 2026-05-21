"""
BacktestJournal — backtest natijalarini DB jadvallariga yozadi va in-memory mirror saqlaydi.

Schema: TMAS migrations/002_backtest_tables.sql (backtest_runs, backtest_trades,
backtest_equity_curve, backtest_blocked_signals, backtest_rejected_signals).

DB pattern (sync — backtest loop sync bo'lgani uchun):
  * ``db_conn=None`` → silent no-op (ReflectorAgent pattern), in-memory mirror
    to'la ishlaydi. F2-2 MVP shu rejimda ishlaydi (no DB).
  * ``db_conn`` → sync Postgres connection (psycopg/psycopg2 cursor-style execute).

Run-by-run isolation: barcha yozuvlar ``run_id`` bilan key qilingan.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any

from apps.api.src.agents.trader.core.broker import ClosedTrade, OrderResult
from apps.api.src.agents.trader.engine.signals import RiskCheckResult, Signal


class BacktestJournal:
    """
    Backtest yozuv qatlami — DB + in-memory mirror.

    :param run_id: Backtest run unikal identifikator (BacktestConfig.run_id)
    :param db_conn: Sync DB connection (psycopg/psycopg2 cursor pattern) yoki None
    """

    def __init__(self, run_id: str, db_conn: Any = None) -> None:
        self.run_id = run_id
        self.db_conn = db_conn

        # In-memory mirror (testlar uchun va DB yo'q bo'lganda asosiy storage)
        self._trades: dict[int, dict] = {}  # ticket → trade row
        self._equity_curve: list[dict] = []
        self._blocked: list[dict] = []
        self._rejected: list[dict] = []
        self._run_meta: dict = {}

    # ── Run lifecycle ─────────────────────────────────────────────────────────

    def log_run_started(self, config_dict: dict, git_commit: str | None = None) -> None:
        """Backtest run boshlanganini `backtest_runs` jadvaliga yozadi."""
        started_at = datetime.now(tz=config_dict.get("_tz")) if "_tz" in config_dict else None
        self._run_meta = {
            "run_id": self.run_id,
            "config": config_dict,
            "git_commit": git_commit,
            "started_at": started_at,
            "ended_at": None,
            "metrics": None,
            "verdict": "PENDING",
        }
        self._exec(
            """
            INSERT INTO backtest_runs (run_id, config, git_commit, started_at, verdict)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (run_id) DO NOTHING
            """,
            (self.run_id, json.dumps(config_dict, default=str), git_commit, started_at, "PENDING"),
        )

    def log_run_ended(self, result_dict: dict) -> None:
        """Backtest run yakunlanganini `backtest_runs` ga yozadi (metrics + verdict)."""
        self._run_meta["ended_at"] = result_dict.get("ended_at")
        self._run_meta["metrics"] = result_dict
        self._run_meta["verdict"] = result_dict.get("verdict", "PENDING")

        self._exec(
            """
            UPDATE backtest_runs
            SET ended_at = %s, metrics = %s, verdict = %s
            WHERE run_id = %s
            """,
            (
                result_dict.get("ended_at"),
                json.dumps(result_dict, default=str),
                result_dict.get("verdict", "PENDING"),
                self.run_id,
            ),
        )

    # ── Trade lifecycle ───────────────────────────────────────────────────────

    def log_trade_opened(
        self,
        signal: Signal,
        order_result: OrderResult,
        risk_check: RiskCheckResult,
        reflection: Any,
    ) -> None:
        """Yangi pozitsiya ochilganini `backtest_trades` jadvaliga yozadi.

        Pending limit orderlar uchun (status="pending"): broker hali fill qilmagan,
        shuning uchun `fill_price`/`commission`/`slippage` None. Bu fieldlarni
        signaldan to'ldiramiz — pending fill `order.entry_price` ga (= signal.entry_price)
        slippage'siz amalga oshadi, commission fill paytida hisoblanadi. Pending hech
        qachon fill bo'lmasa, bu qatorda `exit_time=None` qolib, `get_closed_trades`
        filtridan tushadi.
        """
        if not order_result.success or order_result.ticket is None:
            return

        is_pending = order_result.status == "pending"
        recorded_entry = order_result.fill_price if not is_pending else signal.entry_price
        recorded_commission = order_result.commission if not is_pending else 0.0
        recorded_slippage = order_result.slippage if not is_pending else 0.0

        row = {
            "run_id": self.run_id,
            "ticket": order_result.ticket,
            "symbol": signal.symbol,
            "direction": signal.direction,
            "lot": risk_check.lot_size,
            "entry_price": recorded_entry,
            "entry_time": signal.timestamp,
            "sl": signal.sl,
            "tp": signal.tp,
            "commission": recorded_commission,
            "slippage": recorded_slippage,
            "swap": 0.0,
            "pnl": None,
            "exit_price": None,
            "exit_time": None,
            "close_reason": None,
            "signal": _signal_to_dict(signal),
            "risk_check": _risk_check_to_dict(risk_check),
            "reflection": _reflection_to_dict(reflection),
            "setup_type": signal.setup_type,
            "session": signal.session,
            "day_of_week": signal.day_of_week,
            "mode": signal.mode,
            "regime": signal.metadata.get("regime"),
        }
        self._trades[order_result.ticket] = row

        self._exec(
            """
            INSERT INTO backtest_trades
                (run_id, ticket, symbol, direction, lot,
                 entry_price, entry_time, sl, tp,
                 commission, slippage, swap,
                 signal, risk_check,
                 setup_type, session, day_of_week, mode, regime)
            VALUES (%s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s,
                    %s, %s, %s, %s, %s)
            """,
            (
                self.run_id, order_result.ticket, signal.symbol, signal.direction, risk_check.lot_size,
                recorded_entry, signal.timestamp, signal.sl, signal.tp,
                recorded_commission, recorded_slippage, 0.0,
                json.dumps(row["signal"], default=str), json.dumps(row["risk_check"], default=str),
                signal.setup_type, signal.session, signal.day_of_week, signal.mode, row["regime"],
            ),
        )

    def log_trade_closed(self, closed: ClosedTrade) -> None:
        """Pozitsiya yopilganida exit ma'lumotlarini yangilaydi."""
        row = self._trades.get(closed.ticket)
        if row is not None:
            row["exit_price"] = closed.exit_price
            row["exit_time"] = closed.exit_time
            row["pnl"] = closed.pnl
            row["close_reason"] = closed.close_reason
            row["swap"] = closed.swap

        self._exec(
            """
            UPDATE backtest_trades
            SET exit_price = %s, exit_time = %s, pnl = %s, close_reason = %s, swap = %s
            WHERE run_id = %s AND ticket = %s
            """,
            (
                closed.exit_price, closed.exit_time, closed.pnl, closed.close_reason, closed.swap,
                self.run_id, closed.ticket,
            ),
        )

    def log_blocked_signal(self, signal: Signal, reflection: Any) -> None:
        """Reflector tomonidan bloklangan signal'ni `backtest_blocked_signals` ga yozadi."""
        row = {
            "run_id": self.run_id,
            "timestamp": signal.timestamp,
            "symbol": signal.symbol,
            "reason": reflection.block_reason,
            "signal": _signal_to_dict(signal),
            "reflection": _reflection_to_dict(reflection),
        }
        self._blocked.append(row)

        self._exec(
            """
            INSERT INTO backtest_blocked_signals
                (run_id, "timestamp", symbol, reason, signal, reflection)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                self.run_id, signal.timestamp, signal.symbol,
                reflection.block_reason,
                json.dumps(row["signal"], default=str),
                json.dumps(row["reflection"], default=str),
            ),
        )

    def log_rejected_signal(self, signal: Signal, risk_check: RiskCheckResult) -> None:
        """Risk gate tomonidan rad etilgan signal'ni `backtest_rejected_signals` ga yozadi."""
        row = {
            "run_id": self.run_id,
            "timestamp": signal.timestamp,
            "symbol": signal.symbol,
            "reason": risk_check.reason,
            "signal": _signal_to_dict(signal),
            "risk_check": _risk_check_to_dict(risk_check),
        }
        self._rejected.append(row)

        self._exec(
            """
            INSERT INTO backtest_rejected_signals
                (run_id, "timestamp", symbol, reason, signal, risk_check)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                self.run_id, signal.timestamp, signal.symbol,
                risk_check.reason,
                json.dumps(row["signal"], default=str),
                json.dumps(row["risk_check"], default=str),
            ),
        )

    # ── Equity curve ──────────────────────────────────────────────────────────

    def record_equity_point(
        self,
        timestamp: datetime,
        equity: float,
        balance: float,
        drawdown_pct: float = 0.0,
    ) -> None:
        """`backtest_equity_curve` jadvaliga equity snapshot yozadi."""
        if timestamp.tzinfo is None:
            raise ValueError("BacktestJournal.record_equity_point: timestamp must be tz-aware (UTC).")

        point = {
            "run_id": self.run_id,
            "timestamp": timestamp,
            "equity": equity,
            "balance": balance,
            "drawdown_pct": drawdown_pct,
        }
        self._equity_curve.append(point)

        self._exec(
            """
            INSERT INTO backtest_equity_curve (run_id, "timestamp", equity, balance, drawdown_pct)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (run_id, "timestamp") DO NOTHING
            """,
            (self.run_id, timestamp, equity, balance, drawdown_pct),
        )

    def daily_snapshot(self, broker: Any, timestamp: datetime) -> None:
        """Kunlik equity/balance snapshot — broker holatidan o'qiydi."""
        if not self._equity_curve:
            peak = broker.equity
        else:
            peak = max(p["equity"] for p in self._equity_curve)
        peak = max(peak, broker.equity)
        dd_pct = (peak - broker.equity) / peak * 100.0 if peak > 0 else 0.0
        self.record_equity_point(timestamp, broker.equity, broker.balance, dd_pct)

    # ── In-memory accessors (testlar uchun) ───────────────────────────────────

    def get_all_trades(self) -> list[dict]:
        return list(self._trades.values())

    def get_open_trades(self) -> list[dict]:
        return [t for t in self._trades.values() if t["exit_time"] is None]

    def get_closed_trades(self) -> list[dict]:
        return [t for t in self._trades.values() if t["exit_time"] is not None]

    def get_equity_curve(self) -> list[dict]:
        return list(self._equity_curve)

    def get_blocked_signals(self) -> list[dict]:
        return list(self._blocked)

    def get_rejected_signals(self) -> list[dict]:
        return list(self._rejected)

    def get_run_meta(self) -> dict:
        return dict(self._run_meta)

    # ── Private: DB exec ──────────────────────────────────────────────────────

    def _exec(self, sql: str, params: tuple) -> None:
        """
        DB ga sync execute yuboradi. db_conn=None bo'lsa silent no-op.

        Expected interface: db_conn.cursor().execute(sql, params), commit on caller.
        Pattern: psycopg/psycopg2 sync mode.
        """
        if self.db_conn is None:
            return

        cursor = self.db_conn.cursor()
        try:
            cursor.execute(sql, params)
        finally:
            cursor.close()


# ── Helpers ──────────────────────────────────────────────────────────────────


def _signal_to_dict(signal: Signal) -> dict:
    """Signal → JSON-safe dict (datetime ISO 8601)."""
    return {
        "signal_id": signal.signal_id,
        "symbol": signal.symbol,
        "direction": signal.direction,
        "entry_price": signal.entry_price,
        "sl": signal.sl,
        "tp": signal.tp,
        "confluence_score": signal.confluence_score,
        "setup_type": signal.setup_type,
        "session": signal.session,
        "day_of_week": signal.day_of_week,
        "mode": signal.mode,
        "timestamp": signal.timestamp.isoformat(),
        "metadata": signal.metadata,
    }


def _risk_check_to_dict(risk_check: Any) -> dict:
    """RiskCheckResult-like → JSON-safe dict (asdict if dataclass)."""
    if is_dataclass(risk_check):
        return asdict(risk_check)
    # Fallback: attribute scrape for duck-typed objects.
    return {
        "approved": getattr(risk_check, "approved", None),
        "reason": getattr(risk_check, "reason", None),
        "lot_size": getattr(risk_check, "lot_size", None),
        "risk_amount_usd": getattr(risk_check, "risk_amount_usd", None),
        "risk_pct": getattr(risk_check, "risk_pct", None),
    }


def _reflection_to_dict(reflection: Any) -> dict:
    """ReflectorResult-like → JSON-safe dict (adjustments serialized)."""
    adjustments_raw = getattr(reflection, "adjustments", []) or []
    return {
        "block_reason": getattr(reflection, "block_reason", None),
        "confidence": getattr(reflection, "confidence", None),
        "notes": getattr(reflection, "notes", ""),
        "adjustments": [
            {
                "id": getattr(adj, "id", None),
                "agent_id": getattr(adj, "agent_id", None),
                "param_name": getattr(adj, "param_name", None),
                "param_path": getattr(adj, "param_path", None),
                "old_value": getattr(adj, "old_value", None),
                "new_value": getattr(adj, "new_value", None),
                "reason": getattr(adj, "reason", None),
                "evidence": getattr(adj, "evidence", None),
            }
            for adj in adjustments_raw
        ],
    }
