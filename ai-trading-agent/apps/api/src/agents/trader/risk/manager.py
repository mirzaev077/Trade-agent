import pandas as pd
import numpy as np
from datetime import datetime, date
from ..models.config import RiskConfig
from ..models.signals import TradePermission


class RiskManagement:
    def __init__(self, config: RiskConfig = None):
        self.cfg = config or RiskConfig()
        self._today_trades: list = []
        self._daily_open_count: int = 0
        self._count_date: date = date.today()

    def _reset_if_new_day(self):
        today = date.today()
        if today != self._count_date:
            self._daily_open_count = 0
            self._today_trades = []
            self._count_date = today

    def record_trade_opened(self):
        self._reset_if_new_day()
        self._daily_open_count += 1

    @property
    def daily_trades_used(self) -> int:
        self._reset_if_new_day()
        return self._daily_open_count

    def calculate_position_size(
        self,
        account_balance: float,
        entry: float,
        sl: float,
        symbol_info: dict,
    ) -> float:
        risk_amount = account_balance * (self.cfg.risk_per_trade / 100)
        sl_distance = abs(entry - sl)

        point = symbol_info.get("point", 0.01)
        tick_value = symbol_info.get("trade_tick_value", 1.0)

        if sl_distance <= 0 or point <= 0:
            return symbol_info.get("volume_min", 0.01)

        sl_points = sl_distance / point
        lot_size = risk_amount / (sl_points * tick_value)

        vol_min = symbol_info.get("volume_min", 0.01)
        vol_max = symbol_info.get("volume_max", 100.0)
        vol_step = symbol_info.get("volume_step", 0.01)

        lot_size = max(vol_min, min(lot_size, vol_max))
        lot_size = round(round(lot_size / vol_step) * vol_step, 2)

        return lot_size

    def check_trade_allowed(
        self,
        account_info: dict,
        open_trades: list,
        signal,
    ) -> TradePermission:
        checks = {}

        # 1. Daily loss
        daily_pnl = sum(t.get("pnl", 0) for t in self._today_trades if t.get("pnl", 0) < 0)
        balance = account_info.get("balance", 10000)
        daily_risk_used = abs(daily_pnl) / balance * 100 if balance > 0 else 0
        checks["daily_limit"] = daily_risk_used < self.cfg.daily_max_risk

        # 2. Max positions
        checks["max_positions"] = len(open_trades) < self.cfg.max_positions

        # 2b. Daily trade cap
        self._reset_if_new_day()
        checks["daily_trade_cap"] = self._daily_open_count < self.cfg.max_trades_per_day

        # 3. Drawdown
        equity = account_info.get("equity", balance)
        current_dd = (balance - equity) / balance * 100 if balance > 0 else 0
        checks["drawdown"] = current_dd < self.cfg.max_drawdown

        # 4. Weekend protection
        from ..utils.session_times import is_weekend_protection
        checks["no_weekend"] = not is_weekend_protection()

        all_passed = all(checks.values())
        reason = "OK" if all_passed else self._format_rejection(checks)

        return TradePermission(allowed=all_passed, checks=checks, reason=reason)

    def manage_open_trade(self, trade: dict, current_price: float) -> list:
        actions = []
        direction = trade.get("type", 0)
        entry = trade.get("open", trade.get("price_open", 0.0))
        sl = trade.get("sl", 0.0)
        tp = trade.get("tp", 0.0)

        if entry == 0 or sl == 0:
            return []

        sl_dist = abs(entry - sl)
        is_buy = direction == 0

        # Check TP1 (RR ~2.6)
        tp1_price = entry + sl_dist * 2.6 if is_buy else entry - sl_dist * 2.6
        tp2_price = entry + sl_dist * 6.0 if is_buy else entry - sl_dist * 6.0

        tp1_hit = current_price >= tp1_price if is_buy else current_price <= tp1_price
        tp2_hit = current_price >= tp2_price if is_buy else current_price <= tp2_price

        if tp1_hit and not trade.get("partial_1_done", False):
            actions.append({
                "action": "partial_close",
                "percent": self.cfg.tp1_close_pct,
                "move_sl_to": entry,
            })

        elif tp2_hit and not trade.get("partial_2_done", False):
            actions.append({
                "action": "partial_close",
                "percent": self.cfg.tp2_close_pct,
                "move_sl_to": tp1_price,
            })

        # Trailing stop after TP1
        if tp1_hit:
            atr = sl_dist * 0.5  # approx
            mult = 1.0 if tp2_hit else 1.5
            new_sl = current_price - atr * mult if is_buy else current_price + atr * mult
            current_sl = trade.get("sl", 0.0)

            better = new_sl > current_sl if is_buy else new_sl < current_sl
            if better and current_sl > 0:
                actions.append({"action": "modify_sl", "new_sl": round(new_sl, 5)})

        return actions

    def add_closed_trade(self, trade: dict):
        if trade.get("close_time", datetime.utcnow()).date() == date.today():
            self._today_trades.append(trade)

    def _format_rejection(self, checks: dict) -> str:
        failed = [k for k, v in checks.items() if not v]
        return f"Failed: {', '.join(failed)}"
