import pandas as pd
import numpy as np
import time
from datetime import datetime, timezone
from loguru import logger
from typing import Optional
from .core.clock import get_clock
from .models.orders import TradeOrder, OrderResult
from .utils.timeframes import get_mt5_tf

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    logger.warning("MetaTrader5 not installed. Running in simulation mode.")


class MT5ConnectorError(Exception):
    pass


class MT5Connector:
    def __init__(self):
        self.connected = False
        self.account_info = None
        self._sim_mode = not MT5_AVAILABLE
        # Credential cache for reconnection
        self._login: Optional[int] = None
        self._password: Optional[str] = None
        self._server: Optional[str] = None
        # Disconnect tracking (for telegram alerts)
        self._last_disconnect_at: Optional[datetime] = None
        self._last_recovery_downtime_min: int = 0

    def connect(self, login: int, password: str, server: str) -> dict:
        # Save credentials so ensure_connected() can re-login without args
        self._login = login
        self._password = password
        self._server = server

        if self._sim_mode:
            logger.warning("SIM MODE: MT5 not available, using simulation.")
            self.connected = True
            self.account_info = {
                "login": login,
                "balance": 10000.0,
                "equity": 10000.0,
                "leverage": 2000,
                "server": server,
                "currency": "USD",
                "trade_mode": 0,
                "name": "Demo Account",
            }
            return {
                "login": login,
                "balance": 10000.0,
                "leverage": 2000,
                "server": server,
                "currency": "USD",
                "trade_mode": "Demo",
            }

        if not mt5.initialize():
            raise MT5ConnectorError(f"MT5 initialize failed: {mt5.last_error()}")

        authorized = mt5.login(login=login, password=password, server=server)
        if not authorized:
            raise MT5ConnectorError(f"Login failed: {mt5.last_error()}")

        info = mt5.account_info()._asdict()
        self.account_info = info
        self.connected = True
        logger.info(f"Connected to MT5: {info['login']} @ {info['server']}")

        return {
            "login": info["login"],
            "balance": info["balance"],
            "leverage": info["leverage"],
            "server": info["server"],
            "currency": info["currency"],
            "trade_mode": "Demo" if info["trade_mode"] == 0 else "Real",
        }

    def is_connected(self) -> bool:
        """Runtime check: MT5 terminalga hozirgi vaqtda ulanganmi."""
        if self._sim_mode:
            return True  # sim mode da har doim "connected"
        try:
            info = mt5.terminal_info()
            if info is None:
                return False
            # terminal_info().connected — bool flag (terminal serverga ulanganmi)
            return bool(getattr(info, "connected", False))
        except Exception as e:
            logger.debug(f"is_connected() exception: {e}")
            return False

    def ensure_connected(self, max_retries: int = 5, initial_backoff: float = 2.0) -> bool:
        """
        Ulanmagan bo'lsa qayta ulanishga harakat qilish.
        Exponential backoff: 2, 4, 8, 16, 32 soniya (default).
        Disconnect duration'ni track qiladi (telegram alert uchun).

        Returns:
            True if connected (or sim mode), False if all retries exhausted.
        """
        if self.is_connected():
            # Avval uzilgan bo'lsa — recovered duration'ni qayd qil
            if self._last_disconnect_at is not None:
                downtime = (
                    datetime.now(timezone.utc) - self._last_disconnect_at
                ).total_seconds() / 60
                self._last_recovery_downtime_min = int(downtime)
                self._last_disconnect_at = None
                logger.info(
                    f"MT5 connection recovered after {self._last_recovery_downtime_min} min downtime"
                )
            return True

        # Disconnect detected — birinchi marta bo'lsa vaqt belgi qo'y
        if self._last_disconnect_at is None:
            self._last_disconnect_at = datetime.now(timezone.utc)
            logger.warning("MT5 disconnect detected — entering reconnect loop")

        # Cannot reconnect without saved credentials
        if self._login is None or self._password is None or self._server is None:
            logger.error(
                "ensure_connected: no saved credentials — call connect() first"
            )
            return False

        # Retry loop with exponential backoff
        backoff = initial_backoff
        for attempt in range(1, max_retries + 1):
            logger.warning(
                f"MT5 disconnected, reconnect attempt {attempt}/{max_retries}"
            )
            try:
                # Shutdown stale session before re-init (best-effort)
                if not self._sim_mode and MT5_AVAILABLE:
                    try:
                        mt5.shutdown()
                    except Exception:
                        pass
                self.connect(self._login, self._password, self._server)
                if self.is_connected():
                    logger.info(f"MT5 reconnected on attempt {attempt}")
                    # Clear disconnect marker via is_connected path
                    if self._last_disconnect_at is not None:
                        downtime = (
                            datetime.now(timezone.utc) - self._last_disconnect_at
                        ).total_seconds() / 60
                        self._last_recovery_downtime_min = int(downtime)
                        self._last_disconnect_at = None
                    return True
            except Exception as e:
                logger.error(f"Reconnect attempt {attempt} failed: {e}")

            if attempt < max_retries:
                time.sleep(backoff)
                backoff *= 2

        logger.error(f"MT5 reconnect failed after {max_retries} attempts")
        return False

    def get_disconnect_duration_min(self) -> Optional[int]:
        """Hozir uzilgan bo'lsa, uzilgan vaqtdan beri necha daqiqa o'tdi.

        Returns:
            int — uzilgan daqiqalar soni (agar hozir uzilgan bo'lsa)
            None — agar hozir ulangan bo'lsa
        """
        if self._last_disconnect_at is None:
            return None
        return int(
            (datetime.now(timezone.utc) - self._last_disconnect_at).total_seconds() / 60
        )

    def disconnect(self):
        if not self._sim_mode and MT5_AVAILABLE:
            mt5.shutdown()
        self.connected = False
        logger.info("MT5 disconnected.")

    def refresh_account(self) -> dict:
        if self._sim_mode:
            return self.account_info
        info = mt5.account_info()
        if info is None:
            return self.account_info or {}
        self.account_info = info._asdict()
        return self.account_info

    def get_candles(self, symbol: str, timeframe: str, count: int = 500) -> pd.DataFrame:
        if self._sim_mode:
            return self._generate_sim_candles(count)

        tf = get_mt5_tf(timeframe)

        # Ensure symbol is selected and visible
        mt5.symbol_select(symbol, True)

        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None or len(rates) == 0:
            import time as _time
            _time.sleep(2)
            rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None or len(rates) == 0:
            # Real accountda fake data bilan trade xavfli — None qaytariladi
            logger.warning(f"No live data for {symbol} {timeframe} — skipping this TF")
            return None

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df.set_index("time", inplace=True)
        return df

    def get_symbol_info(self, symbol: str) -> dict:
        if self._sim_mode:
            return {
                "name": symbol,
                "point": 0.01 if "XAU" in symbol else 0.00001,
                "volume_min": 0.01,
                "volume_max": 500.0,
                "volume_step": 0.01,
                "trade_tick_value": 1.0,
                "digits": 2 if "XAU" in symbol else 5,
            }

        info = mt5.symbol_info(symbol)
        if info is None:
            raise MT5ConnectorError(f"Symbol not found: {symbol}")
        return info._asdict()

    def get_current_price(self, symbol: str) -> dict:
        if self._sim_mode:
            return {"bid": 3300.0, "ask": 3300.30, "last": 3300.0}

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise MT5ConnectorError(f"No tick data for {symbol}")
        return {"bid": tick.bid, "ask": tick.ask, "last": tick.last}

    def get_open_positions(self) -> list:
        if self._sim_mode:
            return []
        positions = mt5.positions_get()
        if positions is None:
            return []
        # Faqat OpenClaw magic number bilan ochilgan pozitsiyalar
        return [p._asdict() for p in positions if p.magic == 20240101]

    def find_symbol(self, base: str) -> str:
        """Find actual symbol name on broker (e.g. XAUUSD -> XAUUSDm)"""
        candidates = [base, base + "m", base + ".", base + "z", base + "_i"]
        for name in candidates:
            info = mt5.symbol_info(name)
            if info is not None:
                mt5.symbol_select(name, True)
                logger.info(f"Symbol found: {name}")
                return name
        # Search in all symbols
        all_symbols = mt5.symbols_get()
        if all_symbols:
            for s in all_symbols:
                if base.upper() in s.name.upper():
                    mt5.symbol_select(s.name, True)
                    logger.info(f"Symbol found: {s.name}")
                    return s.name
        return base

    def place_order(self, order: TradeOrder) -> OrderResult:
        if self._sim_mode:
            logger.info(f"SIM ORDER: {order.direction.upper()} {order.lot_size} {order.symbol} @ market | SL={order.sl} TP={order.tp}")
            return OrderResult(
                ticket=int(np.random.randint(100000, 999999)),
                price=2350.0,
                volume=order.lot_size,
                retcode=10009,
                symbol=order.symbol,
                direction=order.direction,
            )

        symbol_info = mt5.symbol_info(order.symbol)
        if symbol_info is None:
            raise MT5ConnectorError(f"Symbol not found: {order.symbol}")

        if not symbol_info.visible:
            mt5.symbol_select(order.symbol, True)

        tick = mt5.symbol_info_tick(order.symbol)
        if tick is None:
            raise MT5ConnectorError(f"No tick for {order.symbol}")

        price = tick.ask if order.direction == "buy" else tick.bid

        # Minimum SL/TP distance check
        min_dist = symbol_info.trade_stops_level * symbol_info.point
        sl = order.sl
        tp = order.tp
        if order.direction == "buy":
            if sl > 0 and (price - sl) < min_dist:
                sl = price - min_dist * 2
            if tp > 0 and (tp - price) < min_dist:
                tp = price + min_dist * 2
        else:
            if sl > 0 and (sl - price) < min_dist:
                sl = price + min_dist * 2
            if tp > 0 and (price - tp) < min_dist:
                tp = price - min_dist * 2

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": order.symbol,
            "volume": order.lot_size,
            "type": mt5.ORDER_TYPE_BUY if order.direction == "buy" else mt5.ORDER_TYPE_SELL,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 20,
            "magic": 20240101,
            "comment": "OpenClaw",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        # Try all filling modes
        for filling in [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN]:
            request["type_filling"] = filling
            result = mt5.order_send(request)
            if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
                break

        if result is None:
            raise MT5ConnectorError(f"order_send None: {mt5.last_error()}")

        RETCODE_DESC = {
            10004: "Requote",
            10006: "Request rejected",
            10007: "Request cancelled",
            10008: "Order placed",
            10009: "Order executed",
            10010: "Only partial execution",
            10011: "Request processing error",
            10012: "Request timeout",
            10013: "Invalid request",
            10014: "Invalid volume",
            10015: "Invalid price",
            10016: "Invalid SL/TP",
            10017: "Trade disabled",
            10018: "Market closed",
            10019: "Insufficient funds",
            10020: "Prices changed",
            10021: "No quotes",
            10025: "Too frequent requests",
            10030: "Invalid fill type",
        }

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            desc = RETCODE_DESC.get(result.retcode, "Unknown error")
            raise MT5ConnectorError(
                f"Order failed [{result.retcode}] {desc}: {result.comment}"
            )

        logger.info(f"Order placed: #{result.order} {order.direction} {order.lot_size} {order.symbol}")
        return OrderResult(
            ticket=result.order,
            price=result.price,
            volume=result.volume,
            retcode=result.retcode,
            symbol=order.symbol,
            direction=order.direction,
        )

    def place_pending_order(
        self, symbol: str, direction: str, limit_price: float,
        sl: float, tp: float, lot: float, expiry_hours: float = 6,
    ) -> "OrderResult":
        if self._sim_mode:
            ticket = int(np.random.randint(100000, 999999))
            logger.info(
                f"SIM PENDING: {direction.upper()} LIMIT {lot} {symbol} "
                f"@ {limit_price:.2f} | SL={sl:.2f} TP={tp:.2f}"
            )
            return OrderResult(
                ticket=ticket, price=limit_price, volume=lot,
                retcode=10009, symbol=symbol, direction=direction,
            )

        from datetime import timedelta
        sym_info = mt5.symbol_info(symbol)
        if sym_info is None:
            raise MT5ConnectorError(f"Symbol not found: {symbol}")
        if not sym_info.visible:
            mt5.symbol_select(symbol, True)

        expiry_ts  = int((get_clock().now() + timedelta(hours=expiry_hours)).timestamp()) if expiry_hours > 0 else 0

        stops_lvl  = max(sym_info.trade_stops_level, 10)
        freeze_lvl = getattr(sym_info, "trade_freeze_level", 0) or 0
        min_dist   = (stops_lvl + freeze_lvl + 5) * sym_info.point

        # Har doim LIMIT order (STOP order ishlatilmaydi)
        order_type = mt5.ORDER_TYPE_BUY_LIMIT if direction == "buy" else mt5.ORDER_TYPE_SELL_LIMIT

        if direction == "buy":
            if sl > 0 and (limit_price - sl) < min_dist:
                sl = round(limit_price - min_dist * 2, 2)
            if tp > 0 and (tp - limit_price) < min_dist:
                tp = round(limit_price + min_dist * 2, 2)
            if sl >= limit_price or (tp > 0 and tp <= limit_price):
                raise MT5ConnectorError(f"Invalid buy stops: price={limit_price} sl={sl} tp={tp}")
        else:
            if sl > 0 and (sl - limit_price) < min_dist:
                sl = round(limit_price + min_dist * 2, 2)
            if tp > 0 and (limit_price - tp) < min_dist:
                tp = round(limit_price - min_dist * 2, 2)
            if sl <= limit_price or (tp > 0 and tp >= limit_price):
                raise MT5ConnectorError(f"Invalid sell stops: price={limit_price} sl={sl} tp={tp}")

        request = {
            "action":       mt5.TRADE_ACTION_PENDING,
            "symbol":       symbol,
            "volume":       lot,
            "type":         order_type,
            "price":        limit_price,
            "sl":           sl,
            "tp":           tp,
            "expiration":   expiry_ts,
            "magic":        20240101,
            "comment":      "OpenClaw",
            "type_time":    mt5.ORDER_TIME_SPECIFIED if expiry_ts > 0 else mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_RETURN,
        }

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            retcode = getattr(result, "retcode", "?")
            comment = getattr(result, "comment", str(mt5.last_error()))
            if retcode == 10027:
                raise MT5ConnectorError(
                    "AutoTrading o'chirilgan! MT5 da 'AutoTrading' tugmasini bosing "
                    "(toolbar da yashil o'q) yoki Tools→Options→Expert Advisors→Allow automated trading"
                )
            raise MT5ConnectorError(f"Pending order failed [{retcode}]: {comment}")

        logger.info(
            f"LIMIT #{result.order} {direction.upper()} {lot} {symbol} "
            f"@ {limit_price:.2f} | SL={sl:.2f} TP={tp:.2f}"
        )
        return OrderResult(
            ticket=result.order, price=limit_price, volume=lot,
            retcode=result.retcode, symbol=symbol, direction=direction,
        )

    def get_pending_orders(self, symbol: str = None) -> list:
        if self._sim_mode:
            return []
        orders = mt5.orders_get(symbol=symbol) if symbol else mt5.orders_get()
        return [o._asdict() for o in orders] if orders else []

    def cancel_pending_order(self, ticket: int) -> bool:
        if self._sim_mode:
            logger.info(f"SIM CANCEL pending #{ticket}")
            return True
        result = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": ticket})
        ok = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
        if ok:
            logger.info(f"Pending #{ticket} cancelled")
        return ok

    def modify_position(self, ticket: int, sl: float = None, tp: float = None) -> bool:
        if self._sim_mode:
            logger.info(f"SIM MODIFY: #{ticket} SL={sl} TP={tp}")
            return True

        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return False

        pos = positions[0]
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol": pos.symbol,
            "sl": sl if sl is not None else pos.sl,
            "tp": tp if tp is not None else pos.tp,
        }
        result = mt5.order_send(request)
        if result is None:
            logger.warning(f"modify_position #{ticket}: order_send returned None")
            return False
        return result.retcode == mt5.TRADE_RETCODE_DONE

    def partial_close(self, ticket: int, percent: float) -> bool:
        if self._sim_mode:
            logger.info(f"SIM PARTIAL CLOSE: #{ticket} {percent}%")
            return True

        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return False

        pos = positions[0]
        close_volume = round(pos.volume * (percent / 100), 2)
        if close_volume < 0.01:
            close_volume = 0.01

        tick = mt5.symbol_info_tick(pos.symbol)
        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "position":     ticket,
            "symbol":       pos.symbol,
            "volume":       close_volume,
            "type":         mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY,
            "price":        tick.bid if pos.type == 0 else tick.ask,
            "deviation":    20,
            "magic":        20240101,
            "comment":      f"OpenClaw|partial|{percent}%",
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        for filling in [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN]:
            request["type_filling"] = filling
            result = mt5.order_send(request)
            if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
                return True
        return False

    def close_all_positions(self):
        # get_open_positions allaqachon magic=20240101 bilan filtrlangan
        for pos in self.get_open_positions():
            self.partial_close(pos.get("ticket", 0), 100)

    def get_closed_position(self, ticket: int) -> dict:
        """Yopilgan pozitsiyaning PnL ma'lumotini olish (history deals)."""
        if self._sim_mode:
            return None
        from datetime import timedelta
        date_to   = get_clock().now() + timedelta(hours=1)
        date_from = date_to - timedelta(days=30)
        deals = mt5.history_deals_get(date_from, date_to)
        if deals is None:
            return None
        # Position ID = opening ticket. Closing deals (entry==DEAL_ENTRY_OUT or DEAL_ENTRY_INOUT)
        total_profit = 0.0
        close_price  = 0.0
        found = False
        for deal in deals:
            if deal.position_id == ticket:
                total_profit += deal.profit + deal.swap + deal.commission
                # Closing deal has non-zero price
                if deal.price > 0:
                    close_price = deal.price
                found = True
        if not found:
            return None
        return {"profit": round(total_profit, 2), "price_close": close_price}

    def get_account_history(self, days: int = 1) -> list:
        """Bugungi yopilgan tradelar (daily PnL hisoblash uchun)."""
        if self._sim_mode:
            return []
        from datetime import timedelta
        date_to   = get_clock().now() + timedelta(hours=1)
        date_from = date_to - timedelta(days=days)
        deals = mt5.history_deals_get(date_from, date_to)
        if deals is None:
            return []
        result = []
        seen_positions = {}
        for deal in deals:
            pid = deal.position_id
            if deal.magic != 20240101:
                continue
            seen_positions.setdefault(pid, 0.0)
            seen_positions[pid] += deal.profit + deal.swap + deal.commission
        for pid, pnl in seen_positions.items():
            result.append({"position_id": pid, "pnl": round(pnl, 2)})
        return result

    def _generate_sim_candles(self, count: int) -> pd.DataFrame:
        import numpy as np
        from datetime import datetime, timedelta

        np.random.seed(42)
        # Sim mode fake candles — wall clock OK (sim test fixture, not backtest path)
        dates = [datetime.now(timezone.utc) - timedelta(minutes=15 * i) for i in range(count, 0, -1)]
        price = 3300.0
        rows = []
        for d in dates:
            open_p = price
            change = np.random.normal(0, 2)
            close_p = open_p + change
            high_p = max(open_p, close_p) + abs(np.random.normal(0, 1))
            low_p = min(open_p, close_p) - abs(np.random.normal(0, 1))
            volume = int(np.random.uniform(500, 2000))
            rows.append({"open": open_p, "high": high_p, "low": low_p, "close": close_p, "tick_volume": volume})
            price = close_p

        df = pd.DataFrame(rows, index=pd.DatetimeIndex(dates))
        return df
