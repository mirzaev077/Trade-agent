"""
F3: TraderCommands — Telegram bot inbound komandalari uchun handlers.

Ishlatish:
    from .telegram_bot import register_command
    from .telegram_commands import TraderCommands

    cmds = TraderCommands(agent=trader_agent)
    cmds.register_all()
    # Endi /status, /positions, /pause, /resume Telegram'dan ishlaydi.

Har handler matn (Telegram javob) qaytaradi. Telegram_bot.dispatch_command
auth + parsing qiladi, biz faqat formatlashga e'tibor beramiz.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from . import telegram_bot as _tg

# Manual pause uchun marker — `_pause_until` ga maxsus uzoq sana yozamiz.
# Existing 5-loss auto-pause 1 soat berishadi; bu farqlanish kerak.
_MANUAL_PAUSE_HORIZON = timedelta(days=3650)  # 10 yil


class TraderCommands:
    """4 ta /status, /positions, /pause, /resume uchun handler hub.

    :param agent: TraderAgent (yoki shu interfeysga mos test stub):
                  • `mt5` — MT5Connector (refresh_account, get_open_positions)
                  • `symbol` — str
                  • `_mode` — "FLOW" | "SNIPER"
                  • `_pause_until` — datetime | None (yozamiz/o'qiymiz)
                  • `health_state` — Optional[HealthState]
                  • `_starting_balance` — float
                  • `_wins`, `_losses` — int (sessiya boshidan)
                  • `_pending_zones` — dict (limit orders count)
    """

    def __init__(self, agent: Any) -> None:
        self.agent = agent

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _fmt_duration(seconds: float) -> str:
        if seconds < 60:
            return f"{seconds:.0f}s"
        m, s = divmod(int(seconds), 60)
        if m < 60:
            return f"{m}m {s}s"
        h, m = divmod(m, 60)
        if h < 24:
            return f"{h}h {m}m"
        d, h = divmod(h, 24)
        return f"{d}d {h}h"

    def _is_paused(self) -> tuple[bool, str | None]:
        """Hozir pause'da? Manual (uzoq future) yoki auto (qisqa future) farqlash."""
        pu = getattr(self.agent, "_pause_until", None)
        if pu is None:
            return False, None
        now = self._now()
        if now >= pu:
            return False, None
        delta = pu - now
        kind = "MANUAL" if delta > timedelta(days=30) else "AUTO"
        until_str = pu.strftime("%Y-%m-%d %H:%M UTC") if kind == "AUTO" else "manual"
        return True, f"{kind} until {until_str}"

    # ── Command: /status ──────────────────────────────────────────────────────

    def cmd_status(self, args: str) -> str:
        a = self.agent
        symbol = getattr(a, "symbol", "?")
        mode = getattr(a, "_mode", "?")

        # Balance / equity
        balance = equity = 0.0
        try:
            acc = a.mt5.refresh_account() if hasattr(a, "mt5") else {}
            if isinstance(acc, dict):
                balance = float(acc.get("balance", 0.0))
                equity = float(acc.get("equity", balance))
        except Exception:  # noqa: BLE001
            pass

        # Daily PnL %
        start_bal = float(getattr(a, "_starting_balance", 0.0) or 0.0)
        daily_pnl_pct = 0.0
        if start_bal > 0:
            daily_pnl_pct = (equity - start_bal) / start_bal * 100.0

        # Positions / pending
        try:
            n_open = len(a.mt5.get_open_positions() or [])
        except Exception:  # noqa: BLE001
            n_open = 0
        n_pending = len(getattr(a, "_pending_zones", {}) or {})

        # WR
        wins = int(getattr(a, "_wins", 0) or 0)
        losses = int(getattr(a, "_losses", 0) or 0)
        wr_str = f"{wins/(wins+losses)*100:.0f}%" if (wins + losses) else "n/a"

        # Uptime
        hs = getattr(a, "health_state", None)
        uptime_str = "?"
        if hs is not None and getattr(hs, "started_at", None) is not None:
            uptime_str = self._fmt_duration(
                (self._now() - hs.started_at).total_seconds()
            )

        paused, pause_desc = self._is_paused()
        pause_line = f"⏸ PAUSED ({pause_desc})" if paused else "▶️ RUNNING"

        return (
            f"📊 <b>{symbol}</b> [{mode}] {pause_line}\n"
            f"Balance: ${balance:.2f} | Equity: ${equity:.2f}\n"
            f"Daily PnL: {'+'if daily_pnl_pct>=0 else ''}{daily_pnl_pct:.2f}%\n"
            f"Positions: {n_open} | Pending: {n_pending}\n"
            f"Sessiya WR: {wr_str} ({wins}W/{losses}L)\n"
            f"Uptime: {uptime_str}"
        )

    # ── Command: /positions ───────────────────────────────────────────────────

    def cmd_positions(self, args: str) -> str:
        a = self.agent
        try:
            positions = a.mt5.get_open_positions() or []
        except Exception as e:  # noqa: BLE001
            return f"⚠️ MT5'dan pozitsiyalarni olishda xato: {e}"

        if not positions:
            return "📭 Ochiq pozitsiya yo'q."

        lines: list[str] = [f"📊 <b>Ochiq pozitsiyalar ({len(positions)})</b>"]
        for p in positions[:10]:  # max 10
            # MT5 position dict / object — universal getter
            def _g(key: str, default: Any = None) -> Any:
                if isinstance(p, dict):
                    return p.get(key, default)
                return getattr(p, key, default)

            ticket = _g("ticket", "?")
            symbol = _g("symbol", "?")
            pos_type = _g("type", "?")
            dir_str = "BUY" if str(pos_type).lower() in ("0", "buy") else "SELL"
            dir_icon = "🟢" if dir_str == "BUY" else "🔴"
            volume = float(_g("volume", 0.0) or 0.0)
            price_open = float(_g("price_open", 0.0) or 0.0)
            sl = float(_g("sl", 0.0) or 0.0)
            tp = float(_g("tp", 0.0) or 0.0)
            profit = float(_g("profit", 0.0) or 0.0)
            sl_str = f"{sl:.2f}" if sl else "—"
            tp_str = f"{tp:.2f}" if tp else "—"
            lines.append(
                f"#{ticket} {dir_icon} {dir_str} {symbol} {volume}lot\n"
                f"  entry {price_open:.2f} | SL {sl_str} | TP {tp_str}\n"
                f"  PnL: {'+'if profit>=0 else ''}{profit:.2f}$"
            )
        if len(positions) > 10:
            lines.append(f"… va yana {len(positions) - 10} ta")
        return "\n".join(lines)

    # ── Command: /pause ───────────────────────────────────────────────────────

    def cmd_pause(self, args: str) -> str:
        a = self.agent
        already_paused, desc = self._is_paused()
        if already_paused and "MANUAL" in (desc or ""):
            return f"⏸ Allaqachon manual pause'da ({desc})"
        a._pause_until = self._now() + _MANUAL_PAUSE_HORIZON
        return (
            "⏸ <b>MANUAL PAUSE</b>\n"
            "Yangi orderlar ochilmaydi. Mavjud pozitsiyalar boshqarilaveradi.\n"
            "Davom etish uchun: /resume"
        )

    # ── Command: /resume ──────────────────────────────────────────────────────

    def cmd_resume(self, args: str) -> str:
        a = self.agent
        paused, desc = self._is_paused()
        if not paused:
            return "▶️ Pause'da emas — allaqachon ishlamoqda."
        a._pause_until = None
        prefix = "▶️ <b>RESUMED</b>"
        if desc and "AUTO" in desc:
            prefix += f"\n<i>Eslatma: avval auto-pause faol edi ({desc})</i>"
        return prefix

    # ── Registration ──────────────────────────────────────────────────────────

    def register_all(self, bot_module: Any = _tg) -> None:
        """4 ta komandani telegram_bot ga ulaydi."""
        bot_module.register_command("status", self.cmd_status)
        bot_module.register_command("positions", self.cmd_positions)
        bot_module.register_command("pause", self.cmd_pause)
        bot_module.register_command("resume", self.cmd_resume)
