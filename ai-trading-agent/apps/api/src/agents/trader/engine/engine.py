"""
BacktestEngine — bar-by-bar backtest orkestratori.

Tartib:
1. ``__init__(config, ...)`` — VirtualClock, HistoricalDataManager, PaperBroker,
   InMemoryBus (placeholder), StubReflector, StubRisk, analyst (caller-provided),
   BacktestJournal yaratiladi.
2. ``run()`` — warmup → primary_timeframe bar loop → final cleanup → _build_result()
3. ``_trading_cycle()`` — pipeline: get_bar → broker.on_bar_closed → closed trades
   sync → trading_hours filter → Analyst → Reflector → Risk → broker.place_order
   → Journal.

LINT QOIDASI: ``datetime.utcnow()`` ishlatish TAQIQLANGAN — har doim ``clock.now()``.

F2-2 NOTE: TMAS portida ReflectorAgent + RiskAgent ni to'liq port qilmadik;
o'rniga ``stubs.py`` dagi pass-through StubReflector/StubRisk ishlatiladi.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

from apps.api.src.agents.trader.core.broker import PaperBroker
from apps.api.src.agents.trader.core.clock import BacktestComplete, VirtualClock
from apps.api.src.agents.trader.core.data import HistoricalDataManager
from apps.api.src.agents.trader.engine.backtest_journal import BacktestJournal
from apps.api.src.agents.trader.engine.config import BacktestConfig
from apps.api.src.agents.trader.engine.result import BacktestResult
from apps.api.src.agents.trader.engine.signals import RiskCheckResult
from apps.api.src.agents.trader.engine.stubs import StubReflector, StubRisk


_TIMEFRAME_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}


class _NoopBus:
    """Minimal placeholder for TMAS InMemoryBus.

    The engine instantiates a bus for symmetry with the live system, but
    nothing in the MVP backtest path publishes or subscribes. We keep the
    API surface (subscribe/publish/clear) so future hooks slot in cleanly.
    """

    def __init__(self) -> None:
        self._subs: dict[str, list] = {}

    def subscribe(self, channel: str, callback) -> None:
        self._subs.setdefault(channel, []).append(callback)

    def publish(self, channel: str, message: dict) -> int:
        callbacks = self._subs.get(channel, [])
        for cb in list(callbacks):
            cb(channel, message)
        return len(callbacks)

    def clear(self) -> None:
        self._subs.clear()


class BacktestEngine:
    """
    Bar-by-bar backtest run koordinatori.

    :param config: BacktestConfig (frozen) — barcha sozlamalar
    :param analyst: Analyst instance — ``analyze_market(symbol, timeframes)`` →
                    Signal | None. F2-2.D da MAJBURIY emas (None → engine
                    qurilib ishga tushadi, lekin run() da hech qanday signal
                    bo'lmaydi). F2-2.E da ICTAdapter ulanadi.
    :param reflector: Reflector instance (default: StubReflector)
    :param risk: Risk instance (default: StubRisk)
    :param db_conn: Sync DB connection BacktestJournal uchun (None = in-memory only)
    """

    PROGRESS_EVERY_BARS = 1000  # default per-bar log frequency

    def __init__(
        self,
        config: BacktestConfig,
        analyst: Any = None,
        reflector: Any = None,
        risk: Any = None,
        db_conn: Any = None,
        progress_every_bars: int | None = None,
    ) -> None:
        self.config = config

        self.clock = VirtualClock(start=config.start, end=config.end)
        self.data = HistoricalDataManager(data_path=config.data_path)
        self.broker = PaperBroker(config=config.broker, clock=self.clock, data=self.data)
        self.bus = _NoopBus()

        self.reflector = reflector if reflector is not None else StubReflector(
            shadow_mode=config.reflector_shadow_mode,
        )
        self.risk = risk if risk is not None else StubRisk()
        self.analyst = analyst  # F2-2.E da majburiy bo'ladi
        self.journal = BacktestJournal(run_id=config.run_id, db_conn=db_conn)

        # Tracker: oldingi `broker.history` uzunligi (newly closed trade detection)
        self._last_history_len: int = 0
        # Tracker: oldingi clock tick (session boundary detection)
        self._last_tick: datetime | None = None
        # Perf: running max equity (peak) for O(1) drawdown. Was recomputed as
        # max() over the whole (copied) equity curve every bar -> O(n) per bar
        # -> O(n^2) per run. Kept in sync at both equity-record sites below.
        self._equity_peak: float | None = None
        # F2 hot-fix: progress logging — uzun runlarda jim qolmaslik uchun.
        # 0 yoki manfiy qiymat → progress log o'chiriladi.
        self._progress_every_bars: int = (
            progress_every_bars if progress_every_bars is not None
            else self.PROGRESS_EVERY_BARS
        )

    # ── Public ────────────────────────────────────────────────────────────────

    def run(self) -> BacktestResult:
        """
        Backtest'ni boshidan oxirigacha ishga tushiradi.

        :return: BacktestResult — meta + total_trades to'ldirilgan skeleton.
                 Qolgan metrikalar keyingi PerformanceAnalyzer to'ldiradi.
        :raises RuntimeError: agar ``analyst`` berilmagan bo'lsa.
        """
        if self.analyst is None:
            raise RuntimeError(
                "BacktestEngine.run() requires an analyst. Pass `analyst=...` "
                "to the constructor (F2-2.E ICTAdapter is the canonical choice)."
            )

        started_at = datetime.now(tz=timezone.utc)

        self.journal.log_run_started(
            config_dict=self._config_to_dict(),
        )

        # Warmup: bu skeleton'da faqat data preload — indikator state hozircha yo'q
        self._warmup()

        step = self._primary_step()
        symbol = self.config.symbol

        # F2 hot-fix: progress tracking — uzun runlarda jim qolmaslik uchun
        bars_processed = 0
        total_bars_est = self._estimate_total_bars(step)
        if self._progress_every_bars > 0:
            logger.info(
                f"[progress] est. total bars = {total_bars_est}, "
                f"log every {self._progress_every_bars}"
            )

        try:
            while not self.clock.is_finished():
                self._tick(symbol)
                self.clock.advance(step)
                bars_processed += 1
                if (
                    self._progress_every_bars > 0
                    and bars_processed % self._progress_every_bars == 0
                ):
                    self._log_progress(bars_processed, total_bars_est, started_at)
        except BacktestComplete:
            pass

        # Final cleanup: barcha ochiq pozitsiyalarni yopish
        closed_at_end = self.broker.close_all_positions(reason="end_of_test")
        for closed in closed_at_end:
            self.journal.log_trade_closed(closed)
            self.risk.record_trade_outcome(closed.pnl, closed.exit_time)

        ended_at = datetime.now(tz=timezone.utc)
        result = self._build_result(started_at, ended_at)

        self.journal.log_run_ended(result_dict=result.to_dict())

        return result

    # ── Progress logging ──────────────────────────────────────────────────────

    def _estimate_total_bars(self, step: timedelta) -> int:
        """Estimate total bars between config.start and config.end (best-effort).

        Bu son aniq emas (calendar gaps, weekend candles HistoricalDataManager
        tomonidan filterlanadi), lekin progress ETA va foiz hisobi uchun
        yetarli aniqlik beradi.
        """
        try:
            total_sec = (self.config.end - self.config.start).total_seconds()
            step_sec = step.total_seconds() or 1.0
            return max(1, int(total_sec / step_sec))
        except Exception:  # noqa: BLE001
            return 1

    def _log_progress(
        self,
        bars_done: int,
        total_bars: int,
        started_at: datetime,
    ) -> None:
        """One-line progress log. Tezligi, ETA va trade count'ni ko'rsatadi."""
        elapsed = (datetime.now(tz=timezone.utc) - started_at).total_seconds()
        pct = (bars_done / total_bars) * 100.0 if total_bars else 0.0
        bps = bars_done / elapsed if elapsed > 0 else 0.0
        eta_sec = (total_bars - bars_done) / bps if bps > 0 else 0.0
        try:
            market_ts = self.clock.now().strftime("%Y-%m-%d %H:%M")
        except Exception:  # noqa: BLE001
            market_ts = "?"
        trades_closed = len(getattr(self.broker, "history", []) or [])
        logger.info(
            f"[progress] {market_ts} | "
            f"bars {bars_done}/{total_bars} ({pct:.1f}%) | "
            f"trades {trades_closed} | "
            f"elapsed {elapsed:.0f}s | "
            f"bps {bps:.1f} | "
            f"ETA {eta_sec:.0f}s"
        )

    # ── Bar tick ──────────────────────────────────────────────────────────────

    def _tick(self, symbol: str) -> None:
        """
        Bitta primary_timeframe bar uchun to'liq cycle.

        Pipeline:
        1. Joriy primary_tf bar'ni olish va broker.on_bar_closed chaqirish
        2. Yangi yopilgan trade'larni journal'ga yozish + risk state yangilash
        3. Session boundary → overnight swap + daily snapshot
        4. Trading hours filter
        5. Analyst → Reflector → Risk → Broker pipeline
        6. Equity point yozish
        """
        now = self.clock.now()

        bar = self._current_bar(symbol)
        if bar is not None:
            self.broker.on_bar_closed(symbol, bar)

        self._flush_closed_trades()

        if self._is_new_session(self._last_tick, now):
            self.broker.apply_overnight_swap()
            self.journal.daily_snapshot(self.broker, now)
            # daily_snapshot appended an equity point — keep the running peak in
            # sync so _record_equity below stays equal to max(equity_curve).
            eq = self.broker.equity
            self._equity_peak = eq if self._equity_peak is None else max(self._equity_peak, eq)
        self._last_tick = now

        if not self._is_trading_allowed(now):
            self._record_equity(now)
            return

        self._trading_cycle(symbol)
        self._record_equity(now)

    def _trading_cycle(self, symbol: str) -> None:
        """Analyst → Reflector → Risk → Broker → Journal pipeline (bitta cycle).

        F2-3.1 Variant C: ``analyze_market`` can return ``Signal | list[Signal] | None``.
        ``None`` and empty list both mean "no signals this cycle". A single Signal is
        wrapped to a 1-element list for uniform processing. Each signal is routed
        independently — one may pass risk while another fails, and broker-level
        rejection on one does not block the rest.
        """
        result = self.analyst.analyze_market(symbol, self.config.timeframes)
        if result is None:
            return
        signals: list = result if isinstance(result, list) else [result]
        if not signals:
            return

        for signal in signals:
            self._route_one_signal(signal)

    def _route_one_signal(self, signal) -> None:
        """Route a single Signal through reflector → risk → broker → journal."""
        reflection = self.reflector.evaluate_signal_sync(signal, market_context={})
        if reflection.block_reason is not None:
            self.journal.log_blocked_signal(signal, reflection)
            return

        risk_check = self.risk.approve_trade(signal, self.broker)
        if not risk_check.approved:
            self.journal.log_rejected_signal(signal, risk_check)
            return

        if signal.is_limit:
            order_result = self.broker.place_order(
                symbol=signal.symbol,
                direction=signal.direction,
                order_type="limit",
                lot=risk_check.lot_size,
                entry=signal.entry_price,
                sl=signal.sl,
                tp=signal.tp,
            )
        else:
            order_result = self.broker.place_order(
                symbol=signal.symbol,
                direction=signal.direction,
                order_type="market",
                lot=risk_check.lot_size,
                sl=signal.sl,
                tp=signal.tp,
            )

        if order_result.success:
            self.journal.log_trade_opened(signal, order_result, risk_check, reflection)
        else:
            broker_reject = RiskCheckResult(
                approved=False,
                reason=f"BROKER_REJECTED:{order_result.error}",
            )
            self.journal.log_rejected_signal(signal, broker_reject)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _warmup(self) -> None:
        """
        Warmup data preload — config.start dan 30 kun oldingi candlelar.

        Skeleton: faqat HistoricalDataManager cache'ini iliydi. Indikator
        state hisoblash (EMA, ATR) ICT engine bilan birga keladi.
        """
        warmup_start = self.config.start - timedelta(days=30)
        for tf in self.config.timeframes:
            try:
                self.data.load(self.config.symbol, tf, warmup_start, self.config.end)
            except (FileNotFoundError, KeyError):
                # Test/skeleton uchun ma'lumot yo'q bo'lsa silent skip
                continue

    def _flush_closed_trades(self) -> None:
        """`broker.history`'da yangi paydo bo'lgan trade'larni journal'ga ko'chiradi."""
        history = self.broker.history
        new_trades = history[self._last_history_len:]
        for closed in new_trades:
            self.journal.log_trade_closed(closed)
            self.risk.record_trade_outcome(closed.pnl, closed.exit_time)
        self._last_history_len = len(history)

    def _record_equity(self, timestamp: datetime) -> None:
        """Equity point yozish + drawdown hisoblash.

        Peak = running max of all equity recorded so far (== max over the equity
        curve, maintained incrementally here and at the daily_snapshot site).
        """
        eq = self.broker.equity
        peak = eq if self._equity_peak is None else max(self._equity_peak, eq)
        self._equity_peak = peak
        dd_pct = (peak - eq) / peak * 100.0 if peak > 0 else 0.0
        self.journal.record_equity_point(
            timestamp, eq, self.broker.balance, dd_pct,
        )

    def _current_bar(self, symbol: str):
        """Joriy primary_timeframe bar'ni qaytaradi (broker.on_bar_closed uchun)."""
        try:
            candles = self.data.get_candles_at(
                symbol, self.config.primary_timeframe, self.clock.now(), count=1,
            )
            if candles.empty:
                return None
            return candles.iloc[-1]
        except (KeyError, Exception):
            return None

    def _primary_step(self) -> timedelta:
        """Primary timeframe step (M15 → 900s)."""
        secs = _TIMEFRAME_SECONDS.get(self.config.primary_timeframe)
        if secs is None:
            raise ValueError(
                f"Unsupported primary_timeframe {self.config.primary_timeframe!r}. "
                f"Supported: {sorted(_TIMEFRAME_SECONDS)!r}"
            )
        return timedelta(seconds=secs)

    def _is_new_session(self, prev: datetime | None, now: datetime) -> bool:
        """Yangi savdo kuni boshlanganini tekshiradi (UTC kun o'zgarishi)."""
        if prev is None:
            return False
        return prev.date() != now.date()

    def _is_trading_allowed(self, now: datetime) -> bool:
        """
        Trading vaqti filtri.

        Bloklangan oraliqlar:
        - Friday 22:00 UTC → Monday 00:00 UTC (weekend gap)
        """
        weekday = now.weekday()  # Mon=0, Sun=6
        hour = now.hour

        # Friday 22:00 → Sunday end
        if weekday == 4 and hour >= 22:
            return False
        if weekday == 5:  # Saturday
            return False
        if weekday == 6:  # Sunday
            return False

        return True

    def _build_result(self, started_at: datetime, ended_at: datetime) -> BacktestResult:
        """
        Skeleton BacktestResult yaratadi (meta + total_trades).

        Keyingi PerformanceAnalyzer qolgan 26 maydonni to'ldiradi
        (returns, risk-adjusted, drawdown, distribution, statistical CI).
        """
        return BacktestResult(
            run_id=self.config.run_id,
            started_at=started_at,
            ended_at=ended_at,
            verdict="PENDING",
            total_trades=len(self.broker.history),
        )

    def _config_to_dict(self) -> dict:
        """BacktestConfig → JSON-safe dict (DB persistence uchun)."""
        return {
            "run_id": self.config.run_id,
            "start": self.config.start.isoformat(),
            "end": self.config.end.isoformat(),
            "symbol": self.config.symbol,
            "primary_timeframe": self.config.primary_timeframe,
            "timeframes": list(self.config.timeframes),
            "data_path": str(self.config.data_path),
            "min_confluence": self.config.min_confluence,
            "reflector_shadow_mode": self.config.reflector_shadow_mode,
            "seed": self.config.seed,
            "risk_config": {
                "risk_per_trade_pct": self.config.risk_config.risk_per_trade_pct,
                "max_open_positions": self.config.risk_config.max_open_positions,
                "max_daily_loss_pct": self.config.risk_config.max_daily_loss_pct,
                "min_rr": self.config.risk_config.min_rr,
                "losing_streak_threshold": self.config.risk_config.losing_streak_threshold,
                "streak_risk_multiplier": self.config.risk_config.streak_risk_multiplier,
            },
            "broker": {
                "initial_balance": self.config.broker.initial_balance,
                "commission_per_lot": self.config.broker.commission_per_lot,
                "slippage_model": self.config.broker.slippage_model,
                "leverage": self.config.broker.leverage,
            },
        }
