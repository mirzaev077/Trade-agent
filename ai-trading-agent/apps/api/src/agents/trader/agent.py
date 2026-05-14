import asyncio
from loguru import logger

from .core.clock import get_clock
from .mt5_connector import MT5Connector
from .analysis import ICTAnalysis
from .brain.ai_validator import TradingAIBrain
from .brain.self_learner import SelfLearner
from .risk.manager import RiskManagement
from .models.config import TradingConfig
from .models.orders import TradeOrder
from .models.signals import TradeSignal
from .utils.session_times import (
    get_current_session,
)
from .utils.news_filter import has_high_impact_news, get_next_news_str
from .utils import telegram_bot as tg
from .utils.trade_analytics import (
    log_trade_csv, get_profit_factor, get_session_stats, build_weekly_report,
)
from .specialist import (
    HTFBiasAgent,
    LiquidityHunterAgent,
    ManipulationAgent,
    EntryAgent,
    SniperAgent,
    FlowAgent,
    ConfluenceAgent,
    SessionGuard,
)
# F0-1: Restart safety — trade meta persistence
from .state.persistence import (
    save_trade_meta, load_trade_meta, recover_from_mt5,
)
# F0-2/F0-3: MT5 disconnect/recovery Telegram alerts (sync versions)
from .utils.telegram_bot import notify_disconnect, notify_recovered


class TraderAgent:
    # ── XAUUSD constants ──────────────────────────────────────────
    PIP         = 0.10

    # SNIPER MODE: D1 + H4 aligned — trend continuation
    SL_SNIPER_MIN = 40   # pips
    SL_SNIPER_MAX = 50
    TP_SNIPER_1   = 40   # pips (min TP)
    TP_SNIPER_2   = 100  # pips
    TP_SNIPER_3   = 150  # pips (impuls bo'lsa 250 ga o'zgaradi)
    RISK_SNIPER   = 0.03 # 3%

    # FLOW MODE: range / manipulation zone
    SL_FLOW_MIN   = 40   # pips
    SL_FLOW_MAX   = 50
    TP_FLOW_1     = 40   # pips (min TP)
    TP_FLOW_2     = 100  # pips
    TP_FLOW_3     = 150  # pips (impuls bo'lsa 250 ga o'zgaradi)
    RISK_FLOW     = 0.03  # 3%

    # Derived (set dynamically per tick based on mode)
    SL_MIN_PIPS = 40
    SL_MAX_PIPS = 50
    TP_MIN_PIPS = 40
    TP_MAX_PIPS = 250
    RISK_PCT    = 0.01

    TF_WEIGHTS  = {"D1": 10, "H4": 8, "H1": 6, "M30": 4, "M15": 3, "M5": 2}

    def __init__(self, config: TradingConfig = None, health_state=None):
        self.config  = config or TradingConfig()
        self.running = False
        self.symbol  = self.config.symbol

        # F1-4: Healthcheck shared state — main.py'da yaratiladi va beriladi.
        # None bo'lsa health endpoint o'chirilgan (back-compat).
        self.health_state = health_state
        if self.health_state is not None:
            self.health_state.scan_interval_sec = int(self.config.scan_interval)

        self.mt5     = MT5Connector()
        self.ict     = ICTAnalysis()
        self.brain   = TradingAIBrain(
            api_key=self.config.claude_api_key,
            min_confidence=self.config.min_ai_confidence,
        )
        self.risk    = RiskManagement(self.config.get_risk_config())
        self.learner = SelfLearner()

        # ── Specialist agents (pipeline) ──────────────────────────
        self.htf_agent    = HTFBiasAgent()
        self.liq_hunter   = LiquidityHunterAgent()
        self.manip_agent  = ManipulationAgent()
        self.entry_agent  = EntryAgent()
        self.sniper_agent     = SniperAgent()
        self.flow_agent       = FlowAgent()
        self.confluence_agent = ConfluenceAgent()
        self.session_guard    = SessionGuard()

        # F0-1: Restart safety — diskdan tiklash (MT5 connect() dan oldin)
        # connect() ichida disk bo'sh bo'lsa MT5'dan recover qilamiz.
        _loaded = load_trade_meta()
        if _loaded:
            self._trade_meta: dict = _loaded
            logger.info(f"_trade_meta: restored {len(_loaded)} positions from disk")
        else:
            self._trade_meta: dict = {}
        self._mt5_recovery_attempted: bool = False  # connect() ichida ishlatamiz

        # F0-2/F0-3: disconnect/recovery Telegram dedup flag'lari
        self._disconnect_notified: bool = False
        self._recovery_notified: bool = False

        # F0-4: news blackout paytida pending cancel dedup flag
        # (har tick'da qayta-qayta cancel chaqirilmasligi uchun)
        self._news_cancel_done: bool = False

        self._pending_zones: dict = {}
        self._used_zones:    set  = set()
        self._wins:          int  = 0
        self._losses:        int  = 0
        self._streak:        int  = 0
        self._pause_until:   object = None   # datetime yoki None
        self._mode:          str  = "FLOW"   # "SNIPER" | "FLOW"
        self._current_regime: str = "range"  # trend | range | chop

        # Order cooldown: har yo'nalishda oxirgi order vaqti (overtrade oldini olish)
        self._last_order_at: dict = {"buy": None, "sell": None}
        self._last_session:  str  = ""   # sessiya o'zgarganda cooldown reset

        # Session open levels (Midnight / London / NY) — kun boshida reset
        self._midnight_open:   float = 0.0
        self._london_open:     float = 0.0
        self._ny_open:         float = 0.0
        self._session_open_date: object = None  # date sentinel

        # Drawdown circuit breaker — boshlang'ich balans
        self._starting_balance: float = 0.0
        self._dd_breaker_hit:   bool  = False
        self.DD_MAX_PCT:        float = 10.0  # 10% umumiy drawdown chegarasi
        self._current_spread_p: float = 0.0
        self._daily_target_notified: bool = False
        self._daily_reset_date: object = None

        # DXY korrelyatsiya
        self._dxy_symbol: str   = ""   # topilgan DXY symbol nomi
        self._dxy_trend:  str   = "sideways"

        # Profit factor monitoring
        self._pf_warn_sent: bool = False

        # Haftalik hisobot (Yakshanba)
        self._last_weekly_report_week: int = -1

        # BE re-entry tracking
        self._be_reentry_candidates: dict = {}  # ticket → meta

        # Monday/Friday reduced mode
        self.REDUCED_DAYS = {0, 4}  # Monday=0, Friday=4

        # Session stats log (har 4 soatda)
        self._last_session_stats_hour: int = -1

    # ── Connect ───────────────────────────────────────────────────

    def connect(self, login: int, password: str, server: str) -> dict:
        info = self.mt5.connect(login=login, password=password, server=server)
        real = self.mt5.find_symbol(self.symbol)
        if real != self.symbol:
            self.symbol = real
            self.config.symbol = real
        self._starting_balance = float(info.get("balance", 0))
        tg.init(self.config.telegram_bot_token, self.config.telegram_chat_id)
        # DXY symbol qidirish
        for dxy_name in ("USDX", "DXY", "USDIx", "USDXDXY"):
            try:
                found = self.mt5.find_symbol(dxy_name)
                if found:
                    self._dxy_symbol = found
                    logger.info(f"DXY symbol topildi: {found}")
                    break
            except Exception:
                pass
        if not self._dxy_symbol:
            logger.debug("DXY symbol topilmadi — korrelyatsiya o'chirilgan")

        # F0-1: agar disk bo'sh bo'lsa (init'dan) — MT5'dan asoslab tiklash
        if not self._trade_meta and not self._mt5_recovery_attempted:
            self._mt5_recovery_attempted = True
            try:
                recovered = recover_from_mt5(self.mt5)
                if recovered:
                    self._trade_meta = recovered
                    logger.warning(
                        f"_trade_meta: disk empty, recovered {len(recovered)} from MT5"
                    )
                    save_trade_meta(self._trade_meta)  # darhol diskka yoz
            except Exception as _re:
                logger.error(f"recover_from_mt5 failed: {_re}")

        logger.success(f"MT5 connected: #{info['login']}  Balance=${info['balance']:.2f}")
        return info

    # ── Main Loop ─────────────────────────────────────────────────

    async def run(self):
        self.running = True
        logger.info(f"OpenClaw ICT started | {self.symbol} | D1→M1 top-down")
        while self.running:
            try:
                await self._tick()
            except Exception as e:
                logger.error(f"Tick error: {e}")
            # F1-4: Healthcheck snapshot — har tick'dan keyin yangilash
            self._update_health_state()
            await asyncio.sleep(self.config.scan_interval)

    def _update_health_state(self) -> None:
        """F1-4: HealthState dataclass'ni so'nggi qiymatlar bilan yangilash."""
        if self.health_state is None:
            return
        try:
            from datetime import datetime, timezone
            self.health_state.last_tick_at  = datetime.now(timezone.utc)
            self.health_state.mt5_connected = bool(self.mt5.is_connected())
            try:
                self.health_state.open_positions = len(self.mt5.get_open_positions())
            except Exception:  # noqa: BLE001
                pass
            # Daily PnL — risk._today_trades'dan
            try:
                today = getattr(self.risk, "_today_trades", []) or []
                pos_pnl = sum(t.get("pnl", 0) for t in today if t.get("pnl", 0) > 0)
                neg_pnl = sum(t.get("pnl", 0) for t in today if t.get("pnl", 0) < 0)
                bal = self._starting_balance or 0.0
                self.health_state.daily_pnl_pct = (
                    (pos_pnl + neg_pnl) / bal * 100 if bal > 0 else 0.0
                )
            except Exception:  # noqa: BLE001
                pass
            # Pause holatlari (DD breaker, consecutive loss, news cancel)
            self.health_state.is_paused = bool(
                getattr(self, "_dd_breaker_hit", False)
                or getattr(self, "_pause_until", None)
                or getattr(self, "_news_cancel_done", False)
            )
        except Exception as e:  # noqa: BLE001 — health update hech qachon tick'ni buzmaydi
            logger.debug(f"[healthcheck] update error: {e}")

    # ── Tick ──────────────────────────────────────────────────────

    async def _tick(self):
        # ── F0-2: MT5 disconnect guard — ghost trade'ni oldini olish ──
        if not self.mt5.ensure_connected(max_retries=5, initial_backoff=2.0):
            downtime_min = self.mt5.get_disconnect_duration_min() or 0
            logger.error(f"MT5 disconnect — tick skip (downtime {downtime_min} min)")
            # 5+ daqiqa bo'lsa Telegram alert (qayta-qayta yubormaslik uchun flag)
            if downtime_min >= 5 and not self._disconnect_notified:
                try:
                    notify_disconnect(downtime_min)
                    self._disconnect_notified = True
                except Exception as e:
                    logger.error(f"notify_disconnect failed: {e}")
            return  # YANGI ORDER YO'Q

        # ── F0-2/F0-3: recovered alert (faqat avval uzilgan bo'lsa) ──
        recovered_min = getattr(self.mt5, "_last_recovery_downtime_min", 0)
        if recovered_min > 0 and not self._recovery_notified:
            try:
                notify_recovered(recovered_min)
            except Exception as e:
                logger.error(f"notify_recovered failed: {e}")
            self._recovery_notified = True
            self.mt5._last_recovery_downtime_min = 0
            self._disconnect_notified = False  # keyingi disconnect uchun reset
        elif recovered_min == 0 and self._recovery_notified:
            # MT5 ulangan va recovery alert allaqachon yuborilgan — flag reset
            self._recovery_notified = False

        # ── Weekend guard ─────────────────────────────────────────
        now_utc = get_clock().now()
        # Friday 22:00 UTC → Monday 00:00 UTC — market closed
        if now_utc.weekday() == 4 and now_utc.hour >= 22:
            logger.debug("Weekend: Friday 22:00+ — skip")
            return
        if now_utc.weekday() in (5, 6):
            logger.debug(f"Weekend ({['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][now_utc.weekday()]}) — skip")
            return

        account = self.mt5.refresh_account()
        if not account:
            return

        # ── News filter ───────────────────────────────────────────
        try:
            if await has_high_impact_news(window_min=30):
                news_str = get_next_news_str()
                logger.warning(f"📰 NEWS BLACKOUT — trade yo'q | {news_str}")
                await tg.send(f"📰 <b>NEWS BLACKOUT</b>\n{news_str}")

                # F0-4: News paytida pending limit/stop orderlarni bekor qil
                # (keng spread'da execute bo'lib ketmasligi uchun).
                # Bir marta cancel — har 15s'da takrorlanmasligi uchun flag.
                if not self._news_cancel_done:
                    try:
                        cancelled = self._cancel_all_pending_orders(reason="NEWS_BLACKOUT")
                        if cancelled > 0:
                            logger.warning(
                                f"📰 NEWS BLACKOUT: {cancelled} pending order(s) cancelled"
                            )
                            try:
                                await tg.send(
                                    f"📰 <b>NEWS BLACKOUT</b>\n"
                                    f"{cancelled} ta pending order bekor qilindi"
                                )
                            except Exception:
                                pass
                    except Exception as _e:
                        logger.error(f"News blackout cancel failed: {_e}")
                    finally:
                        self._news_cancel_done = True

                await self._manage_positions()
                return
            else:
                # News window tugagan — flag reset (keyingi news uchun)
                if self._news_cancel_done:
                    self._news_cancel_done = False
        except Exception:
            pass  # xato bo'lsa davom et

        # ── Drawdown circuit breaker ──────────────────────────────
        balance_now = account.get("balance", 0)
        if self._starting_balance > 0 and balance_now > 0:
            dd_pct = (self._starting_balance - balance_now) / self._starting_balance * 100
            if dd_pct >= self.DD_MAX_PCT:
                if not self._dd_breaker_hit:
                    self._dd_breaker_hit = True
                    logger.critical(
                        f"🚨 DRAWDOWN CIRCUIT BREAKER: -{dd_pct:.1f}% "
                        f"(${self._starting_balance:.0f}→${balance_now:.0f}) — TRADING TO'XTATILDI"
                    )
                    await tg.notify_dd_breaker(dd_pct, balance_now)
                await self._manage_positions()
                return

        # ── Monday/Friday qoidasi: kamroq trade ──────────────────
        _today_wd = get_clock().now().weekday()
        _reduced_day = _today_wd in self.REDUCED_DAYS

        # ── Haftalik hisobot (Yakshanba) ──────────────────────────
        _cur_week = get_clock().now().isocalendar()[1]
        if (get_clock().now().weekday() == 6 and
                _cur_week != self._last_weekly_report_week):
            self._last_weekly_report_week = _cur_week
            try:
                report = build_weekly_report(
                    self.learner.data.get("trades", []),
                    account.get("balance", 0),
                )
                await tg.send(report)
                logger.info("📊 Haftalik hisobot yuborildi")
            except Exception as _we:
                logger.debug(f"Weekly report error: {_we}")

        # ── Candles: W1 → M1 ─────────────────────────────────────
        w1  = self._candles("W1",   60)
        d1  = self._candles("D1",  150)
        h4  = self._candles("H4",  300)
        h1  = self._candles("H1",  500)
        m30 = self._candles("M30", 300)
        m15 = self._candles("M15", 500)
        m5  = self._candles("M5",  300)
        m1  = self._candles("M1",  500)

        if h4 is None or h1 is None or m5 is None:
            logger.warning("Insufficient data")
            return

        # ── ICT analysis: W1 → M1 ────────────────────────────────
        ict_w1  = self.ict.analyze(w1,  "W1")  if w1  is not None else None
        ict_d1  = self.ict.analyze(d1,  "D1")  if d1  is not None else None
        ict_h4  = self.ict.analyze(h4,  "H4")
        ict_h1  = self.ict.analyze(h1,  "H1")
        ict_m30 = self.ict.analyze(m30, "M30") if m30 is not None else None
        ict_m15 = self.ict.analyze(m15, "M15") if m15 is not None else None
        ict_m5  = self.ict.analyze(m5,  "M5")
        ict_m1  = self.ict.analyze(m1,  "M1")  if m1  is not None else None

        # ── DXY korrelyatsiya ─────────────────────────────────────
        if self._dxy_symbol:
            try:
                dxy_h4 = self.mt5.get_candles(self._dxy_symbol, "H4", 100)
                if dxy_h4 is not None and len(dxy_h4) > 20:
                    dxy_ict = self.ict.analyze(dxy_h4, "H4")
                    self._dxy_trend = dxy_ict.structure.get("trend", "sideways")
            except Exception:
                pass

        # ── Agent 1: HTF Bias (W1 → D1 → H4 → H1) ───────────────
        bias_result = self.htf_agent.analyze(ict_d1, ict_h4, ict_h1, ict_w1)
        d1_bias     = bias_result.d1_trend
        h4_trend    = bias_result.h4_trend
        h1_trend    = bias_result.h1_trend
        self._mode  = bias_result.mode

        want_dir = (
            "buy"  if bias_result.bias == "buy"  else
            "sell" if bias_result.bias == "sell" else
            "none"
        )

        # ── Current price ─────────────────────────────────────────
        try:
            tick      = self.mt5.get_current_price(self.symbol)
            mid_price = (tick["ask"] + tick["bid"]) / 2
            spread_p  = (tick["ask"] - tick["bid"]) / self.PIP
            # XAUUSD normal spread 2-5 pip; 15+ pip = news spike / market close
            if spread_p > 15.0:
                logger.warning(f"📡 Spread {spread_p:.1f}p — keng (news/close), skip")
                await self._manage_positions()
                return
            self._current_spread_p = spread_p
        except Exception:
            return

        # ── Session open levels (Midnight / London 08:00 / NY 13:30) ─
        now = get_clock().now()
        today = now.date()
        if self._session_open_date != today:
            self._midnight_open = self._london_open = self._ny_open = 0.0
            self._session_open_date = today
            self._daily_target_notified = False  # yangi kun — reset
        if now.hour == 0  and self._midnight_open == 0.0:
            self._midnight_open = mid_price
            logger.info(f"Midnight Open: {mid_price:.2f}")
        if now.hour == 8  and now.minute < 10 and self._london_open == 0.0:
            self._london_open = mid_price
            logger.info(f"London Open: {mid_price:.2f}")
        if now.hour == 13 and now.minute < 40 and self._ny_open == 0.0:
            self._ny_open = mid_price
            logger.info(f"NY Open: {mid_price:.2f}")

        # ── Status log ───────────────────────────────────────────
        session  = get_current_session()

        # Sessiya o'zgarganda cooldown + used_zones reset → yangi sessiya, yangi imkoniyat
        if session != self._last_session:
            self._last_order_at = {"buy": None, "sell": None}
            self._used_zones.clear()
            self._last_session  = session

        in_kz    = self._in_kill_zone()
        in_sb    = self._in_silver_bullet()
        kz_str   = " [SB]" if in_sb else (" [KZ]" if in_kz else "")
        mmxm_h4  = ict_h4.structure.get("mmxm", "?")
        mmxm_dir = ict_h4.structure.get("mmxm_dir", "?")
        m1_str   = " M1✓" if ict_m1 is not None else ""
        w1_str   = ict_w1.structure.get("trend", "?") if ict_w1 else "?"
        pending_cnt = len(self._pending_zones)

        total_closed = self._wins + self._losses
        wr_str   = f"{self._wins/(total_closed)*100:.0f}%" if total_closed > 0 else "N/A"
        streak_s = (f"+{self._streak}" if self._streak > 0 else str(self._streak))
        lot_mode = " [LOT-50%]" if self._streak <= -3 else ""
        rd_str   = " [MON/FRI]" if _reduced_day else ""
        pf_val   = get_profit_factor(self.learner.data.get("trades", []))
        dxy_str  = f" DXY={self._dxy_trend[:4]}" if self._dxy_symbol else ""
        logger.info(
            f"[{get_clock().now().strftime('%H:%M')}] {self.symbol} [{self._mode}] "
            f"W1={w1_str} D1={d1_bias} H4={h4_trend} H1={h1_trend}{dxy_str} | "
            f"MMXM={mmxm_h4}({mmxm_dir}) | want={want_dir} | "
            f"{session}{kz_str}{m1_str}{rd_str} | pending={pending_cnt} | "
            f"WR={wr_str}({self._wins}W/{self._losses}L) PF={pf_val} streak={streak_s}{lot_mode}"
        )

        # ── Profit Factor monitoring ───────────────────────────────
        if total_closed >= 10 and pf_val < 1.2 and not self._pf_warn_sent:
            self._pf_warn_sent = True
            msg = f"⚠️ <b>Profit Factor past!</b>\nPF={pf_val} ({total_closed} trade)\nStrategiya tahlil qiling."
            await tg.send(msg)
            logger.warning(f"PF={pf_val} < 1.2 — Telegram ogohlantirish yuborildi")
        elif pf_val >= 1.5:
            self._pf_warn_sent = False  # reset when PF recovers

        # ── Session win rate — har 4 soatda Telegram ──────────────
        _cur_hour = get_clock().now().hour
        if _cur_hour % 4 == 0 and _cur_hour != self._last_session_stats_hour:
            self._last_session_stats_hour = _cur_hour
            try:
                _all_trades = self.learner.data.get("trades", [])
                if len(_all_trades) >= 5:
                    s_stats = get_session_stats(_all_trades)
                    lines = ["📊 <b>Sessiya statistikasi</b>"]
                    for s_name, sc in sorted(s_stats.items(), key=lambda x: -x[1]["pnl"]):
                        lines.append(
                            f"{s_name}: {sc['wins']}W/{sc['losses']}L "
                            f"WR={sc['wr']}% PnL={sc['pnl']:+.1f}$"
                        )
                    await tg.send("\n".join(lines))
                    logger.info("📊 Session stats yuborildi")
            except Exception as _ss:
                logger.debug(f"Session stats error: {_ss}")

        # ── 5 consecutive losses → 1 soat pauza ──────────────────
        if self._pause_until is not None:
            if get_clock().now() < self._pause_until:
                rem = int((self._pause_until - get_clock().now()).total_seconds() / 60)
                logger.warning(f"⏸ PAUSED {rem}m (5 ketma-ket yo'qotish)")
                await self._manage_positions()
                return
            else:
                self._pause_until = None
                logger.info("▶ Pauza tugadi — trading qayta boshlandi")

        # ── Risk limits ───────────────────────────────────────────
        balance        = account.get("balance", 10000)
        daily_pnl_neg  = sum(t.get("pnl", 0) for t in self.risk._today_trades if t.get("pnl", 0) < 0)
        daily_pnl_pos  = sum(t.get("pnl", 0) for t in self.risk._today_trades if t.get("pnl", 0) > 0)
        daily_loss_pct = abs(daily_pnl_neg) / balance * 100 if balance > 0 else 0
        daily_gain_pct = daily_pnl_pos / balance * 100 if balance > 0 else 0

        if daily_loss_pct >= self.config.daily_max_risk:
            logger.warning(f"Daily loss {daily_loss_pct:.1f}% — manage only")
            await self._manage_pending_zones()
            await self._manage_positions()
            return

        # Daily profit target hit → stop new trades
        target_pct = self.config.daily_profit_target
        if daily_gain_pct >= target_pct:
            if not getattr(self, "_daily_target_notified", False):
                self._daily_target_notified = True
                logger.success(
                    f"🎯 DAILY TARGET +{daily_gain_pct:.1f}% (${daily_pnl_pos:.2f}) — "
                    f"bugungi trading yakunlandi!"
                )
                await tg.notify_daily_target(daily_gain_pct, daily_pnl_pos)
            await self._manage_pending_zones()
            await self._manage_positions()
            return

        daily_cap = self.risk.cfg.max_trades_per_day
        if self.risk.daily_trades_used >= daily_cap:
            logger.info(f"Daily cap {self.risk.daily_trades_used}/{daily_cap} — manage only")
            await self._manage_pending_zones()
            await self._manage_positions()
            return

        # ── Manage existing pending orders ────────────────────────
        await self._manage_pending_zones()

        # ── Open positions (faqat agent magic=20240101) ───────────
        _all_positions = self.mt5.get_open_positions()
        open_positions = [
            p for p in _all_positions
            if isinstance(p, dict) and p.get("magic", 0) == 20240101
        ]

        if len(open_positions) >= self.config.max_positions:
            logger.info(f"Max {self.config.max_positions} positions ({len(open_positions)}) — manage only")
            await self._manage_positions()
            return

        tf_ict_data = [
            ("D1",  ict_d1,  self._calc_atr(d1)  if d1  is not None else 1.0, d1),
            ("H4",  ict_h4,  self._calc_atr(h4),  h4),
            ("H1",  ict_h1,  self._calc_atr(h1),  h1),
            ("M30", ict_m30, self._calc_atr(m30) if m30 is not None else 0.5, m30),
            ("M15", ict_m15, self._calc_atr(m15) if m15 is not None else 0.3, m15),
            ("M5",  ict_m5,  self._calc_atr(m5),  m5),
            ("M1",  ict_m1,  self._calc_atr(m1)  if m1  is not None else 0.1, m1),
        ]

        # Quality flags (ikkala yo'nalish uchun ham)
        mmxm_ok  = ict_h4.structure.get("mmxm_swept", False) or \
                   ict_h1.structure.get("mmxm_swept", False)
        kz_ok    = in_kz
        judas_ok = ict_h1.structure.get("judas", False) or \
                   (ict_m15.structure.get("judas", False) if ict_m15 else False) or \
                   (ict_m5.structure.get("judas",  False) if ict_m5  else False)
        sh_ok    = ict_h1.structure.get("stop_hunt", False) or \
                   (ict_m15.structure.get("stop_hunt", False) if ict_m15 else False) or \
                   (ict_m1.structure.get("stop_hunt",  False) if ict_m1  else False)

        ict_map = {
            "D1": ict_d1, "H4": ict_h4, "H1": ict_h1,
            "M30": ict_m30, "M15": ict_m15, "M5": ict_m5, "M1": ict_m1,
        }

        # ── Market regime (SelfLearner) ───────────────────────────
        regime = self.learner.get_regime(ict_h4, ict_h1)
        self._current_regime = regime
        if regime == "chop":
            logger.info("🌀 CHOP market — trade yo'q")
            await self._manage_pending_zones()
            await self._manage_positions()
            return

        # ── Agent 2: Liquidity Hunter ─────────────────────────────
        hunt_result  = self.liq_hunter.hunt(ict_map, mid_price)

        # ── Agent 3: Manipulation confirmation ───────────────────────
        manip_result = self.manip_agent.confirm(ict_map)
        liq_swept    = manip_result.confirmed or hunt_result.any_swept

        # Limit orderlar uchun sweep shart emas — zona ga pre-place qilamiz
        # Sweep bo'lmasa ham davom etamiz, faqat score pastroq bo'ladi
        if not liq_swept:
            logger.debug(f"No sweep yet — {hunt_result.summary} | zones qidiriladi")

        if manip_result.confirmed:
            logger.info(
                f"🎯 MANIPULATION: {manip_result.sweep_type} [{manip_result.tf_found}] "
                f"strength={manip_result.strength:.2f} → {manip_result.direction_after}"
            )

        # ── Volatility filter ──────────────────────────────────────
        if not self._volatility_ok(h1):
            logger.debug("💤 Low volatility — limit pre-place davom etadi")
            # Low vol da ham limit qo'yish mumkin — faqat market order yo'q

        impulse_dir = self._detect_impulse(m1)
        if impulse_dir != "none":
            logger.info(f"⚡ IMPULSE {impulse_dir.upper()}")

        cur_session    = get_current_session()
        america_session = cur_session in ("newyork", "overlap")
        h1_atr = self._calc_atr(h1)

        # ── Bias Flip: CHoCH/BOS overrides slow D1 EMA ───────────────
        # When H4/H1 structure flips OPPOSITE to current want_dir:
        #   1. Override want_dir to new direction
        #   2. Cancel all pending orders in old direction immediately
        #   3. Clear used_zones so new zones can be placed
        _h4_choch = ict_h4.structure.get("choch", False)
        _h4_bos   = int(ict_h4.structure.get("bos", 0))
        _h4_disp  = ict_h4.structure.get("displacement", False)
        _h1_choch = ict_h1.structure.get("choch", False)
        _h1_bos   = int(ict_h1.structure.get("bos", 0))

        _h4_dir = ("buy"  if h4_trend == "bullish" else
                   "sell" if h4_trend == "bearish" else "none")
        _h1_dir = ("buy"  if h1_trend == "bullish" else
                   "sell" if h1_trend == "bearish" else "none")

        _flip_to  = "none"
        _flip_why = ""

        if want_dir != "none":
            # Case A: H4 CHoCH + BOS + Displacement — confirmed H4 structure flip
            if (_h4_choch and _h4_bos >= 1 and _h4_disp
                    and _h4_dir not in ("none", want_dir)):
                _flip_to  = _h4_dir
                _flip_why = f"H4_CHoCH+BOS({_h4_bos})+Disp"

            # Case B: H1 CHoCH + BOS + impulse all agree on new direction
            elif (_h1_choch and _h1_bos >= 1
                  and impulse_dir not in ("none", want_dir)
                  and _h1_dir == impulse_dir):
                _flip_to  = impulse_dir
                _flip_why = f"H1_CHoCH+BOS({_h1_bos})+Impulse"

            # Case C: H4 BOS≥2 + H1 BOS≥2 aligned opposite — structural momentum
            elif (_h4_bos >= 2 and _h1_bos >= 2
                  and _h4_dir not in ("none", want_dir)
                  and _h4_dir == _h1_dir):
                _flip_to  = _h4_dir
                _flip_why = f"H4_BOS({_h4_bos})+H1_BOS({_h1_bos}) aligned"

        if _flip_to not in ("none", want_dir):
            _old_dir  = want_dir
            want_dir  = _flip_to
            logger.warning(
                f"🔄 BIAS FLIP: {_old_dir.upper()}→{want_dir.upper()} "
                f"[{_flip_why}] | D1={d1_bias} H4={h4_trend} H1={h1_trend}"
            )
            # Cancel all pending orders in old direction
            _n_cancelled = 0
            for _tc in list(self._pending_zones.keys()):
                if self._pending_zones.get(_tc, {}).get("direction") == _old_dir:
                    try:
                        self.mt5.cancel_pending_order(_tc)
                        del self._pending_zones[_tc]
                        _n_cancelled += 1
                    except Exception as _ce:
                        logger.debug(f"Flip-cancel #{_tc}: {_ce}")
            if _n_cancelled:
                logger.info(f"  🗑 {_n_cancelled} {_old_dir.upper()} pending(s) cancelled after flip")
            self._used_zones.clear()
            try:
                await tg.send(
                    f"🔄 <b>Bias Flip: {_old_dir.upper()} → {want_dir.upper()}</b>\n"
                    f"📋 {_flip_why}\n"
                    f"D1={d1_bias}  H4={h4_trend}  H1={h1_trend}\n"
                    f"🗑 {_n_cancelled} pending(s) cancelled"
                )
            except Exception:
                pass

        # ── SessionGuard: dead zone (22–00 UTC) → yangi order yo'q ──
        sg_result = self.session_guard.check()
        if not sg_result.allowed:
            logger.debug(f"SessionGuard: {sg_result.reason} — yangi order yo'q")
            await self._manage_positions()
            return

        for direction in ("buy", "sell"):
            # ── 1. Faqat want_dir ga trade — hedging yo'q ─────────
            if want_dir != "none" and direction != want_dir:
                continue

            # ── 2. Kuchli manipulation qarama-qarshi bo'lsa → BLOCK ─
            # Faqat aniq qarama-qarshi (buy vs sell) va strength > 0.75 da bloklaydi
            opposite = "sell" if direction == "buy" else "buy"
            if (manip_result.confirmed
                    and manip_result.direction_after == opposite
                    and manip_result.strength > 0.75):
                logger.warning(
                    f"MANIPULATION CONFLICT: sweep→{manip_result.direction_after.upper()} "
                    f"str={manip_result.strength:.2f} — BLOCKED"
                )
                continue

            # ── 3. Qarama-qarshi ochiq pozitsiya bo'lsa — skip ────
            opposite_dir = "sell" if direction == "buy" else "buy"
            opposite_open = sum(
                1 for p in open_positions
                if isinstance(p, dict) and
                ("buy" if p.get("type", -1) == 0 else "sell") == opposite_dir
            )
            if opposite_open > 0:
                logger.debug(f"Opposite {opposite_dir.upper()} ochiq — {direction.upper()} skip")
                continue

            # ── 3b. Monday/Friday: faqat yuqori sifatli setap ────────
            if _reduced_day:
                # Mon/Fri da faqat SNIPER modeda trade, FLOW skip
                if self._mode != "SNIPER":
                    logger.debug(f"Mon/Fri FLOW skip — {direction.upper()}")
                    continue

            # ── 3c. DXY korrelyatsiya filtri ──────────────────────
            # XAUUSD DXY bilan teskari: DXY bullish → XAUUSD sell tasdiqlanadi
            # DXY bearish → XAUUSD buy tasdiqlanadi
            if self._dxy_symbol and self._dxy_trend != "sideways":
                dxy_confirms_buy  = self._dxy_trend == "bearish"   # DXY tushganda XAU ko'tariladi
                dxy_confirms_sell = self._dxy_trend == "bullish"   # DXY ko'tarilganda XAU tushadi
                if direction == "buy"  and not dxy_confirms_buy:
                    logger.debug(f"DXY={self._dxy_trend} BUY ga zid — skip")
                    continue
                if direction == "sell" and not dxy_confirms_sell:
                    logger.debug(f"DXY={self._dxy_trend} SELL ga zid — skip")
                    continue

            disp_ok  = self._displacement_ok(direction, ict_m1, ict_m5, ict_m15)
            htf_conf = self._htf_confluence(direction, ict_map)

            # ── 4. HTF confluence: kamida 1 ta tasdiq ─────────────
            if htf_conf < 1:
                logger.debug(f"{direction.upper()} HTF conf={htf_conf}/4 — kam, skip")
                continue

            # ── 4b. Kill Zone majburiy — faqat London/NY da yangi limit ──
            # Asia da ham trade qilish mumkin (asian_range breakout)
            _kz_active = in_kz or in_sb or cur_session in ("london", "overlap", "newyork", "asian")
            if not _kz_active:
                logger.debug(f"{direction.upper()} KZ emas ({cur_session}) — yangi limit yo'q")
                continue

            # ── 4c. M5 BOS confirmation — yo'nalish tasdiqlangan bo'lsin ──
            if ict_m5 is not None:
                m5_trend = ict_m5.structure.get("trend", "sideways")
                m5_bos   = ict_m5.structure.get("bos", 0)
                m5_choch = ict_m5.structure.get("choch", False)
                m5_ok = (
                    (direction == "buy"  and (m5_trend == "bullish" or m5_bos > 0)) or
                    (direction == "sell" and (m5_trend == "bearish" or m5_bos > 0)) or
                    m5_choch
                )
                if not m5_ok and self._mode == "SNIPER":
                    logger.debug(f"M5 BOS yo'q ({m5_trend}) — SNIPER {direction.upper()} skip")
                    continue

            # ── Zone qidirish ──────────────────────────────────────
            zones = self._find_all_ict_zones(
                direction, mid_price, tf_ict_data,
                want_trend=want_dir, htf_conf=htf_conf, silver_bullet=in_sb,
            )
            ob_fvg_ok = any(
                z.get("label", "").split("_")[-1] in ("OB", "FVG", "BB", "OTE", "IFVG")
                for z in zones
            )

            # SelfLearner: disabled setup'larni filtrlaymiz + weight qo'llaymiz
            zones = [
                z for z in zones
                if self.learner.get_setup_weight(z.get("label", "")) > 0
            ]
            for z in zones:
                lw = self.learner.get_setup_weight(z.get("label", ""))
                z["weight"] = round(z["weight"] * lw, 2)

            crt  = self._crt_zones(direction, h1, m15, mid_price, h1_atr)
            sol  = self._session_open_zones(direction, mid_price, h1_atr)
            nwog = self._nwog_zones(direction, d1, mid_price, h1_atr)

            # ── Agent 4: Entry zone refinement ────────────────────
            entry_result = self.entry_agent.find_zones(
                raw_zones=zones,
                entry_pct=self.learner.get_entry_pct(),
            )
            zones = entry_result.zones

            ob_fvg_ok = entry_result.entry_type in ("OB", "BB", "OTE", "FVG", "IFVG")

            # ── 4d. FVG + OB confluence tekshiruvi ────────────────
            # Eng yaxshi zona: OB/BB ichida FVG ham bo'lsin (zone narxiga yaqin ±30p)
            if zones and self._mode == "SNIPER":
                top_zone   = zones[0]
                top_entry  = top_zone.get("entry", 0)
                has_ob  = any(
                    z.get("label","").split("_")[-1] in ("OB","BB","OTE")
                    and abs(z.get("entry",0) - top_entry) < 30 * self.PIP
                    for z in zones
                )
                has_fvg = any(
                    z.get("label","").split("_")[-1] in ("FVG","IFVG","BISI","SIBI","BPR")
                    and abs(z.get("entry",0) - top_entry) < 50 * self.PIP
                    for z in zones
                )
                if not (has_ob or has_fvg):
                    logger.debug(f"FVG/OB confluence yo'q — SNIPER {direction.upper()} skip")
                    continue

            # ── Structure flags ────────────────────────────────────
            struct_ok = (
                ict_h4.structure.get("bos", 0) > 0 or
                ict_h4.structure.get("choch", False) or
                ict_h1.structure.get("bos", 0) > 0
            )
            struct_break = (
                ict_h1.structure.get("bos", 0) > 0 or
                (ict_m15.structure.get("bos", 0) > 0 if ict_m15 else False)
            )

            # ── Agent 5a/5b: Sniper or Flow scoring ───────────────
            if self._mode == "SNIPER":
                decision = self.sniper_agent.decide(
                    htf_conf=htf_conf,
                    liq_swept=liq_swept,
                    impulse_ok=(impulse_dir == direction),
                    ob_fvg_ok=ob_fvg_ok,
                    in_kz=in_kz,
                    struct_ok=struct_ok,
                    manip_strength=manip_result.strength,
                )
            else:
                decision = self.flow_agent.decide(
                    liq_swept=liq_swept,
                    micro_disp=disp_ok,
                    zone_ok=bool(zones),
                    struct_break=struct_break,
                )

            score     = decision.score
            # SNIPER max=10, min=5 (htf+ob_fvg+struct = 5pt yetarli)
            # FLOW max=4, min=2 (zone_ok+struct_break yoki liq_swept+zone_ok)
            min_score = 5 if self._mode == "SNIPER" else 2

            if not decision.should_trade:
                logger.info(
                    f"[{self._mode}] {direction.upper()} "
                    f"score={score}/{min_score} — skip | {decision.reason}"
                )
                continue

            # ── Cooldown: KZ ichida 30 daqiqa, tashqarida 60 daqiqa ──
            cooldown_min = 30 if in_kz else 60
            last_at = self._last_order_at.get(direction)
            if last_at is not None:
                elapsed = (get_clock().now() - last_at).total_seconds() / 60
                if elapsed < cooldown_min:
                    logger.debug(
                        f"[{self._mode}] {direction.upper()} cooldown "
                        f"{elapsed:.0f}/{cooldown_min}m — skip"
                    )
                    continue

            # ── Agent 6: ConfluenceAgent — final gate ─────────────
            cf = self.confluence_agent.score(
                d1_bias  = d1_bias,
                h4_trend = h4_trend,
                h1_trend = h1_trend,
                direction = direction,
                sweep_confirmed  = liq_swept,
                sweep_strength   = manip_result.strength,
                sweep_type       = manip_result.sweep_type,
                has_zone         = ob_fvg_ok,
                in_kz            = in_kz,
                in_sb            = in_sb,
                has_bos          = ict_h4.structure.get("bos", 0) > 0 or ict_h1.structure.get("bos", 0) > 0,
                has_choch        = ict_h4.structure.get("choch", False) or ict_h1.structure.get("choch", False),
                has_displacement = disp_ok,
                dxy_correlated   = (
                    (direction == "buy"  and self._dxy_trend == "bearish") or
                    (direction == "sell" and self._dxy_trend == "bullish")
                ),
                mode = self._mode,
            )
            # SessionGuard min_confluence bilan taqqoslash
            base_min = sg_result.min_confluence
            if cf.total_score < base_min or not cf.pillars_ok:
                logger.info(
                    f"[CONFLUENCE] {direction.upper()} REJECTED: "
                    f"{cf.reason} (session_min={base_min})"
                )
                continue
            logger.info(f"[CONFLUENCE] {direction.upper()} {cf.reason}")

            # Quality stamp + merge CRT/SOL/NWOG zones
            for z in zones + crt + sol + nwog:
                z["quality"] = score
            zones = sorted(zones + crt + sol + nwog, key=lambda x: -x["weight"])

            if america_session:
                am_dir = "buy" if h1_trend == "bullish" else ("sell" if h1_trend == "bearish" else None)
                if am_dir and direction != am_dir:
                    zones = [z for z in zones if z.get("tf") not in ("M1", "M5", "M15")]

            logger.info(
                f"[{self._mode}] {direction.upper()} "
                f"score={score}/{min_score} ✓ | {decision.reason} | "
                f"Zones={len(zones)} HTF={htf_conf}/4 "
                f"entry={entry_result.entry_type} manip={manip_result.sweep_type}"
            )

            if zones:
                await self._place_zone_limits(
                    zones, direction, account, mid_price,
                    ict_map=ict_map, silver_bullet=in_sb,
                    open_positions=open_positions, mode=self._mode,
                    impulse_dir=impulse_dir, h1=h1,
                    d1_bias=d1_bias, h4_trend=h4_trend, h1_trend=h1_trend,
                    w1_bias=w1_str,
                )

        # ── Manage open positions ─────────────────────────────────
        await self._manage_positions()


    # ── D1 primary zone filter ────────────────────────────────────

    def _d1_has_valid_zone(self, direction: str, price: float, ict_d1) -> bool:
        """D1 yoki H4 da biror OB/FVG bo'lsa yetarli (liberal filtr)."""
        if ict_d1 is None:
            return True
        want_bull = direction == "buy"

        # Wider price tolerance (2%) — price nearby also counts
        for ob in ict_d1.order_blocks:
            if ob.get("mitigated"):
                continue
            hi = float(ob.get("high", 0))
            lo = float(ob.get("low",  0))
            if hi <= 0 or lo <= 0:
                continue
            if want_bull and "bullish" in ob.get("type", "") and price >= lo * 0.98:
                return True
            if not want_bull and "bearish" in ob.get("type", "") and price <= hi * 1.02:
                return True

        for fvg in ict_d1.fvgs:
            if fvg.get("filled"):
                continue
            hi = float(fvg.get("high", 0))
            lo = float(fvg.get("low",  0))
            if hi <= 0 or lo <= 0:
                continue
            if want_bull and "bullish" in fvg.get("type", "") and price >= lo * 0.98:
                return True
            if not want_bull and "bearish" in fvg.get("type", "") and price <= hi * 1.02:
                return True

        # D1 da zona topilmasa ham H4 bilan ishlashga ruxsat
        return True

    # ── Find all ICT key level zones ──────────────────────────────

    def _find_all_ict_zones(
        self, want_dir: str, price: float, tf_ict_data: list,
        want_trend: str = "none", htf_conf: int = 0, silver_bullet: bool = False,
    ) -> list:
        """
        Extracts entry zones from ALL ICT key levels across all timeframes:
        OB · BB · MB · RB · FVG · IFVG · BISI · SIBI · BPR
        EQH · EQL · BSL · SSL · ERL · OTE(dynamic fib) · Liquidity voids
        Each zone: {direction, entry, sl, zone_lo, zone_hi, label, weight, tf, quality}
        """
        # Trend-aligned va Silver Bullet multiplier
        trend_bonus = 1.3 if want_trend == want_dir else 1.0
        sb_bonus    = 1.5 if silver_bullet else 1.0
        htf_bonus   = 1.0 + htf_conf * 0.15   # 0 conf=1.0, 4 conf=1.6

        zones = []

        for tf, ict, atr, candles in tf_ict_data:
            if ict is None:
                continue
            wt      = self.TF_WEIGHTS.get(tf, 1)
            buf     = atr * 0.10
            vol_mul = self._volume_boost(candles)   # 1.0 yoki 1.4 (kuchli hajm)

            # ── 1. Order Blocks ───────────────────────────────────
            for ob in ict.order_blocks:
                if ob.get("mitigated"):
                    continue
                z = self._ob_zone(ob, want_dir, price, buf, wt * vol_mul, tf, "OB")
                if z: zones.append(z)

            # ── 2. Breaker Blocks ─────────────────────────────────
            for bb in ict.breaker_blocks:
                z = self._ob_zone(bb, want_dir, price, buf, wt * vol_mul, tf, "BB")
                if z: zones.append(z)

            # ── 3. Mitigation Blocks ──────────────────────────────
            for mb in ict.structure.get("mitigation_blocks", []):
                z = self._ob_zone(mb, want_dir, price, buf, wt * 0.7, tf, "MB")
                if z: zones.append(z)

            # ── 4. Rejection Blocks ───────────────────────────────
            for rb in ict.structure.get("rejection_blocks", []):
                if ("bullish" in rb.get("type","") and want_dir == "buy") or \
                   ("bearish" in rb.get("type","") and want_dir == "sell"):
                    z = self._ob_zone(rb, want_dir, price, buf, wt * 0.6, tf, "RB")
                    if z: zones.append(z)

            # ── 5. FVG / IFVG / BISI / SIBI / BPR ───────────────
            for fvg in ict.fvgs:
                if fvg.get("filled"):
                    continue
                z = self._fvg_zone(fvg, want_dir, price, buf, wt * vol_mul, tf)
                if z: zones.append(z)

            # ── 6. OTE Zone ───────────────────────────────────────
            ote = ict.structure.get("ote_zone", {})
            if ote.get("active") and ote.get("direction") == want_dir:
                hi = float(ote.get("high", 0))
                lo = float(ote.get("low",  0))
                if hi > 0 and lo > 0:
                    z = self._ob_zone(
                        {"type": f"{'bullish' if want_dir=='buy' else 'bearish'}_ob",
                         "high": hi, "low": lo, "mitigated": False},
                        want_dir, price, buf, wt * 1.5, tf, "OTE"
                    )
                    if z: zones.append(z)

            # ── 7. Liquidity sweeps → reversal zones ──────────────
            for lz in ict.liquidity_zones:
                z = self._liq_zone(lz, want_dir, price, atr, wt, tf)
                if z: zones.append(z)

            # ── 8. PDH/PDL as key levels ──────────────────────────
            pdh = float(ict.structure.get("pdh", 0))
            pdl = float(ict.structure.get("pdl", 0))
            if want_dir == "sell" and pdh > 0 and price <= pdh * 1.002:
                sl = round(pdh + buf * 2, 2)
                sl_p = (sl - pdh) / self.PIP
                tp1, tp2, tp3 = self._rr_targets(pdh, "sell", sl_p)
                if self.SL_MIN_PIPS <= sl_p <= self.SL_MAX_PIPS:
                    zones.append({
                        "direction": "sell", "entry": round(pdh, 2), "sl": sl,
                        "zone_lo": pdh - atr * 0.5, "zone_hi": pdh + atr * 0.5,
                        "tp1": tp1, "tp2": tp2, "tp3": tp3,
                        "label": f"{tf}_PDH", "weight": wt * 2.5, "tf": tf,
                    })
            if want_dir == "buy" and pdl > 0 and price >= pdl * 0.998:
                sl = round(pdl - buf * 2, 2)
                sl_p = (pdl - sl) / self.PIP
                tp1, tp2, tp3 = self._rr_targets(pdl, "buy", sl_p)
                if self.SL_MIN_PIPS <= sl_p <= self.SL_MAX_PIPS:
                    zones.append({
                        "direction": "buy", "entry": round(pdl, 2), "sl": sl,
                        "zone_lo": pdl - atr * 0.5, "zone_hi": pdl + atr * 0.5,
                        "tp1": tp1, "tp2": tp2, "tp3": tp3,
                        "label": f"{tf}_PDL", "weight": wt * 2.5, "tf": tf,
                    })

            # ── 9. Asian Range high/low ───────────────────────────
            ar_hi = float(ict.structure.get("asian_high", 0))
            ar_lo = float(ict.structure.get("asian_low",  0))
            if want_dir == "sell" and ar_hi > 0 and price <= ar_hi * 1.001:
                sl = round(ar_hi + buf * 2, 2)
                sl_p = (sl - ar_hi) / self.PIP
                tp1, tp2, tp3 = self._rr_targets(ar_hi, "sell", sl_p)
                if self.SL_MIN_PIPS <= sl_p <= self.SL_MAX_PIPS:
                    zones.append({
                        "direction": "sell", "entry": round(ar_hi, 2), "sl": sl,
                        "zone_lo": ar_hi - atr * 0.3, "zone_hi": ar_hi + atr * 0.3,
                        "tp1": tp1, "tp2": tp2, "tp3": tp3,
                        "label": f"{tf}_AR_HI", "weight": wt * 2, "tf": tf,
                    })
            if want_dir == "buy" and ar_lo > 0 and price >= ar_lo * 0.999:
                sl = round(ar_lo - buf * 2, 2)
                sl_p = (ar_lo - sl) / self.PIP
                tp1, tp2, tp3 = self._rr_targets(ar_lo, "buy", sl_p)
                if self.SL_MIN_PIPS <= sl_p <= self.SL_MAX_PIPS:
                    zones.append({
                        "direction": "buy", "entry": round(ar_lo, 2), "sl": sl,
                        "zone_lo": ar_lo - atr * 0.3, "zone_hi": ar_lo + atr * 0.3,
                        "tp1": tp1, "tp2": tp2, "tp3": tp3,
                        "label": f"{tf}_AR_LO", "weight": wt * 2, "tf": tf,
                    })

            # ── 10. Dynamic OTE (Fibonacci 0.62–0.786) ───────────
            for z in self._ote_fib_zones(want_dir, candles, tf, atr, wt, price):
                zones.append(z)

            # ── 11. Liquidity Voids (to'ldirilmagan bo'shliq) ────
            for lv in ict.structure.get("liq_voids", []):
                z = self._liq_void_zone(lv, want_dir, price, buf, wt * 0.9, tf)
                if z: zones.append(z)

            # ── 12. EQL / EQH / IDM unswept → approach zone ──────
            for lz in ict.liquidity_zones:
                if lz.get("swept"):
                    continue
                if lz.get("type", "") in ("EQH", "EQL", "IDM_buy", "IDM_sell"):
                    z = self._unswept_liq_zone(lz, want_dir, price, atr, wt, tf)
                    if z: zones.append(z)

            # ── 13. Dealing Range Equilibrium (50% level) ─────────
            dr = ict.structure.get("dealing_range", {})
            if dr and float(dr.get("eq", 0)) > 0:
                z = self._dealing_eq_zone(dr, want_dir, price, atr, wt, tf)
                if z: zones.append(z)

            # ── 14. BOS points → S/R retest zone ──────────────────
            for bp in ict.structure.get("bos_points", []):
                z = self._bos_sr_zone(bp, want_dir, price, atr, wt * 0.8, tf)
                if z: zones.append(z)

            # ── 15. CISD level → key reaction zone ────────────────
            cisd_on  = ict.structure.get("cisd", False)
            cisd_lv  = float(ict.structure.get("cisd_level", 0))
            cisd_dir = ict.structure.get("cisd_dir", "none")
            if cisd_on and cisd_lv > 0:
                want = "bullish" if want_dir == "buy" else "bearish"
                if cisd_dir == want:
                    z = self._bos_sr_zone(
                        {"dir": cisd_dir, "price": cisd_lv}, want_dir, price, atr, wt * 1.2, tf
                    )
                    if z:
                        z["label"] = f"{tf}_CISD"
                        zones.append(z)

        # Apply multipliers (trend + SB + HTF confluence)
        composite = trend_bonus * sb_bonus * htf_bonus
        # TF → candles lookup for swing TP
        tf_candles = {row[0]: row[3] for row in tf_ict_data}
        for z in zones:
            z["weight"] = round(z["weight"] * composite, 2)
            z["htf_conf"] = htf_conf
            z["silver_bullet"] = silver_bullet
            z_tf = z.get("tf", "")
            if z_tf != "M1":
                cdl = tf_candles.get(z_tf)
                if cdl is None:
                    cdl = tf_candles.get("H1")
                sl_p = abs(z["entry"] - z["sl"]) / self.PIP
                z["tp1"], z["tp2"], z["tp3"] = self._swing_tp_targets(
                    z["entry"], z["direction"], sl_p, cdl, self.TP_MAX_PIPS
                )
                # Structural SL: swing low/high dan tashqarida
                refined_sl = self._structural_sl(z["direction"], z["entry"], cdl, z["sl"])
                if refined_sl is not None:
                    z["sl"] = refined_sl

        # Deduplicate (1.5 pip buffer)
        deduped = []
        for z in sorted(zones, key=lambda x: -x["weight"]):
            if not any(abs(d["entry"] - z["entry"]) < 1.5 for d in deduped):
                deduped.append(z)

        return deduped[:20]

    # ── OB/BB/MB zone helper ──────────────────────────────────────

    def _ob_zone(self, ob: dict, want_dir: str, price: float,
                 buf: float, wt: float, tf: str, label_suffix: str) -> dict:
        hi = float(ob.get("high", 0))
        lo = float(ob.get("low",  0))
        if hi <= 0 or lo <= 0 or hi <= lo:
            return None

        typ = ob.get("type", "")
        mid = (hi + lo) / 2

        rng = hi - lo
        MAX_DIST = 200 * self.PIP

        sl_min = self.SL_MIN_PIPS
        tp_max = self.TP_MAX_PIPS

        if want_dir == "buy" and ("bullish" in typ or "buy" in typ or "mitigation" in typ):
            if price > hi + MAX_DIST:
                return None
            if price < lo - MAX_DIST:
                return None
            # Entry at 30% of OB (sniper — not chasing)
            entry_p = round(lo + rng * 0.30, 2)
            sl      = round(lo - buf, 2)
            sl_p    = (entry_p - sl) / self.PIP
            if sl_p < sl_min:
                sl   = round(entry_p - sl_min * self.PIP, 2)
                sl_p = sl_min
            if sl_p > self.SL_MAX_PIPS:
                sl   = round(entry_p - self.SL_MAX_PIPS * self.PIP, 2)
                sl_p = self.SL_MAX_PIPS
            if not (sl_min <= sl_p <= self.SL_MAX_PIPS):
                return None
            bonus = 1 if lo <= price <= hi else 0
            tp1, tp2, tp3 = self._rr_targets(entry_p, "buy", sl_p, tp_max)
            return {
                "direction": "buy", "entry": entry_p, "sl": sl,
                "zone_lo": lo, "zone_hi": hi,
                "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "label": f"{tf}_{label_suffix}", "weight": wt * 3 + bonus, "tf": tf,
            }

        elif want_dir == "sell" and ("bearish" in typ or "sell" in typ or "mitigation" in typ):
            if price < lo - MAX_DIST:
                return None
            if price > hi + MAX_DIST:
                return None
            # Entry at 30% of OB (sniper — not chasing)
            entry_p = round(hi - rng * 0.30, 2)
            sl      = round(hi + buf, 2)
            sl_p    = (sl - entry_p) / self.PIP
            if sl_p < sl_min:
                sl   = round(entry_p + sl_min * self.PIP, 2)
                sl_p = sl_min
            if sl_p > self.SL_MAX_PIPS:
                sl   = round(entry_p + self.SL_MAX_PIPS * self.PIP, 2)
                sl_p = self.SL_MAX_PIPS
            if not (sl_min <= sl_p <= self.SL_MAX_PIPS):
                return None
            bonus = 1 if lo <= price <= hi else 0
            tp1, tp2, tp3 = self._rr_targets(entry_p, "sell", sl_p, tp_max)
            return {
                "direction": "sell", "entry": entry_p, "sl": sl,
                "zone_lo": lo, "zone_hi": hi,
                "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "label": f"{tf}_{label_suffix}", "weight": wt * 3 + bonus, "tf": tf,
            }
        return None

    # ── FVG zone helper ───────────────────────────────────────────

    def _fvg_zone(self, fvg: dict, want_dir: str, price: float,
                  buf: float, wt: float, tf: str) -> dict:
        hi  = float(fvg.get("high", 0))
        lo  = float(fvg.get("low",  0))
        ce  = float(fvg.get("ce",   (hi + lo) / 2 if hi and lo else 0))
        typ = fvg.get("type", "")

        if hi <= 0 or lo <= 0 or hi <= lo:
            return None

        is_bull_fvg = ("bullish" in typ or "sibi" in typ or "ifvg_bullish" in typ or "bpr" in typ)
        is_bear_fvg = ("bearish" in typ or "bisi" in typ or "ifvg_bearish" in typ or "bpr" in typ)

        rng_fvg  = hi - lo
        MAX_DIST = 200 * self.PIP
        sl_min   = self.SL_MIN_PIPS
        tp_max   = self.TP_MAX_PIPS

        if want_dir == "buy" and is_bull_fvg:
            if price > hi + MAX_DIST:
                return None
            if price < lo - MAX_DIST:
                return None
            # Sniper: FVG pastidan 10% yuqorida (lo edge kirish)
            entry_p = round(lo + rng_fvg * 0.10, 2)
            sl      = round(lo - buf, 2)
            sl_p    = (entry_p - sl) / self.PIP
            if sl_p < sl_min:
                sl   = round(entry_p - sl_min * self.PIP, 2)
                sl_p = sl_min
            if sl_p > self.SL_MAX_PIPS:
                sl   = round(entry_p - self.SL_MAX_PIPS * self.PIP, 2)
                sl_p = self.SL_MAX_PIPS
            if not (sl_min <= sl_p <= self.SL_MAX_PIPS):
                return None
            tp1, tp2, tp3 = self._rr_targets(entry_p, "buy", sl_p, tp_max)
            return {
                "direction": "buy", "entry": entry_p, "sl": sl,
                "zone_lo": lo, "zone_hi": hi,
                "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "label": f"{tf}_{typ.upper()[:4]}", "weight": wt * 2, "tf": tf,
            }

        elif want_dir == "sell" and is_bear_fvg:
            if price < lo - MAX_DIST:
                return None
            if price > hi + MAX_DIST:
                return None
            # Sniper: FVG tepasidan 10% pastda
            entry_p = round(hi - rng_fvg * 0.10, 2)
            sl      = round(hi + buf, 2)
            sl_p    = (sl - entry_p) / self.PIP
            if sl_p < sl_min:
                sl   = round(entry_p + sl_min * self.PIP, 2)
                sl_p = sl_min
            if sl_p > self.SL_MAX_PIPS:
                sl   = round(entry_p + self.SL_MAX_PIPS * self.PIP, 2)
                sl_p = self.SL_MAX_PIPS
            if not (sl_min <= sl_p <= self.SL_MAX_PIPS):
                return None
            tp1, tp2, tp3 = self._rr_targets(entry_p, "sell", sl_p, tp_max)
            return {
                "direction": "sell", "entry": entry_p, "sl": sl,
                "zone_lo": lo, "zone_hi": hi,
                "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "label": f"{tf}_{typ.upper()[:4]}", "weight": wt * 2, "tf": tf,
            }
        return None

    # ── Liquidity zone helper ─────────────────────────────────────

    def _liq_zone(self, lz: dict, want_dir: str, price: float,
                  atr: float, wt: float, tf: str) -> dict:
        """After liquidity sweep → reversal entry zone."""
        if not lz.get("swept"):
            return None

        lz_type = lz.get("type", "")
        lz_p    = float(lz.get("price", 0))
        if lz_p <= 0:
            return None

        # SSL swept → buy reversal (price swept lows, now bouncing up)
        if want_dir == "buy" and lz_type in ("SSL", "ERL_low", "EQL"):
            entry_p = round(lz_p + atr * 0.1, 2)
            sl      = round(lz_p - atr * 0.3, 2)
            sl_p    = (entry_p - sl) / self.PIP
            if not (self.SL_MIN_PIPS <= sl_p <= self.SL_MAX_PIPS):
                return None
            if price < sl:
                return None
            tp1, tp2, tp3 = self._rr_targets(entry_p, "buy", sl_p)
            return {
                "direction": "buy", "entry": entry_p, "sl": sl,
                "zone_lo": sl, "zone_hi": entry_p + atr * 0.2,
                "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "label": f"{tf}_{lz_type}_sweep", "weight": wt * 2.5, "tf": tf,
            }

        # BSL swept → sell reversal
        elif want_dir == "sell" and lz_type in ("BSL", "ERL_high", "EQH"):
            entry_p = round(lz_p - atr * 0.1, 2)
            sl      = round(lz_p + atr * 0.3, 2)
            sl_p    = (sl - entry_p) / self.PIP
            if not (self.SL_MIN_PIPS <= sl_p <= self.SL_MAX_PIPS):
                return None
            if price > sl:
                return None
            tp1, tp2, tp3 = self._rr_targets(entry_p, "sell", sl_p)
            return {
                "direction": "sell", "entry": entry_p, "sl": sl,
                "zone_lo": entry_p - atr * 0.2, "zone_hi": sl,
                "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "label": f"{tf}_{lz_type}_sweep", "weight": wt * 2.5, "tf": tf,
            }
        return None

    # ── RR targets ───────────────────────────────────────────────

    # ── Yangi zone helperlari: LiqVoid / UnsweptLiq / DR Eq / BOS S/R ──

    def _liq_void_zone(self, lv: dict, want_dir: str, price: float,
                       buf: float, wt: float, tf: str):
        """Liquidity Void — katta impulse shamining bo'shlig'i, narx qaytib to'ldiradi."""
        hi  = float(lv.get("high", 0))
        lo  = float(lv.get("low",  0))
        typ = lv.get("type", "")
        if hi <= 0 or lo <= 0 or hi <= lo:
            return None
        MAX_DIST = 200 * self.PIP
        if want_dir == "buy" and "bull" in typ:
            if not (lo - MAX_DIST <= price <= hi + MAX_DIST):
                return None
            entry_p = round(lo + (hi - lo) * 0.2, 2)
            sl      = round(lo - buf * 1.5, 2)
            sl_p    = (entry_p - sl) / self.PIP
            if sl_p < self.SL_MIN_PIPS:
                sl   = round(entry_p - self.SL_MIN_PIPS * self.PIP, 2); sl_p = self.SL_MIN_PIPS
            if sl_p > self.SL_MAX_PIPS: return None
            tp1, tp2, tp3 = self._rr_targets(entry_p, "buy", sl_p)
            return {"direction": "buy", "entry": entry_p, "sl": sl,
                    "zone_lo": lo, "zone_hi": hi,
                    "tp1": tp1, "tp2": tp2, "tp3": tp3,
                    "label": f"{tf}_LiqVoid", "weight": wt * 2.2, "tf": tf}
        elif want_dir == "sell" and "bear" in typ:
            if not (lo - MAX_DIST <= price <= hi + MAX_DIST):
                return None
            entry_p = round(hi - (hi - lo) * 0.2, 2)
            sl      = round(hi + buf * 1.5, 2)
            sl_p    = (sl - entry_p) / self.PIP
            if sl_p < self.SL_MIN_PIPS:
                sl   = round(entry_p + self.SL_MIN_PIPS * self.PIP, 2); sl_p = self.SL_MIN_PIPS
            if sl_p > self.SL_MAX_PIPS: return None
            tp1, tp2, tp3 = self._rr_targets(entry_p, "sell", sl_p)
            return {"direction": "sell", "entry": entry_p, "sl": sl,
                    "zone_lo": lo, "zone_hi": hi,
                    "tp1": tp1, "tp2": tp2, "tp3": tp3,
                    "label": f"{tf}_LiqVoid", "weight": wt * 2.2, "tf": tf}
        return None

    def _unswept_liq_zone(self, lz: dict, want_dir: str, price: float,
                          atr: float, wt: float, tf: str):
        """EQL/EQH/IDM unswept — narx yaqinlashganda limit entry."""
        lz_type = lz.get("type", "")
        lz_p    = float(lz.get("price", lz.get("high", lz.get("low", 0))))
        if lz_p <= 0: return None
        MAX_DIST = 150 * self.PIP
        if want_dir == "buy" and lz_type in ("EQL", "IDM_buy"):
            if not (lz_p <= price <= lz_p + MAX_DIST): return None
            entry_p = round(lz_p + atr * 0.06, 2)
            sl      = round(lz_p - atr * 0.40, 2)
            sl_p    = (entry_p - sl) / self.PIP
            if sl_p < self.SL_MIN_PIPS:
                sl   = round(entry_p - self.SL_MIN_PIPS * self.PIP, 2); sl_p = self.SL_MIN_PIPS
            if sl_p > self.SL_MAX_PIPS: return None
            tp1, tp2, tp3 = self._rr_targets(entry_p, "buy", sl_p)
            return {"direction": "buy", "entry": entry_p, "sl": sl,
                    "zone_lo": lz_p - atr * 0.1, "zone_hi": lz_p + atr * 0.1,
                    "tp1": tp1, "tp2": tp2, "tp3": tp3,
                    "label": f"{tf}_{lz_type}", "weight": wt * 1.6, "tf": tf}
        elif want_dir == "sell" and lz_type in ("EQH", "IDM_sell"):
            if not (lz_p - MAX_DIST <= price <= lz_p): return None
            entry_p = round(lz_p - atr * 0.06, 2)
            sl      = round(lz_p + atr * 0.40, 2)
            sl_p    = (sl - entry_p) / self.PIP
            if sl_p < self.SL_MIN_PIPS:
                sl   = round(entry_p + self.SL_MIN_PIPS * self.PIP, 2); sl_p = self.SL_MIN_PIPS
            if sl_p > self.SL_MAX_PIPS: return None
            tp1, tp2, tp3 = self._rr_targets(entry_p, "sell", sl_p)
            return {"direction": "sell", "entry": entry_p, "sl": sl,
                    "zone_lo": lz_p - atr * 0.1, "zone_hi": lz_p + atr * 0.1,
                    "tp1": tp1, "tp2": tp2, "tp3": tp3,
                    "label": f"{tf}_{lz_type}", "weight": wt * 1.6, "tf": tf}
        return None

    def _dealing_eq_zone(self, dr: dict, want_dir: str, price: float,
                         atr: float, wt: float, tf: str):
        """Dealing Range Equilibrium (50%) — market maker'lar akumullash/distribusiya zonasi."""
        eq        = float(dr.get("eq", 0))
        zone_curr = dr.get("current_zone", "")
        if eq <= 0: return None
        dist = abs(price - eq) / self.PIP
        if dist > 60 or dist < 1: return None
        if want_dir == "buy" and price < eq and zone_curr == "discount":
            entry_p = round(eq - atr * 0.06, 2)
            sl      = round(eq - atr * 0.50, 2)
            sl_p    = (entry_p - sl) / self.PIP
            if sl_p < self.SL_MIN_PIPS:
                sl   = round(entry_p - self.SL_MIN_PIPS * self.PIP, 2); sl_p = self.SL_MIN_PIPS
            if sl_p > self.SL_MAX_PIPS: return None
            tp1, tp2, tp3 = self._rr_targets(entry_p, "buy", sl_p)
            return {"direction": "buy", "entry": entry_p, "sl": sl,
                    "zone_lo": eq - atr * 0.1, "zone_hi": eq + atr * 0.05,
                    "tp1": tp1, "tp2": tp2, "tp3": tp3,
                    "label": f"{tf}_DR_Eq", "weight": wt * 1.9, "tf": tf}
        elif want_dir == "sell" and price > eq and zone_curr == "premium":
            entry_p = round(eq + atr * 0.06, 2)
            sl      = round(eq + atr * 0.50, 2)
            sl_p    = (sl - entry_p) / self.PIP
            if sl_p < self.SL_MIN_PIPS:
                sl   = round(entry_p + self.SL_MIN_PIPS * self.PIP, 2); sl_p = self.SL_MIN_PIPS
            if sl_p > self.SL_MAX_PIPS: return None
            tp1, tp2, tp3 = self._rr_targets(entry_p, "sell", sl_p)
            return {"direction": "sell", "entry": entry_p, "sl": sl,
                    "zone_lo": eq - atr * 0.05, "zone_hi": eq + atr * 0.1,
                    "tp1": tp1, "tp2": tp2, "tp3": tp3,
                    "label": f"{tf}_DR_Eq", "weight": wt * 1.9, "tf": tf}
        return None

    def _bos_sr_zone(self, bp: dict, want_dir: str, price: float,
                     atr: float, wt: float, tf: str):
        """BOS/CISD level — singan struktura yangi S/R sifatida qaytariladi."""
        bos_p   = float(bp.get("price", 0))
        bos_dir = bp.get("dir", "none")
        if bos_p <= 0: return None
        dist = abs(price - bos_p) / self.PIP
        if dist > 80 or dist < 0.5: return None
        if want_dir == "buy" and bos_dir == "bullish" and price >= bos_p:
            entry_p = round(bos_p + atr * 0.05, 2)
            sl      = round(bos_p - atr * 0.40, 2)
            sl_p    = (entry_p - sl) / self.PIP
            if sl_p < self.SL_MIN_PIPS:
                sl   = round(entry_p - self.SL_MIN_PIPS * self.PIP, 2); sl_p = self.SL_MIN_PIPS
            if sl_p > self.SL_MAX_PIPS: return None
            tp1, tp2, tp3 = self._rr_targets(entry_p, "buy", sl_p)
            return {"direction": "buy", "entry": entry_p, "sl": sl,
                    "zone_lo": bos_p - atr * 0.1, "zone_hi": bos_p + atr * 0.1,
                    "tp1": tp1, "tp2": tp2, "tp3": tp3,
                    "label": f"{tf}_BOS_S", "weight": wt * 2.4, "tf": tf}
        elif want_dir == "sell" and bos_dir == "bearish" and price <= bos_p:
            entry_p = round(bos_p - atr * 0.05, 2)
            sl      = round(bos_p + atr * 0.40, 2)
            sl_p    = (sl - entry_p) / self.PIP
            if sl_p < self.SL_MIN_PIPS:
                sl   = round(entry_p + self.SL_MIN_PIPS * self.PIP, 2); sl_p = self.SL_MIN_PIPS
            if sl_p > self.SL_MAX_PIPS: return None
            tp1, tp2, tp3 = self._rr_targets(entry_p, "sell", sl_p)
            return {"direction": "sell", "entry": entry_p, "sl": sl,
                    "zone_lo": bos_p - atr * 0.1, "zone_hi": bos_p + atr * 0.1,
                    "tp1": tp1, "tp2": tp2, "tp3": tp3,
                    "label": f"{tf}_BOS_R", "weight": wt * 2.4, "tf": tf}
        return None

    def _rr_targets(self, entry: float, direction: str, sl_pips: float, tp_max: float = 150.0) -> tuple:
        """TP1=1.5R  TP2=2.5R  TP3=4R — tp_max bilan cheklangan."""
        sign  = 1 if direction == "buy" else -1
        tp1_p = min(sl_pips * 1.5, tp_max)
        tp2_p = min(sl_pips * 2.5, tp_max)
        tp3_p = min(sl_pips * 4.0, tp_max)
        tp1   = round(entry + sign * tp1_p * self.PIP, 2)
        tp2   = round(entry + sign * tp2_p * self.PIP, 2)
        tp3   = round(entry + sign * tp3_p * self.PIP, 2)
        return tp1, tp2, tp3

    def _structural_sl(self, direction: str, entry: float, candles, zone_sl: float) -> float:
        """
        Fractal swing low (buy) yoki swing high (sell) dan tashqarida SL qo'yadi.
        40-50 pip oralig'ida bo'lishi shart. Topilmasa — zone SL qaytaradi.
        """
        if candles is None or len(candles) < 10:
            return None
        hi = candles["high"].astype(float).values
        lo = candles["low"].astype(float).values
        n  = min(80, len(hi))
        start = max(2, len(hi) - n)
        buf = 3 * self.PIP  # swing dan 3 pip chetroqda

        if direction == "buy":
            swings = []
            for i in range(start + 2, len(lo) - 1):
                v = lo[i]
                if v < lo[i-1] and v < lo[i-2] and v < lo[i+1]:
                    if v < entry:
                        swings.append(v)
            swings.sort(reverse=True)  # entry'ga eng yaqindan
            for sw in swings:
                sl_cand = round(sw - buf, 2)
                sl_p = (entry - sl_cand) / self.PIP
                if self.SL_MIN_PIPS <= sl_p <= self.SL_MAX_PIPS:
                    return sl_cand
        else:
            swings = []
            for i in range(start + 2, len(hi) - 1):
                v = hi[i]
                if v > hi[i-1] and v > hi[i-2] and v > hi[i+1]:
                    if v > entry:
                        swings.append(v)
            swings.sort()  # entry'ga eng yaqindan
            for sw in swings:
                sl_cand = round(sw + buf, 2)
                sl_p = (sl_cand - entry) / self.PIP
                if self.SL_MIN_PIPS <= sl_p <= self.SL_MAX_PIPS:
                    return sl_cand
        return None

    def _liquidity_sl(self, direction: str, entry: float, ict_map: dict) -> float:
        """Nearest unswept SSL (buy) yoki BSL (sell) dan tashqarida SL qo'yadi.
        Valid range: SL_MIN_PIPS–SL_MAX_PIPS. Topilmasa None qaytaradi."""
        buf = 3 * self.PIP
        best = None
        for tf in ("H1", "H4", "M30", "M15"):
            ict = ict_map.get(tf)
            if ict is None:
                continue
            for lz in ict.liquidity_zones:
                if lz.get("swept"):
                    continue
                lz_p = float(lz.get("price", 0))
                if lz_p <= 0:
                    continue
                lz_type = lz.get("type", "")
                if direction == "buy" and lz_type in ("SSL", "ERL_low", "EQL"):
                    if lz_p < entry:
                        sl_cand = round(lz_p - buf, 2)
                        sl_p = (entry - sl_cand) / self.PIP
                        if self.SL_MIN_PIPS <= sl_p <= self.SL_MAX_PIPS:
                            if best is None or sl_cand > best:
                                best = sl_cand
                elif direction == "sell" and lz_type in ("BSL", "ERL_high", "EQH"):
                    if lz_p > entry:
                        sl_cand = round(lz_p + buf, 2)
                        sl_p = (sl_cand - entry) / self.PIP
                        if self.SL_MIN_PIPS <= sl_p <= self.SL_MAX_PIPS:
                            if best is None or sl_cand < best:
                                best = sl_cand
        return best

    def _swing_tp_targets(
        self, entry: float, direction: str, sl_pips: float,
        candles, tp_max: float = 150.0,
    ) -> tuple:
        """
        Fractal swing high/low ga asoslangan TP.
        TP1 = eng yaqin swing liquidity, TP2 = o'rtacha, TP3 = uzoqdagi.
        Swing fractal: har ikki tomonida 2 ta past/yuqori sham bo'lishi kerak.
        """
        if candles is None or len(candles) < 10:
            return self._rr_targets(entry, direction, sl_pips, tp_max)

        hi = candles["high"].astype(float).values
        lo = candles["low"].astype(float).values
        n  = min(150, len(hi))
        start = max(2, len(hi) - n)

        swings = []

        if direction == "buy":
            # Fractal swing highs: hi[i] > hi[i-1] and hi[i] > hi[i-2] and hi[i] > hi[i+1]
            for i in range(start + 2, len(hi) - 1):
                v = hi[i]
                if v > hi[i-1] and v > hi[i-2] and v > hi[i+1]:
                    if v > entry + 5 * self.PIP:
                        dist = (v - entry) / self.PIP
                        if dist <= tp_max:
                            swings.append(v)
            swings = sorted(set(round(s, 2) for s in swings))  # nearest first

        else:
            # Fractal swing lows: lo[i] < lo[i-1] and lo[i] < lo[i-2] and lo[i] < lo[i+1]
            for i in range(start + 2, len(lo) - 1):
                v = lo[i]
                if v < lo[i-1] and v < lo[i-2] and v < lo[i+1]:
                    if v < entry - 5 * self.PIP:
                        dist = (entry - v) / self.PIP
                        if dist <= tp_max:
                            swings.append(v)
            swings = sorted(set(round(s, 2) for s in swings), reverse=True)  # nearest first

        if len(swings) >= 3:
            return swings[0], swings[len(swings) // 2], swings[-1]
        elif len(swings) == 2:
            fb = self._rr_targets(entry, direction, sl_pips, tp_max)
            return swings[0], swings[1], fb[2]
        elif len(swings) == 1:
            fb = self._rr_targets(entry, direction, sl_pips, tp_max)
            return swings[0], fb[1], fb[2]

        return self._rr_targets(entry, direction, sl_pips, tp_max)

    # ── Place limit orders for zones ──────────────────────────────

    async def _place_zone_limits(
        self, zones: list, want_dir: str, account: dict, current_price: float = 0.0,
        ict_map: dict = None, silver_bullet: bool = False, open_positions: list = None,
        mode: str = "FLOW", impulse_dir: str = "none", h1=None,
        d1_bias: str = "N/A", h4_trend: str = "N/A", h1_trend: str = "N/A",
        w1_bias: str = "N/A",
    ):
        # ── Mode-specific parametrlar ─────────────────────────────
        # TP3: impuls yo'nalishi mos bo'lsa 250 pip, aks holda max 150 pip
        has_impulse = (impulse_dir == want_dir)
        tp3_max = 250 if has_impulse else 150

        if mode == "SNIPER":
            risk_pct  = self.RISK_SNIPER
            sl_min_m  = self.SL_SNIPER_MIN
            sl_max_m  = self.SL_SNIPER_MAX
            tp_fixed  = (self.TP_SNIPER_1, self.TP_SNIPER_2, tp3_max)
        else:
            risk_pct  = self.RISK_FLOW
            sl_min_m  = self.SL_FLOW_MIN
            sl_max_m  = self.SL_FLOW_MAX
            tp_fixed  = (self.TP_FLOW_1, self.TP_FLOW_2, tp3_max)

        # Streak <= -3: risk 50% kamaytirish
        if self._streak <= -3:
            risk_pct *= 0.5
            logger.debug(f"Streak {self._streak}: risk 50% → {risk_pct*100:.2f}%")

        balance  = account.get("balance", 10000)
        sym_info = self.mt5.get_symbol_info(self.symbol)
        point    = sym_info.get("point",            0.01)
        tick_val = sym_info.get("trade_tick_value", 1.0)
        vol_min  = sym_info.get("volume_min",       0.01)
        vol_max  = sym_info.get("volume_max",       500.0)
        vol_step = sym_info.get("volume_step",      0.01)

        # Shu yo'nalishda ochiq + pending orderlar soni
        open_dir_cnt = sum(
            1 for p in (open_positions or [])
            if isinstance(p, dict) and ("buy" if p.get("type", -1) == 0 else "sell") == want_dir
        )
        existing    = len([v for v in self._pending_zones.values() if v["direction"] == want_dir])
        max_limit   = 4   # har yo'nalishda max 4 pending limit order (har zona 1 ta)
        ict_map     = ict_map or {}
        placed_sets = 0

        # Ochiq pozitsiya bo'lsa ham limit qo'yishda davom etamiz

        # M30+ HTF entries faqat London/NY/Overlap sessiyada (Kill Zone)
        in_active_session = get_current_session() in ("london", "overlap", "newyork")

        short_tfs = ("M1", "M5", "M15")

        # M5/M15 entry: faqat H4 bias aniq va kamida 2 HTF tasdiq bo'lganda
        h4_bias_clear = want_dir in ("buy", "sell")
        short_zones = [
            z for z in zones
            if z.get("tf") in ("M5", "M15")
            and h4_bias_clear
            and z.get("htf_conf", 0) >= 2
        ] if h4_bias_clear else []

        long_zones = [
            z for z in zones
            if z.get("tf") not in short_tfs
        ]

        # Eng yaxshi zonalarni og'irlik bo'yicha saralash
        long_zones  = sorted(long_zones,  key=lambda z: -z.get("weight", 0))
        short_zones = sorted(short_zones, key=lambda z: -z.get("weight", 0))

        # FLOW: 4 HTF + 2 LTF; SNIPER: 5 HTF + 2 LTF
        if mode == "FLOW":
            ordered_zones = long_zones[:4] + short_zones[:2]
            if not ordered_zones:
                ordered_zones = short_zones[:4]
        else:
            ordered_zones = long_zones[:5] + short_zones[:2]
            if not ordered_zones:
                ordered_zones = long_zones[:5]


        for zone in ordered_zones:
            if existing + placed_sets >= max_limit:
                break

            entry = zone["entry"]
            sl    = zone["sl"]

            if entry <= 0 or sl <= 0:
                continue
            if want_dir == "buy"  and sl >= entry:
                continue
            if want_dir == "sell" and sl <= entry:
                continue

            # BUY LIMIT: entry joriy narxdan past bo'lishi shart
            # SELL LIMIT: entry joriy narxdan yuqori bo'lishi shart
            if current_price > 0:
                if want_dir == "buy"  and entry >= current_price:
                    continue
                if want_dir == "sell" and entry <= current_price:
                    continue
                # Uzoqlik filtri: 400 pip (HTF zonalari uzoqda bo'lishi mumkin)
                if abs(current_price - entry) > 400 * self.PIP:
                    continue

            # Minimum quality: faqat score=0 bo'lsa skip
            zone_q_pre = zone.get("quality", 0)
            if zone_q_pre < 1:
                continue

            # Spread vs SL nisbati: spread > 40% of SL → skip
            sl_pips_check = abs(entry - sl) / self.PIP
            if self._current_spread_p > 0 and sl_pips_check > 0:
                if self._current_spread_p / sl_pips_check > 0.40:
                    logger.debug(
                        f"Spread {self._current_spread_p:.1f}p > 40% of SL {sl_pips_check:.0f}p — skip"
                    )
                    continue

            # Otib ketgan / ishlatilgan zona tekshiruvi
            if any(abs(entry - uz) <= 0.8 for uz in self._used_zones):
                continue

            # Duplicate zone check — narx yaqinligi VA bir xil label
            zone_label = zone.get("label", "")
            already_price = any(
                abs(v["entry"] - entry) <= 1.5
                for v in self._pending_zones.values()
                if v["direction"] == want_dir
            )
            already_label = any(
                v.get("label", "") == zone_label
                for v in self._pending_zones.values()
                if v["direction"] == want_dir
            )
            if already_price or already_label:
                continue

            # SelfLearner: entry_pct adaptation (OB/FVG zone ichida kirish joyi)
            zone_lo = zone.get("zone_lo", entry)
            zone_hi = zone.get("zone_hi", entry)
            rng_z   = zone_hi - zone_lo
            if rng_z > 0:
                ep = self.learner.get_entry_pct()
                if want_dir == "buy":
                    entry = round(zone_lo + rng_z * ep, 2)
                else:
                    entry = round(zone_hi - rng_z * ep, 2)

            sl_dist = abs(entry - sl)
            zone_q  = zone.get("quality", 0)
            sign    = "+" if want_dir == "buy" else "-"
            sl_p    = sl_dist / self.PIP

            # SL mode-specific range tekshiruvi
            if sl_p < sl_min_m:
                sl = round(
                    entry - sl_min_m * self.PIP if want_dir == "buy"
                    else entry + sl_min_m * self.PIP, 2
                )
                sl_p = sl_min_m
            if sl_p > sl_max_m:
                sl = round(
                    entry - sl_max_m * self.PIP if want_dir == "buy"
                    else entry + sl_max_m * self.PIP, 2
                )
                sl_p = sl_max_m
            sl_dist = sl_p * self.PIP

            # TP — minimum 40 pip majburiy, swing-based override
            s   = 1 if want_dir == "buy" else -1
            MIN_TP_PIPS = 40
            tp1 = zone.get("tp1") or round(entry + s * tp_fixed[0] * self.PIP, 2)
            tp2 = zone.get("tp2") or round(entry + s * tp_fixed[1] * self.PIP, 2)
            tp3 = zone.get("tp3") or round(entry + s * tp_fixed[2] * self.PIP, 2)
            # TP1 minimum 40 pip — kichik bo'lsa majburan 40 pip qilamiz
            if abs(tp1 - entry) / self.PIP < MIN_TP_PIPS:
                tp1 = round(entry + s * MIN_TP_PIPS * self.PIP, 2)
            if abs(tp2 - entry) / self.PIP < MIN_TP_PIPS * 2:
                tp2 = round(entry + s * MIN_TP_PIPS * 2 * self.PIP, 2)
            if abs(tp3 - entry) / self.PIP < MIN_TP_PIPS * 3:
                tp3 = round(entry + s * MIN_TP_PIPS * 3 * self.PIP, 2)
            tp1_p = abs(tp1 - entry) / self.PIP
            tp2_p = abs(tp2 - entry) / self.PIP
            tp3_p = abs(tp3 - entry) / self.PIP

            # ── Dynamic liquidity SL refinement ──────────────────
            # Nearest unswept SSL/BSL dan tashqarida SL qo'yish
            liq_sl = self._liquidity_sl(want_dir, entry, ict_map)
            if liq_sl is not None:
                if want_dir == "buy" and liq_sl > sl:
                    sl   = liq_sl
                    sl_p = (entry - sl) / self.PIP
                    logger.debug(f"LiqSL BUY: SL→{sl:.2f} ({sl_p:.0f}p) [SSL]")
                elif want_dir == "sell" and liq_sl < sl:
                    sl   = liq_sl
                    sl_p = (sl - entry) / self.PIP
                    logger.debug(f"LiqSL SELL: SL→{sl:.2f} ({sl_p:.0f}p) [BSL]")
                sl_dist = abs(entry - sl)

            # ── ATR dynamic lot sizing ────────────────────────────
            # Bazaviy ATR: XAUUSD H1 normal ~15 pip
            # Agar hozirgi ATR yuqori bo'lsa → lot kamaytir (xavf bir xil dollar)
            BASE_ATR_PIPS = 15.0
            zone_atr_pips = self._calc_atr(h1) / self.PIP if h1 is not None else BASE_ATR_PIPS
            atr_multiplier = BASE_ATR_PIPS / max(zone_atr_pips, 5.0)
            atr_multiplier = max(0.5, min(atr_multiplier, 1.5))  # 0.5x – 1.5x oralig'i

            risk_amt_z = balance * risk_pct * atr_multiplier
            sl_pts     = sl_dist / point if point > 0 else 1
            total_lot  = risk_amt_z / (sl_pts * tick_val) if sl_pts > 0 else 0.01
            lot_each   = max(vol_min, total_lot)
            lot_each   = round(round(lot_each / vol_step) * vol_step, 2)
            lot_each   = min(lot_each, vol_max)

            # ── BE re-entry: lot 50% kamaytirish ─────────────────
            _be_cand = self._be_reentry_candidates.get(want_dir)
            _is_be_reentry = False
            if _be_cand:
                _cand_age = (get_clock().now() - _be_cand["time"]).total_seconds()
                if _cand_age < 7200:  # 2h ichida
                    lot_each = max(vol_min, round(round(lot_each * 0.5 / vol_step) * vol_step, 2))
                    _is_be_reentry = True
                    logger.info(f"BE re-entry {want_dir.upper()}: lot 50% → {lot_each}")
                else:
                    del self._be_reentry_candidates[want_dir]

            # ── Market yoki Limit qaror ───────────────────────────────
            zone_lo  = zone.get("zone_lo", entry - sl_dist * 0.5)
            zone_hi  = zone.get("zone_hi", entry + sl_dist * 0.5)
            zone_tf_now = zone.get("tf", "H4")

            # Barcha TF uchun limit order — market entry yo'q
            do_market = False

            # ── SL adjustment closure (needed by AI block + market/limit paths) ──
            def _adj_sl(e):
                s  = sl
                sp = abs(e - s) / self.PIP
                if sp < sl_min_m:
                    s  = round(e - sl_min_m * self.PIP if want_dir == "buy" else e + sl_min_m * self.PIP, 2)
                    sp = sl_min_m
                if sp > sl_max_m:
                    s  = round(e - sl_max_m * self.PIP if want_dir == "buy" else e + sl_max_m * self.PIP, 2)
                    sp = sl_max_m
                return s, sp
            lmt_sl, lmt_sl_p = _adj_sl(entry)

            # ── AI Brain: Claude API — HARD GATE (reject = skip) ────
            is_short_tf = zone_tf_now in ("M1", "M5", "M15")
            ai_note = ""
            if self.brain._enabled:
                _tp1_for_ai = tp1 if tp1 else round(entry + s * max(40, lmt_sl_p * 2.0) * self.PIP, 2)
                ai_signal = TradeSignal(
                    symbol=self.symbol,
                    direction=want_dir,
                    timeframe=zone_tf_now,
                    entry=entry,
                    sl=lmt_sl,
                    tp1=_tp1_for_ai,
                    tp2=tp2,
                    tp3=tp3,
                    rr_ratio=abs(_tp1_for_ai - entry) / max(abs(entry - lmt_sl), 0.01),
                    confluence_score=float(zone_q),
                    ict_analysis=(ict_map or {}).get(zone_tf_now),
                )
                ai_market = {
                    "session":            get_current_session(),
                    "account_balance":    balance,
                    "zone_label":         zone.get("label", ""),
                    "zone_tf":            zone_tf_now,
                    "d1_trend":           d1_bias,
                    "h4_trend":           h4_trend,
                    "h1_trend":           h1_trend,
                    "w1_bias":            w1_bias,
                    "dxy_note":           f"DXY={self._dxy_trend}" if self._dxy_symbol else "N/A",
                    "learned_stats":      self.learner.get_setup_stats(),
                    "session_day_weight": 1.0,
                    "session_day_label":  get_current_session(),
                }
                try:
                    ai_dec = await self.brain.validate_signal(ai_signal, ai_market)
                    if ai_dec.approved:
                        ai_note = f"✓ AI {ai_dec.confidence:.2f} risk={ai_dec.risk_level}"
                        logger.info(f"AI ✓ {zone['label']} conf={ai_dec.confidence:.2f} risk={ai_dec.risk_level}")
                        # AI adjustments qo'llash
                        adj = ai_dec.adjustments or {}
                        if adj.get("entry") and abs(adj["entry"] - entry) < 20 * self.PIP:
                            entry = float(adj["entry"])
                        if adj.get("sl") and abs(adj["sl"] - lmt_sl) < 20 * self.PIP:
                            lmt_sl = float(adj["sl"])
                            lmt_sl_p = abs(entry - lmt_sl) / self.PIP
                        if adj.get("tp1") and abs(adj["tp1"] - _tp1_for_ai) < 50 * self.PIP:
                            tp1 = float(adj["tp1"])
                        if adj.get("lot_size_multiplier", 1.0) != 1.0:
                            m = float(adj["lot_size_multiplier"])
                            m = max(0.8, min(m, 1.2))
                            lot_each = max(vol_min, round(round(lot_each * m / vol_step) * vol_step, 2))
                    else:
                        logger.warning(
                            f"AI ✗ REJECTED {zone['label']}: {ai_dec.reasoning[:100]}"
                        )
                        continue   # AI reject → bu zonani o'tkazib yubor
                except Exception as _ae:
                    logger.warning(f"AI Brain error: {_ae} — zone skipped (conservative)")
                    continue   # AI xato → ham o'tkazib yubor (xavfsiz)

            # ── 1 ta entry: zona uchun bitta order ───────────────────
            set_id = str(__import__("uuid").uuid4())
            sb_tag  = " SB" if zone.get("silver_bullet") or silver_bullet else ""
            htf_tag = f" HTF={zone.get('htf_conf',0)}/4"

            if do_market:
                mkt_e         = current_price if current_price > 0 else entry
                mkt_sl, mkt_sl_p = _adj_sl(mkt_e)
                mkt_tp        = zone.get("tp1") or round(
                    mkt_e + mkt_sl_p * 2.0 * self.PIP if want_dir == "buy"
                    else mkt_e - mkt_sl_p * 2.0 * self.PIP, 2
                )
                mkt_tp_p      = abs(mkt_tp - mkt_e) / self.PIP
                try:
                    res = self.mt5.place_order(TradeOrder(
                        symbol=self.symbol, direction=want_dir,
                        lot_size=lot_each, sl=mkt_sl, tp=mkt_tp,
                    ))
                    self._trade_meta[res.ticket] = {
                        "set_id": set_id, "tp_label": "TP1", "tp": mkt_tp,
                        "sl": mkt_sl, "sl_pips": mkt_sl_p,
                        "entry": res.price, "direction": want_dir,
                        "tf": zone_tf_now, "be_done": False, "pnl": 0,
                        "mode": mode, "label": zone.get("label", "OB"),
                    }
                    save_trade_meta(self._trade_meta)  # F0-1
                    self.risk.record_trade_opened()
                    placed_sets += 1
                    self._last_order_at[want_dir] = get_clock().now()
                    logger.success(
                        f"\n{'═'*60}\n"
                        f"  MARKET | {want_dir.upper()} | {zone['label']} [{zone_tf_now}]{sb_tag}{htf_tag}\n"
                        f"  Entry: {mkt_e:.2f}  SL: {mkt_sl:.2f} ({mkt_sl_p:.0f}p)  TP: {mkt_tp:.2f} ({sign}{mkt_tp_p:.0f}p)\n"
                        f"  Lot: {lot_each}  Risk: ${risk_amt_z:.2f}  {ai_note}\n"
                        f"{'═'*60}"
                    )
                except Exception as e:
                    logger.error(f"Market order failed: {e}")
                continue

            # ── Limit: 1 ta order ────────────────────────────────────
            # lmt_sl / lmt_sl_p already set above (and possibly AI-adjusted)
            # TP1 — minimum 1.5:1 RR majburiy (SL*1.5 va 40 pipdan katta)
            min_tp_rr = round(lmt_sl_p * 2.0)   # 2.0:1 RR minimum
            min_tp_pips = max(60, min_tp_rr)     # absolute min 60 pip
            lmt_tp   = tp1  # swing-based TP
            lmt_tp_p = abs(lmt_tp - entry) / self.PIP
            if lmt_tp_p < min_tp_pips:
                lmt_tp   = round(entry + s * min_tp_pips * self.PIP, 2)
                lmt_tp_p = float(min_tp_pips)
            # RR < 2.0 bo'lsa bu zone ni o'tkazib yubor
            if lmt_sl_p > 0 and lmt_tp_p / lmt_sl_p < 1.95:
                logger.debug(
                    f"RR {lmt_tp_p:.0f}p/{lmt_sl_p:.0f}p = "
                    f"{lmt_tp_p/lmt_sl_p:.2f} < 2.0 — skip"
                )
                continue
            try:
                res = self.mt5.place_pending_order(
                    symbol=self.symbol, direction=want_dir,
                    limit_price=entry, sl=lmt_sl, tp=lmt_tp,
                    lot=lot_each, expiry_hours=8,
                )
                self._pending_zones[res.ticket] = {
                    "set_id": set_id, "direction": want_dir,
                    "entry": entry, "sl": lmt_sl, "tp": lmt_tp,
                    "tp1": tp1, "tp2": tp2, "tp3": tp3,
                    "tp_label": "TP1", "sl_pips": lmt_sl_p,
                    "zone_lo": zone_lo, "zone_hi": zone_hi,
                    "tf": zone_tf_now, "mode": mode,
                    "label": zone["label"], "placed_at": get_clock().now(),
                }
                placed_sets += 1
                self._last_order_at[want_dir] = get_clock().now()
                if _is_be_reentry and want_dir in self._be_reentry_candidates:
                    del self._be_reentry_candidates[want_dir]
                logger.success(
                    f"\n{'─'*60}\n"
                    f"  [{mode}] LIMIT | {want_dir.upper()} | {zone['label']} [{zone_tf_now}]{sb_tag}{htf_tag}\n"
                    f"  Entry: {entry:.2f}  SL: {lmt_sl:.2f} ({lmt_sl_p:.0f}p)\n"
                    f"  TP1: {tp1:.2f} ({tp1_p:.0f}p) | TP2: {tp2:.2f} | TP3: {tp3:.2f}\n"
                    f"  Lot: {lot_each}  Risk: {risk_pct*100:.1f}% ATRx{atr_multiplier:.2f}  {ai_note}\n"
                    f"{'─'*60}"
                )
                await tg.notify_order(
                    "LIMIT", want_dir, self.symbol,
                    entry, lmt_sl, tp1, lot_each, zone["label"], mode,
                )
            except Exception as e:
                logger.error(f"Limit order failed: {e}")

    # ── F0-4: News blackout pending cancel ────────────────────────

    def _cancel_all_pending_orders(self, reason: str = "NEWS_BLACKOUT") -> int:
        """
        OpenClaw (magic=20240101) magic'li barcha pending orderlarni bekor qilish.

        News blackout paytida ishlatilishi mo'ljallangan — pending'lar keng spread'da
        execute bo'lib ketmasligi uchun ulardan tozalanamiz.

        Args:
            reason: log uchun sabab string (NEWS_BLACKOUT, MANUAL, ...)

        Returns:
            Bekor qilingan orderlar soni.
        """
        try:
            all_pending = self.mt5.get_pending_orders(self.symbol)
        except Exception as e:
            logger.error(f"_cancel_all_pending_orders: get_pending_orders failed — {e}")
            return 0

        if not all_pending:
            return 0

        # Faqat OpenClaw magic'li orderlarni cancel qilamiz
        openclaw_pending = [
            o for o in all_pending if int(o.get("magic", 0)) == 20240101
        ]
        if not openclaw_pending:
            return 0

        cancelled = 0
        for order in openclaw_pending:
            ticket = int(order.get("ticket", 0))
            if not ticket:
                continue
            try:
                if self.mt5.cancel_pending_order(ticket):
                    logger.warning(
                        f"PENDING CANCELLED [ticket={ticket}, reason={reason}, "
                        f"price={order.get('price_open')}, symbol={order.get('symbol')}]"
                    )
                    # _pending_zones tracking mavjud bo'lsa, undan ham olib tashlaymiz
                    # (lekin _trade_meta — F0-1 ishi — pending'lar uchun ishlatilmaydi)
                    if ticket in self._pending_zones:
                        try:
                            del self._pending_zones[ticket]
                        except Exception:
                            pass
                    cancelled += 1
                else:
                    logger.error(f"cancel_pending_order({ticket}) returned False")
            except Exception as e:
                logger.error(f"cancel_pending_order({ticket}) exception: {e}")

        if cancelled:
            logger.warning(
                f"_cancel_all_pending_orders: {cancelled} order(s) cancelled, reason={reason}"
            )
        return cancelled

    # ── Manage pending zones (fill detect + expiry) ───────────────

    async def _manage_pending_zones(self):
        if not self._pending_zones:
            return

        mt5_pending  = {o["ticket"]: o for o in self.mt5.get_pending_orders(self.symbol)}
        positions    = self.mt5.get_open_positions()
        pos_by_ident = {p.get("identifier", 0): p for p in positions if isinstance(p, dict)}

        # Joriy narxni olish (invalidatsiya uchun)
        try:
            _tick_now = self.mt5.get_current_price(self.symbol)
            _price_now = (_tick_now["ask"] + _tick_now["bid"]) / 2
        except Exception:
            _price_now = 0.0

        for ticket in list(self._pending_zones.keys()):
            meta = self._pending_zones[ticket]

            if ticket in mt5_pending:
                age_h = (get_clock().now() - meta["placed_at"]).total_seconds() / 3600
                if age_h > 8:
                    try:
                        self.mt5.cancel_pending_order(ticket)
                    except Exception:
                        pass
                    del self._pending_zones[ticket]
                    logger.info(f"Pending #{ticket} expired (8h) — cancelled")
                    continue

                # Zone invalidatsiya: narx 80+ pip uzoqlashsa — zona buzilgan
                if _price_now > 0:
                    z_entry = meta.get("entry", 0)
                    z_dir   = meta.get("direction", "")
                    if z_entry > 0:
                        dist_p = (_price_now - z_entry) / self.PIP
                        # BUY LIMIT: narx pastga ketsa (entry dan 80+ pip pastga) — zona yo'q
                        # SELL LIMIT: narx yuqoriga ketsa (entry dan 80+ pip yuqoriga) — zona yo'q
                        invalidated = (
                            (z_dir == "buy"  and dist_p < -80) or
                            (z_dir == "sell" and dist_p >  80)
                        )
                        if invalidated:
                            try:
                                self.mt5.cancel_pending_order(ticket)
                            except Exception:
                                pass
                            del self._pending_zones[ticket]
                            logger.info(
                                f"Zone #{ticket} INVALIDATED: {z_dir.upper()} "
                                f"entry={z_entry:.2f} price={_price_now:.2f} "
                                f"dist={dist_p:.0f}p — cancelled"
                            )
                            continue
            else:
                filled = pos_by_ident.get(ticket)
                if filled:
                    pos_ticket = filled.get("ticket", ticket)
                    if pos_ticket not in self._trade_meta:
                        self._trade_meta[pos_ticket] = {
                            "set_id":    meta["set_id"],
                            "tp_label":  meta["tp_label"],
                            "tp":        meta["tp"],
                            "sl":        meta["sl"],
                            "sl_pips":   meta["sl_pips"],
                            "tp1":       meta.get("tp1", 0),
                            "tp2":       meta.get("tp2", 0),
                            "tp3":       meta.get("tp3", 0),
                            "entry":     float(filled.get("price_open", meta["entry"])),
                            "direction": meta["direction"],
                            "tf":        meta.get("tf", "H1"),
                            "be_done":   False,
                            "tp1_partial_done": False,
                            "tp2_partial_done": False,
                            "tp3_done":         False,
                            "pnl":       0,
                            "mode":      meta.get("mode", "FLOW"),
                            "label":     meta.get("label", "OB"),
                        }
                        save_trade_meta(self._trade_meta)  # F0-1
                        fill_price = float(filled.get("price_open", meta["entry"]))
                        logger.success(
                            f"FILLED → #{pos_ticket} {meta['direction'].upper()} "
                            f"@ {fill_price:.2f} | {meta['label']} | {meta['tp_label']}"
                        )
                        await tg.notify_order(
                            "FILLED", meta["direction"], self.symbol,
                            fill_price, meta["sl"], meta.get("tp1", meta["tp"]),
                            float(filled.get("volume", 0)), meta["label"],
                            meta.get("mode", "FLOW"),
                        )
                else:
                    logger.info(f"Pending #{ticket} cancelled/expired — removed")
                del self._pending_zones[ticket]

    # ── Manage open positions (BE + trail) ────────────────────────

    async def _manage_positions(self):
        _all_pos  = self.mt5.get_open_positions()
        positions = [p for p in _all_pos if isinstance(p, dict) and p.get("magic", 0) == 20240101]
        open_tickets = {p["ticket"] for p in positions}

        # Yopilgan pozitsiyalarni aniqlash → win/loss + used_zones + set cancel
        for ticket, meta in list(self._trade_meta.items()):
            if ticket not in open_tickets:
                entry_p    = meta.get("entry", 0)
                pnl        = meta.get("pnl", 0)
                set_id     = meta.get("set_id", "")
                exit_price = 0.0
                _history_found = False
                # Retry up to 3 times (1s apart) — MT5 history may lag by a few seconds
                for _attempt in range(3):
                    try:
                        history = self.mt5.get_closed_position(ticket)
                        if history is not None:
                            pnl        = float(history.get("profit", 0))
                            exit_price = float(history.get("price_close", 0))
                            _history_found = True
                            break
                    except Exception:
                        pass
                    if _attempt < 2:
                        await asyncio.sleep(1)
                # Fallback ONLY when history is completely unavailable (not when profit==0)
                if not _history_found and entry_p > 0:
                    direction_closed = meta.get("direction", "buy")
                    sl_price = meta.get("sl", 0)
                    tp1_price = meta.get("tp1", 0) or meta.get("tp", 0)
                    if sl_price > 0:
                        # Determine whether SL or TP was more likely hit
                        # by checking which is closer to current known last price
                        _sl_pips = abs(meta.get("sl_pips", 0))
                        if direction_closed == "buy" and sl_price < entry_p:
                            pnl = -_sl_pips * 0.1   # SL hit ≈ -$0.1/pip
                        elif direction_closed == "sell" and sl_price > entry_p:
                            pnl = -_sl_pips * 0.1
                closed_dir = meta.get("direction", "buy")
                if pnl > 0:
                    self._wins    += 1
                    result_str     = "WIN"
                    self._streak   = max(self._streak + 1, 1)
                    self._pause_until = None          # win — pause reset
                    self._last_order_at[closed_dir] = None  # win — cooldown reset
                elif pnl < 0:
                    self._losses  += 1
                    result_str     = "LOSS"
                    self._streak   = min(self._streak - 1, -1)
                    consec = max(0, -self._streak)
                    if consec >= 5 and self._pause_until is None:
                        from datetime import timedelta
                        self._pause_until = get_clock().now() + timedelta(hours=1)
                        logger.warning(
                            f"🚨 {consec} ketma-ket yo'qotish — 1 soat PAUZA "
                            f"({self._pause_until.strftime('%H:%M')} gacha)"
                        )
                else:
                    result_str     = "BE"
                    self._streak   = 0
                if entry_p > 0:
                    self._used_zones.add(round(entry_p, 1))
                total = self._wins + self._losses
                wr    = f"{self._wins/total*100:.0f}%" if total > 0 else "N/A"
                streak_str = f"+{self._streak}" if self._streak > 0 else str(self._streak)
                logger.info(
                    f"CLOSED #{ticket} → {result_str} ${pnl:.2f} | "
                    f"WR={wr} ({self._wins}W/{self._losses}L) streak={streak_str}"
                )
                await tg.notify_order(
                    result_str, meta.get("direction","buy"), self.symbol,
                    meta.get("entry",0), meta.get("sl",0), meta.get("tp",0),
                    0, meta.get("label","OB"), meta.get("mode","FLOW"), pnl,
                )
                try:
                    tp_entry = meta.get("entry", entry_p)
                    tp_price = meta.get("tp", tp_entry)
                    tp_pips  = abs(tp_price - tp_entry) / self.PIP if tp_entry else 0.0
                    self.learner.log_trade(
                        entry_type = meta.get("label", "OB"),
                        timeframe  = meta.get("tf", "H1"),
                        session    = get_current_session(),
                        direction  = meta.get("direction", "buy"),
                        sl_pips    = float(meta.get("sl_pips", 0)),
                        tp_pips    = round(tp_pips, 1),
                        result     = result_str.lower(),
                        pnl        = round(pnl, 2),
                        mode       = meta.get("mode", "FLOW"),
                        regime     = self._current_regime,
                    )
                except Exception as _le:
                    logger.warning(f"SelfLearner log_trade error: {_le}")

                # CSV export
                try:
                    _tp_entry = meta.get("entry", entry_p)
                    _tp_price = meta.get("tp", _tp_entry)
                    _tp_pips  = abs(_tp_price - _tp_entry) / self.PIP if _tp_entry else 0.0
                    log_trade_csv(
                        direction  = meta.get("direction", "buy"),
                        symbol     = self.symbol,
                        entry      = _tp_entry,
                        exit_price = exit_price,
                        sl         = meta.get("sl", 0),
                        tp1        = meta.get("tp1", 0) or meta.get("tp", 0),
                        lot        = 0.0,
                        pnl        = round(pnl, 2),
                        result     = result_str.lower(),
                        label      = meta.get("label", "OB"),
                        tf         = meta.get("tf", "H1"),
                        mode       = meta.get("mode", "FLOW"),
                        session    = get_current_session(),
                        regime     = self._current_regime,
                        sl_pips    = float(meta.get("sl_pips", 0)),
                        tp_pips    = round(_tp_pips, 1),
                    )
                except Exception as _ce:
                    logger.debug(f"CSV log error: {_ce}")

                # BE re-entry: setup hali validga o'xshasa qayta kirish
                if result_str == "BE":
                    _be_dir = meta.get("direction", "buy")
                    self._be_reentry_candidates[_be_dir] = {
                        "time":  get_clock().now(),
                        "entry": meta.get("entry", 0),
                        "label": meta.get("label", ""),
                    }
                    logger.info(f"BE re-entry candidate: {_be_dir.upper()} @ {meta.get('entry',0):.2f}")

                del self._trade_meta[ticket]
                save_trade_meta(self._trade_meta)  # F0-1

                if set_id:
                    if result_str in ("WIN", "BE"):
                        # TP yutdi → ochiq sibling pozitsiyalarni BE ga ko'chir
                        _sib_modified = False
                        for sib_t, sib_m in list(self._trade_meta.items()):
                            if sib_m.get("set_id") == set_id and not sib_m.get("be_done"):
                                sib_entry = sib_m.get("entry", 0)
                                if sib_entry > 0:
                                    try:
                                        self.mt5.modify_position(sib_t, sl=sib_entry)
                                        sib_m["be_done"] = True
                                        _sib_modified = True
                                        logger.info(
                                            f"Set BE: #{sib_t} SL→{sib_entry:.2f} "
                                            f"(sibling #{ticket} yutdi)"
                                        )
                                    except Exception:
                                        pass
                        if _sib_modified:
                            save_trade_meta(self._trade_meta)  # F0-1
                        # Pending sibling'larni bekor qil
                        for pend_ticket in list(self._pending_zones.keys()):
                            if self._pending_zones[pend_ticket].get("set_id") == set_id:
                                try:
                                    self.mt5.cancel_pending_order(pend_ticket)
                                    logger.info(f"Set WIN: pending #{pend_ticket} bekor")
                                except Exception:
                                    pass
                                del self._pending_zones[pend_ticket]
                    else:
                        # LOSS → barcha pending sibling'larni bekor qil
                        for pend_ticket in list(self._pending_zones.keys()):
                            if self._pending_zones[pend_ticket].get("set_id") == set_id:
                                try:
                                    self.mt5.cancel_pending_order(pend_ticket)
                                    logger.info(f"Set LOSS: pending #{pend_ticket} bekor")
                                except Exception:
                                    pass
                                del self._pending_zones[pend_ticket]

        if not positions:
            return

        try:
            tick  = self.mt5.get_current_price(self.symbol)
            price = (tick["ask"] + tick["bid"]) / 2
        except Exception:
            return

        for pos in positions:
            if not isinstance(pos, dict):
                continue
            ticket  = pos.get("ticket", 0)
            meta    = self._trade_meta.get(ticket, {})
            entry   = float(pos.get("price_open", meta.get("entry", 0)))
            sl      = float(pos.get("sl",   meta.get("sl",    0)))
            tp      = float(pos.get("tp",   meta.get("tp",    0)))
            is_buy  = pos.get("type", -1) == 0

            if entry == 0 or sl == 0:
                continue

            sl_dist = abs(entry - sl)
            if sl_dist == 0:
                continue

            profit_dist = (price - entry) if is_buy else (entry - price)

            # ── TP1: 40% yopish ──────────────────────────────────
            tp1 = float(meta.get("tp1", 0) or 0)
            if tp1 > 0 and not meta.get("tp1_partial_done", False):
                tp1_hit = (price >= tp1) if is_buy else (price <= tp1)
                if tp1_hit:
                    try:
                        self.mt5.partial_close(ticket, 40)
                        meta["tp1_partial_done"] = True
                        save_trade_meta(self._trade_meta)  # F0-1
                        logger.success(f"✅ TP1 40% #{ticket}: price={price:.2f} TP1={tp1:.2f}")
                    except Exception as e:
                        logger.debug(f"TP1 partial failed: {e}")

            # ── TP2: qolganining 50% yopish + SL → BE+10 ─────────
            tp2 = float(meta.get("tp2", 0) or 0)
            if tp2 > 0 and not meta.get("tp2_partial_done", False) and meta.get("tp1_partial_done", False):
                tp2_hit = (price >= tp2) if is_buy else (price <= tp2)
                if tp2_hit:
                    try:
                        self.mt5.partial_close(ticket, 50)
                        meta["tp2_partial_done"] = True
                        save_trade_meta(self._trade_meta)  # F0-1
                        # SL → entry + 10 pip (kichik foyda kafolat)
                        be_plus = round(
                            entry + 10 * self.PIP if is_buy else entry - 10 * self.PIP, 2
                        )
                        current_sl_now = float(pos.get("sl", sl))
                        if (is_buy and be_plus > current_sl_now) or \
                           (not is_buy and be_plus < current_sl_now):
                            self.mt5.modify_position(ticket, sl=be_plus)
                            logger.success(
                                f"✅ TP2 50% #{ticket}: price={price:.2f} TP2={tp2:.2f} "
                                f"→ SL={be_plus:.2f} (BE+10p)"
                            )
                        else:
                            logger.success(f"✅ TP2 50% #{ticket}: price={price:.2f}")
                    except Exception as e:
                        logger.debug(f"TP2 partial failed: {e}")

            # ── TP3: qolganini to'liq yopish ─────────────────────
            tp3 = float(meta.get("tp3", 0) or 0)
            if tp3 > 0 and not meta.get("tp3_done", False) and meta.get("tp2_partial_done", False):
                tp3_hit = (price >= tp3) if is_buy else (price <= tp3)
                if tp3_hit:
                    try:
                        self.mt5.partial_close(ticket, 100)
                        meta["tp3_done"] = True
                        save_trade_meta(self._trade_meta)  # F0-1
                        logger.success(f"🎯 TP3 100% #{ticket}: price={price:.2f} TP3={tp3:.2f}")
                    except Exception as e:
                        logger.debug(f"TP3 close failed: {e}")

            # ── Step trailing: har 10 pip foydada SL 10 pip yaqinlashadi ──
            # +10p → SL (orig-10)p | +20p → SL (orig-20)p | +40p → SL=entry (BE)
            orig_sl_pips = float(meta.get("sl_pips", sl_dist / self.PIP))
            profit_pips  = profit_dist / self.PIP
            STEP         = 10   # har 10 pip
            steps        = int(profit_pips / STEP)   # necha qadam yurdi
            if steps > 0:
                # Yangi SL masofasi: orig - steps*10, minimum 0 (BE)
                new_sl_dist_pips = max(0.0, orig_sl_pips - steps * STEP)
                if is_buy:
                    new_sl = round(entry - new_sl_dist_pips * self.PIP, 2)
                else:
                    new_sl = round(entry + new_sl_dist_pips * self.PIP, 2)

                current_sl = float(pos.get("sl", sl))
                better = (new_sl > current_sl) if is_buy else (new_sl < current_sl)
                if better:
                    try:
                        self.mt5.modify_position(ticket, sl=new_sl)
                        be_tag = " → BREAKEVEN" if new_sl_dist_pips == 0 else ""
                        logger.info(
                            f"TRAIL #{ticket}: +{profit_pips:.0f}p "
                            f"SL dist {orig_sl_pips:.0f}p→{new_sl_dist_pips:.0f}p "
                            f"SL={new_sl:.2f}{be_tag}"
                        )
                        if new_sl_dist_pips == 0:
                            meta["be_done"] = True
                            save_trade_meta(self._trade_meta)  # F0-1
                    except Exception as e:
                        logger.debug(f"Trail modify failed: {e}")

            # Time-based exit: 6 soat ochiq + foyda 10 pipdan kam → yopish
            open_time = pos.get("time", None)
            if open_time:
                try:
                    import time as _t
                    age_h = (_t.time() - float(open_time)) / 3600
                    if age_h >= 6:
                        profit_pips = profit_dist / self.PIP
                        if profit_pips < 10:
                            logger.info(
                                f"Time exit #{ticket}: {age_h:.1f}h ochiq, "
                                f"foyda={profit_pips:.1f}p < 10p → yopish"
                            )
                            self.mt5.partial_close(ticket, 100)
                except Exception:
                    pass

    # ── Helpers ───────────────────────────────────────────────────

    def _candles(self, tf: str, count: int):
        try:
            return self.mt5.get_candles(self.symbol, tf, count)
        except Exception:
            return None

    def _calc_atr(self, candles, period: int = 14) -> float:
        if candles is None or len(candles) < period:
            return 1.0
        h  = candles["high"].astype(float)
        l  = candles["low"].astype(float)
        c  = candles["close"].astype(float)
        import pandas as pd
        tr = pd.concat([h-l, (h-c.shift(1)).abs(), (l-c.shift(1)).abs()], axis=1).max(axis=1)
        v  = tr.rolling(period, min_periods=1).mean().iloc[-1]
        import numpy as np
        return float(v) if not np.isnan(v) else 1.0

    def _in_kill_zone(self) -> bool:
        now = get_clock().now()
        cur = now.hour * 60 + now.minute
        return (
            0*60  <= cur <= 3*60   or   # Asia KZ     (00:00-03:00 UTC)
            8*60  <= cur <= 11*60  or   # London KZ   (08:00-11:00 UTC)
            13*60 <= cur <= 16*60       # New York KZ (13:00-16:00 UTC)
        )

    def _in_silver_bullet(self) -> bool:
        """ICT Silver Bullet: London 10-11 UTC, NY 15-16 UTC — eng yuqori ishonchlilik."""
        now = get_clock().now()
        cur = now.hour * 60 + now.minute
        return (
            10*60 <= cur <= 11*60 or   # London Silver Bullet
            15*60 <= cur <= 16*60      # NY Silver Bullet
        )

    def _htf_confluence(self, direction: str, ict_map: dict) -> int:
        """Nechta HTF (D1/H4/H1/M30) shu yo'nalishni tasdiqlaydi (0–4)."""
        count = 0
        want = "bullish" if direction == "buy" else "bearish"
        for tf in ("D1", "H4", "H1", "M30"):
            ict = ict_map.get(tf)
            if ict is None:
                continue
            trend = ict.structure.get("trend", "sideways")
            bos   = ict.structure.get("bos", 0)
            choch = ict.structure.get("choch", False)
            if trend == want:
                count += 1
            elif bos > 0 and choch:
                count += 1  # structural break confirms direction
        return count

    def _has_liquidity_sweep(self, ict_map: dict) -> bool:
        """H4 → M5 oralig'ida swept liquidity pool, stop hunt yoki judas swing."""
        for tf in ("H4", "H1", "M30", "M15", "M5"):
            ict = ict_map.get(tf)
            if ict is None:
                continue
            for lz in ict.liquidity_zones:
                if lz.get("swept"):
                    return True
            if ict.structure.get("stop_hunt") or ict.structure.get("judas"):
                return True
        return False

    def _volatility_ok(self, h1_candles) -> bool:
        """H1 ATR juda past bo'lsa — trade yo'q (dead market)."""
        atr = self._calc_atr(h1_candles)
        return atr >= 0.25   # XAUUSD H1 ATR minimum 0.25

    def _sniper_score(
        self, htf_conf: int, liq_swept: bool, impulse_ok: bool,
        ob_fvg_ok: bool, in_kz: bool, struct_ok: bool,
    ) -> int:
        """SNIPER score 0-10. ≥7 kerak."""
        s = 0
        if htf_conf >= 2:  s += 2   # D1+H4 alignment
        if liq_swept:      s += 2   # liquidity sweep
        if impulse_ok:     s += 2   # strong impulse/displacement
        if ob_fvg_ok:      s += 2   # OB/FVG reaction
        if in_kz:          s += 1   # kill zone volatility
        if struct_ok:      s += 1   # BOS/CHoCH confirmed
        return s

    def _flow_score(
        self, liq_swept: bool, micro_disp: bool,
        zone_ok: bool, struct_break: bool,
    ) -> int:
        """FLOW score 0-4. ≥3 kerak."""
        s = 0
        if liq_swept:    s += 1
        if micro_disp:   s += 1
        if zone_ok:      s += 1
        if struct_break: s += 1
        return s

    def _displacement_ok(self, direction: str, ict_m1, ict_m5, ict_m15) -> bool:
        for ict in [ict_m1, ict_m5, ict_m15]:
            if ict is None:
                continue
            disp = ict.structure.get("displacement", False)
            disp_dir = ict.structure.get("displacement_dir", "none")
            want = "bullish" if direction == "buy" else "bearish"
            if disp and disp_dir == want:
                return True
        return False

    def _ote_fib_zones(
        self, want_dir: str, candles, tf: str, atr: float, wt: float, price: float,
    ) -> list:
        """Dinamik OTE: so'nggi swing high/low dan 0.705 fib entry (0.62–0.786 zona)."""
        if candles is None or len(candles) < 30:
            return []
        import numpy as np
        hi = candles["high"].astype(float).values
        lo = candles["low"].astype(float).values
        n  = min(60, len(hi))

        swing_hi = float(np.max(hi[-n:]))
        swing_lo = float(np.min(lo[-n:]))
        swing    = swing_hi - swing_lo
        if swing < 8 * self.PIP:   # juda kichik swing — skip
            return []

        sl_min = self.SL_MIN_PIPS
        tp_max = self.TP_MAX_PIPS
        MAX_DIST = 200 * self.PIP
        zones = []

        if want_dir == "buy":
            # Bullish impulse: lo→hi bo'ldi, narx endi retracement'da
            ote_hi  = round(swing_hi - swing * 0.618, 2)
            ote_lo  = round(swing_hi - swing * 0.786, 2)
            entry_p = round(swing_hi - swing * 0.705, 2)   # 70.5% OTE
            if price < lo[-1] - MAX_DIST or price > hi[-1] + MAX_DIST:
                return []
            sl = round(ote_lo - atr * 0.15, 2)
            sl_p = (entry_p - sl) / self.PIP
            if sl_p < sl_min:
                sl = round(entry_p - sl_min * self.PIP, 2)
                sl_p = sl_min
            if sl_p > self.SL_MAX_PIPS:
                return []
            tp1, tp2, tp3 = self._rr_targets(entry_p, "buy", sl_p, tp_max)
            zones.append({
                "direction": "buy", "entry": entry_p, "sl": sl,
                "zone_lo": ote_lo, "zone_hi": ote_hi,
                "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "label": f"{tf}_OTE", "weight": wt * 4.5, "tf": tf,
            })

        elif want_dir == "sell":
            # Bearish impulse: hi→lo bo'ldi, narx endi retracement'da
            ote_lo  = round(swing_lo + swing * 0.618, 2)
            ote_hi  = round(swing_lo + swing * 0.786, 2)
            entry_p = round(swing_lo + swing * 0.705, 2)
            if price < lo[-1] - MAX_DIST or price > hi[-1] + MAX_DIST:
                return []
            sl = round(ote_hi + atr * 0.15, 2)
            sl_p = (sl - entry_p) / self.PIP
            if sl_p < sl_min:
                sl = round(entry_p + sl_min * self.PIP, 2)
                sl_p = sl_min
            if sl_p > self.SL_MAX_PIPS:
                return []
            tp1, tp2, tp3 = self._rr_targets(entry_p, "sell", sl_p, tp_max)
            zones.append({
                "direction": "sell", "entry": entry_p, "sl": sl,
                "zone_lo": ote_lo, "zone_hi": ote_hi,
                "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "label": f"{tf}_OTE", "weight": wt * 4.5, "tf": tf,
            })

        return zones

    def _session_open_zones(self, direction: str, price: float, atr: float) -> list:
        """Midnight / London / NY Open narxlari zona sifatida.
        Narx bu levelga qaytib kelsa → limit entry."""
        zones = []
        levels = [
            ("MidnightOpen", self._midnight_open, 32.0),
            ("LondonOpen",   self._london_open,   30.0),
            ("NYOpen",       self._ny_open,        30.0),
        ]
        for label, level, base_wt in levels:
            if level <= 0:
                continue
            dist_p = abs(price - level) / self.PIP
            if dist_p > 80:   # 80 pipdan uzoqsa — hozir relevantmas
                continue
            if dist_p < 1:    # narx aynan shu levelda — allaqachon tegdi
                continue

            if direction == "buy" and price < level:
                # Narx open dan pastda → open level'ga qaytishi mumkin (buy)
                entry_p = round(level - atr * 0.05, 2)
                sl      = round(level - atr * 0.40, 2)
                sl_p    = (entry_p - sl) / self.PIP
                if sl_p < self.SL_MIN_PIPS:
                    sl   = round(entry_p - self.SL_MIN_PIPS * self.PIP, 2)
                    sl_p = self.SL_MIN_PIPS
                if sl_p > self.SL_MAX_PIPS:
                    continue
                tp1, tp2, tp3 = self._rr_targets(entry_p, "buy", sl_p)
                zones.append({
                    "direction": "buy", "entry": entry_p, "sl": sl,
                    "zone_lo": level - atr * 0.1, "zone_hi": level + atr * 0.05,
                    "tp1": tp1, "tp2": tp2, "tp3": tp3,
                    "label": f"{label}_buy", "weight": base_wt, "tf": "H1",
                })

            elif direction == "sell" and price > level:
                # Narx open dan yuqorida → open level'ga qaytishi mumkin (sell)
                entry_p = round(level + atr * 0.05, 2)
                sl      = round(level + atr * 0.40, 2)
                sl_p    = (sl - entry_p) / self.PIP
                if sl_p < self.SL_MIN_PIPS:
                    sl   = round(entry_p + self.SL_MIN_PIPS * self.PIP, 2)
                    sl_p = self.SL_MIN_PIPS
                if sl_p > self.SL_MAX_PIPS:
                    continue
                tp1, tp2, tp3 = self._rr_targets(entry_p, "sell", sl_p)
                zones.append({
                    "direction": "sell", "entry": entry_p, "sl": sl,
                    "zone_lo": level - atr * 0.05, "zone_hi": level + atr * 0.1,
                    "tp1": tp1, "tp2": tp2, "tp3": tp3,
                    "label": f"{label}_sell", "weight": base_wt, "tf": "H1",
                })
        return zones

    def _nwog_zones(self, direction: str, d1_candles, price: float, atr: float) -> list:
        """NWOG (New Week Opening Gap): Juma yopilishi vs Dushanba ochilishi orasidagi gap.
        Gap fills juda yuqori ehtimollik (~85%). XAUUSD da Weekend gap bo'lsa ishlatiladi."""
        if d1_candles is None or len(d1_candles) < 3:
            return []
        today_wd = get_clock().now().weekday()
        if today_wd > 2:   # Faqat Dushanba/Seshanba/Chorshanba relevantmas → Dush+Sesh (0,1)
            pass           # Hafta davomida ham gap dolg'alashi mumkin
        try:
            prev_close  = float(d1_candles["close"].iloc[-2])
            today_open  = float(d1_candles["open"].iloc[-1])
        except Exception:
            return []
        gap     = today_open - prev_close
        gap_p   = abs(gap) / self.PIP
        if gap_p < 8:     # 8 pipdan kichik gap — ahamiyatsiz
            return []

        zones = []
        if direction == "buy" and gap < 0:
            # Narx pastga ochildi (bearish gap) → gap fill uchun narx ko'tarilishi mumkin
            if price > today_open + gap_p * 2 * self.PIP:
                return []  # allaqachon otib ketdi
            entry_p = round(today_open + abs(gap) * 0.15, 2)
            sl      = round(today_open - atr * 0.30, 2)
            sl_p    = (entry_p - sl) / self.PIP
            if sl_p < self.SL_MIN_PIPS:
                sl   = round(entry_p - self.SL_MIN_PIPS * self.PIP, 2)
                sl_p = self.SL_MIN_PIPS
            if sl_p > self.SL_MAX_PIPS:
                return []
            tp1, tp2, tp3 = self._rr_targets(entry_p, "buy", sl_p)
            tp1 = min(tp1, prev_close)  # TP1 gap to'ldirish
            zones.append({
                "direction": "buy", "entry": entry_p, "sl": sl,
                "zone_lo": today_open - atr * 0.1, "zone_hi": prev_close,
                "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "label": f"NWOG_buy_{gap_p:.0f}p", "weight": 45.0, "tf": "D1",
            })

        elif direction == "sell" and gap > 0:
            # Narx yuqoriga ochildi (bullish gap) → gap fill uchun narx tushishi mumkin
            if price < today_open - gap_p * 2 * self.PIP:
                return []
            entry_p = round(today_open - abs(gap) * 0.15, 2)
            sl      = round(today_open + atr * 0.30, 2)
            sl_p    = (sl - entry_p) / self.PIP
            if sl_p < self.SL_MIN_PIPS:
                sl   = round(entry_p + self.SL_MIN_PIPS * self.PIP, 2)
                sl_p = self.SL_MIN_PIPS
            if sl_p > self.SL_MAX_PIPS:
                return []
            tp1, tp2, tp3 = self._rr_targets(entry_p, "sell", sl_p)
            tp1 = max(tp1, prev_close)  # TP1 gap to'ldirish
            zones.append({
                "direction": "sell", "entry": entry_p, "sl": sl,
                "zone_lo": prev_close, "zone_hi": today_open + atr * 0.1,
                "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "label": f"NWOG_sell_{gap_p:.0f}p", "weight": 45.0, "tf": "D1",
            })

        return zones

    def _volume_boost(self, candles) -> float:
        """So'nggi sham hajmi o'rtachadan 1.5× katta bo'lsa → zone weight ×1.4."""
        if candles is None or len(candles) < 20:
            return 1.0
        import numpy as np
        vol = candles["tick_volume"].astype(float).values
        avg = float(np.mean(vol[-20:]))
        last = float(vol[-1])
        if avg > 0 and last >= avg * 1.5:
            return 1.4
        return 1.0

    def _detect_impulse(self, m1_candles) -> str:
        """Kuchli M1 impulse yo'nalishini aniqlash ('buy', 'sell', 'none').
        2.5×ATR harakat + shamlar yo'nalishiga mos yopilish."""
        if m1_candles is None or len(m1_candles) < 5:
            return "none"
        atr = self._calc_atr(m1_candles)
        if atr <= 0:
            return "none"
        last_hi    = float(m1_candles["high"].astype(float).iloc[-3:].max())
        last_lo    = float(m1_candles["low"].astype(float).iloc[-3:].min())
        last_close = float(m1_candles["close"].astype(float).iloc[-1])
        first_open = float(m1_candles["open"].astype(float).iloc[-3])
        move = last_hi - last_lo
        if move < atr * 2.5:
            return "none"
        if last_close > first_open and (last_hi - last_close) < move * 0.35:
            return "buy"
        if last_close < first_open and (last_close - last_lo) < move * 0.35:
            return "sell"
        return "none"

    def _crt_zones(
        self, direction: str, h1_candles, m15_candles,
        price: float, atr: float,
    ) -> list:
        """CRT (Candle Range Theory): oldingi H1 sham rangini sweep → qaytish.

        Logika:
          SELL CRT — recent M15 high, H1 high dan oshdi (BSL sweep), narx qaytib tushdi
          BUY  CRT — recent M15 low,  H1 low  dan pastga tushdi (SSL sweep), narx qaytib chiqdi
        Entry zona rangning 20% ichida; SL sweep dan narida.
        """
        if h1_candles is None or len(h1_candles) < 4:
            return []
        zones = []
        m15_hi = float(m15_candles["high"].astype(float).iloc[-8:].max()) if m15_candles is not None else price
        m15_lo = float(m15_candles["low"].astype(float).iloc[-8:].min())  if m15_candles is not None else price

        for i in range(-4, -1):
            try:
                crt_hi  = float(h1_candles["high"].iloc[i])
                crt_lo  = float(h1_candles["low"].iloc[i])
            except Exception:
                continue
            crt_rng = crt_hi - crt_lo
            if crt_rng < 10 * self.PIP:
                continue

            if direction == "buy":
                # SSL sweep: M15 low crt_lo dan pastga tushgan, hozir narx crt_lo ustida
                if not (m15_lo < crt_lo and price >= crt_lo):
                    continue
                entry_p = round(crt_lo + crt_rng * 0.20, 2)
                sl      = round(crt_lo - atr * 0.25, 2)
                sl_p    = (entry_p - sl) / self.PIP
                if sl_p < self.SL_MIN_PIPS:
                    sl   = round(entry_p - self.SL_MIN_PIPS * self.PIP, 2)
                    sl_p = self.SL_MIN_PIPS
                if sl_p > self.SL_MAX_PIPS:
                    continue
                tp1, tp2, tp3 = self._rr_targets(entry_p, "buy", sl_p)
                zones.append({
                    "direction": "buy", "entry": entry_p, "sl": sl,
                    "zone_lo": crt_lo, "zone_hi": crt_lo + crt_rng * 0.5,
                    "tp1": tp1, "tp2": tp2, "tp3": tp3,
                    "label": "H1_CRT_buy", "weight": 35.0, "tf": "H1",
                })

            elif direction == "sell":
                # BSL sweep: M15 high crt_hi dan yuqoriga chiqqan, hozir narx crt_hi ostida
                if not (m15_hi > crt_hi and price <= crt_hi):
                    continue
                entry_p = round(crt_hi - crt_rng * 0.20, 2)
                sl      = round(crt_hi + atr * 0.25, 2)
                sl_p    = (sl - entry_p) / self.PIP
                if sl_p < self.SL_MIN_PIPS:
                    sl   = round(entry_p + self.SL_MIN_PIPS * self.PIP, 2)
                    sl_p = self.SL_MIN_PIPS
                if sl_p > self.SL_MAX_PIPS:
                    continue
                tp1, tp2, tp3 = self._rr_targets(entry_p, "sell", sl_p)
                zones.append({
                    "direction": "sell", "entry": entry_p, "sl": sl,
                    "zone_lo": crt_hi - crt_rng * 0.5, "zone_hi": crt_hi,
                    "tp1": tp1, "tp2": tp2, "tp3": tp3,
                    "label": "H1_CRT_sell", "weight": 35.0, "tf": "H1",
                })

        return zones
