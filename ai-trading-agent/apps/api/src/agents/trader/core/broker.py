"""
PaperBroker — Backtesting uchun virtual broker.

Haqiqiy broker xatti-harakatini simulyatsiya qiladi:
- Market va pending (limit/stop) orderlar
- Slippage va spread modellari
- Commission va overnight swap
- Margin call mexanizmi
- SL/TP avtomatik tekshiruvi (conservative: SL > TP)

LINT QOIDASI: `datetime.utcnow()` ishlatish TAQIQLANGAN.
Har doim `clock.now()` orqali vaqtni oling.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from apps.api.src.agents.trader.core.clock import VirtualClock
from apps.api.src.agents.trader.core.data import HistoricalDataManager


# ── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass
class BrokerConfig:
    """
    Broker sozlamalari.

    :param initial_balance: Boshlang'ich balans (USD)
    :param commission_per_lot: Komissiya har lot uchun (masalan 7.0 USD/lot)
    :param slippage_model: Slippage modeli — 'fixed' | 'volatility' | 'liquidity'
    :param swap_rates: Overnight swap stavkalari {(symbol, direction): rate_per_lot}
    :param fixed_slippage_pips: Qat'iy slippage (pip larda), faqat 'fixed' modelda
    :param leverage: Leverage nisbati (masalan 100 = 1:100)
    :param contract_size: Standart lot hajmi (FX = 100_000; XAUUSD = 100)
    """

    initial_balance: float
    commission_per_lot: float
    slippage_model: str
    swap_rates: dict
    fixed_slippage_pips: float = 1.0
    leverage: int = 100
    contract_size: int = 100_000


@dataclass
class Position:
    """
    Ochiq pozitsiya.

    :param ticket: Unikal identifikator
    :param symbol: Savdo instrumenti (masalan 'EURUSD')
    :param direction: Yo'nalish — 'buy' | 'sell'
    :param lot: Lot hajmi
    :param entry_price: Kirish narxi
    :param entry_time: Kirish vaqti
    :param sl: Stop Loss narxi (None — o'rnatilmagan)
    :param tp: Take Profit narxi (None — o'rnatilmagan)
    :param commission: To'langan komissiya
    :param slippage: Qo'llanilgan slippage miqdori
    :param current_price: Joriy narx (on_bar_closed'da yangilanadi)
    :param swap_total: Jami to'plangan swap
    :param unrealized_pnl: Hisoblangan yopilmagan foyda/zarar
    """

    ticket: int
    symbol: str
    direction: str
    lot: float
    entry_price: float
    entry_time: datetime
    sl: float | None
    tp: float | None
    commission: float
    slippage: float
    current_price: float
    swap_total: float = 0.0
    unrealized_pnl: float = 0.0


@dataclass
class Order:
    """
    Kutilayotgan limit yoki stop order.

    :param ticket: Unikal identifikator
    :param symbol: Savdo instrumenti
    :param direction: Yo'nalish — 'buy' | 'sell'
    :param type: Order turi — 'limit' | 'stop'
    :param lot: Lot hajmi
    :param entry_price: Ishga tushirish narxi
    :param sl: Stop Loss narxi
    :param tp: Take Profit narxi
    :param placed_at: Order qo'yilgan vaqt
    """

    ticket: int
    symbol: str
    direction: str
    type: str
    lot: float
    entry_price: float
    sl: float | None
    tp: float | None
    placed_at: datetime


@dataclass
class ClosedTrade:
    """
    Yopilgan savdo yozuvi.

    :param ticket: Unikal identifikator
    :param symbol: Savdo instrumenti
    :param direction: Yo'nalish — 'buy' | 'sell'
    :param lot: Lot hajmi
    :param entry_price: Kirish narxi
    :param exit_price: Chiqish narxi
    :param entry_time: Kirish vaqti
    :param exit_time: Chiqish vaqti
    :param pnl: Yalpi foyda/zarar (USD)
    :param pnl_pips: Foyda/zarar (pip larda)
    :param commission: To'langan komissiya
    :param slippage: Qo'llanilgan slippage
    :param swap: Jami swap
    :param close_reason: Yopish sababi — 'sl'|'tp'|'manual'|'margin_call'|'end_of_test'
    """

    ticket: int
    symbol: str
    direction: str
    lot: float
    entry_price: float
    exit_price: float
    entry_time: datetime
    exit_time: datetime
    pnl: float
    pnl_pips: float
    commission: float
    slippage: float
    swap: float
    close_reason: str


@dataclass
class OrderResult:
    """
    Order yoki fill natijasi.

    :param success: Muvaffaqiyatli yoki yo'q
    :param ticket: Berilgan ticket (success=True da)
    :param fill_price: To'ldirilgan narx (market orderlar uchun)
    :param slippage: Qo'llanilgan slippage
    :param commission: Olingan komissiya
    :param error: Xato kodi (success=False da)
    :param status: 'pending' — limit/stop orderlar uchun
    """

    success: bool
    ticket: int | None = None
    fill_price: float | None = None
    slippage: float | None = None
    commission: float | None = None
    error: str | None = None
    status: str | None = None


# ── PaperBroker ──────────────────────────────────────────────────────────────


class PaperBroker:
    """
    Virtual broker — backtesting uchun haqiqiy broker simulyatsiyasi.

    Qo'llab-quvvatlanadigan funksiyalar:
    - Market orderlar (darhol to'ldirish)
    - Limit va Stop orderlar (kutib turish)
    - Slippage modellari: fixed, volatility, liquidity
    - Spread: tick data yoki tahminiy
    - Overnight swap (chorshanba kuni 3x)
    - Conservative SL/TP tekshiruvi (SL birinchi — worst-case)
    - Margin call mexanizmi
    - Barcha yopilgan savdolar tarixi

    Misol::

        config = BrokerConfig(
            initial_balance=10_000,
            commission_per_lot=7.0,
            slippage_model='fixed',
            swap_rates={('EURUSD', 'buy'): -0.5, ('EURUSD', 'sell'): 0.2},
        )
        broker = PaperBroker(config, clock, data)
        result = broker.place_order('EURUSD', 'buy', 'market', lot=0.1)
    """

    def __init__(
        self,
        config: BrokerConfig,
        clock: VirtualClock,
        data: HistoricalDataManager,
    ) -> None:
        """
        PaperBroker'ni yaratadi.

        :param config: Broker sozlamalari
        :param clock: Virtual soat (look-ahead bias dan himoya)
        :param data: Tarixiy ma'lumotlar menejeri
        """
        self.config = config
        self.clock = clock
        self.data = data

        self.balance: float = config.initial_balance
        self.equity: float = config.initial_balance

        self.positions: dict[int, Position] = {}
        self.pending_orders: dict[int, Order] = {}
        self.history: list[ClosedTrade] = []

        self._next_ticket: int = 1_000_000

    # ── Public order management ──────────────────────────────────────────────

    def place_order(
        self,
        symbol: str,
        direction: str,
        order_type: str,
        lot: float,
        entry: float | None = None,
        sl: float | None = None,
        tp: float | None = None,
    ) -> OrderResult:
        """
        Yangi order qo'yadi.

        Market orderlar darhol to'ldiriladi.
        Limit/stop orderlar on_bar_closed'da tekshiriladi.

        :param symbol: Savdo instrumenti (masalan 'EURUSD')
        :param direction: 'buy' | 'sell'
        :param order_type: 'market' | 'limit' | 'stop'
        :param lot: Lot hajmi
        :param entry: Limit/stop orderlar uchun kirish narxi
        :param sl: Stop Loss narxi
        :param tp: Take Profit narxi
        :return: OrderResult — muvaffaqiyat yoki xato ma'lumotlari bilan
        """
        ticket = self._next_ticket
        self._next_ticket += 1

        # Margin tekshiruvi — market orderlar uchun keyingi bar narxini ishlatamiz
        if entry is not None:
            price_for_margin = entry
        elif order_type == "market":
            next_bar = self._get_next_bar(symbol)
            price_for_margin = next_bar["open"] if next_bar is not None else 1.0
        else:
            price_for_margin = 1.0  # unknown price, skip strict check
        required = self._calc_required_margin(symbol, lot, price_for_margin)
        if required > self.free_margin():
            return OrderResult(success=False, error="NOT_ENOUGH_MONEY")

        if order_type == "market":
            return self._fill_market_order(ticket, symbol, direction, lot, sl, tp)

        # Limit yoki stop order
        if entry is None:
            return OrderResult(
                success=False,
                error="ENTRY_PRICE_REQUIRED",
            )

        order = Order(
            ticket=ticket,
            symbol=symbol,
            direction=direction,
            type=order_type,
            lot=lot,
            entry_price=entry,
            sl=sl,
            tp=tp,
            placed_at=self.clock.now(),
        )
        self.pending_orders[ticket] = order
        return OrderResult(success=True, ticket=ticket, status="pending")

    def close_position(
        self,
        ticket: int,
        reason: str = "manual",
    ) -> ClosedTrade | None:
        """
        Ochiq pozitsiyani qo'lda yopadi.

        :param ticket: Yopiladigan pozitsiya ticket raqami
        :param reason: Yopish sababi (default: 'manual')
        :return: ClosedTrade yoki None (pozitsiya topilmasa)
        """
        pos = self.positions.get(ticket)
        if pos is None:
            return None

        bar = self._get_current_bar(pos.symbol)
        if bar is None:
            # Ma'lumot yo'q — oxirgi ma'lum narx bilan yopamiz
            exit_price = pos.current_price
        else:
            slippage = self._calc_slippage(pos.symbol, pos.lot, pos.direction)
            if pos.direction == "buy":
                exit_price = bar["close"] - slippage
            else:
                exit_price = bar["close"] + slippage

        return self._close_position(
            ticket, pos, exit_price, (reason, exit_price), self.clock.now()
        )

    def cancel_order(self, ticket: int) -> bool:
        """
        Kutilayotgan orderni bekor qiladi.

        :param ticket: Bekor qilinadigan order ticket raqami
        :return: True — muvaffaqiyatli, False — topilmadi
        """
        if ticket in self.pending_orders:
            del self.pending_orders[ticket]
            return True
        return False

    def close_all_positions(self, reason: str = "end_of_test") -> list[ClosedTrade]:
        """
        Barcha ochiq pozitsiyalarni yopadi.

        Backtest oxirida yoki fors-major holatlarda ishlatiladi.

        :param reason: Yopish sababi
        :return: Yopilgan savdolar ro'yxati
        """
        closed: list[ClosedTrade] = []
        for ticket in list(self.positions.keys()):
            trade = self.close_position(ticket, reason=reason)
            if trade is not None:
                closed.append(trade)
        return closed

    # ── Bar event handler ────────────────────────────────────────────────────

    def on_bar_closed(self, symbol: str, bar: pd.Series) -> None:
        """
        Har bir M1 bar yopilganda chaqiriladi.

        Tartib:
        1. Shu symbol uchun pending orderlarni tekshirish
        2. Shu symbol uchun ochiq pozitsiyalarda SL/TP tekshirish
        3. Equity yangilash
        4. Margin call tekshiruvi (equity/balance < 0.5)

        :param symbol: Bar yopilgan instrument
        :param bar: OHLCV bar ma'lumotlari (pd.Series: open, high, low, close, ...)
        """
        # 1. Pending orderlarni tekshirish
        for ticket in list(self.pending_orders.keys()):
            order = self.pending_orders.get(ticket)
            if order is None or order.symbol != symbol:
                continue
            if self._is_pending_triggered(order, bar):
                self._fill_pending_order(ticket, order, bar)

        # 2. Ochiq pozitsiyalarda SL/TP tekshirish
        for ticket in list(self.positions.keys()):
            pos = self.positions.get(ticket)
            if pos is None or pos.symbol != symbol:
                continue

            # Joriy narxni yangilash
            pos.current_price = bar["close"]

            trigger = self._check_sl_tp(pos, bar)
            if trigger is not None:
                reason_str, exit_price = trigger
                self._close_position(
                    ticket, pos, exit_price, (reason_str, exit_price), self.clock.now()
                )

        # 3. Equity yangilash
        self._update_equity()

        # 4. Margin call tekshiruvi
        if self.balance > 0 and self.equity / self.balance < 0.5:
            self._handle_margin_call()

    def apply_overnight_swap(self) -> None:
        """
        Har kecha (server roll-over vaqtida) swap qo'llaydi.

        Chorshanba kuni (weekday == 2) haftalik swap uchun 3x ko'paytirgich.
        Swap balansga qo'shiladi (manfiy bo'lishi mumkin — debit).

        Odatda backtest engine'da har kuni 00:00 da chaqiriladi.
        """
        multiplier = 3 if self.clock.now().weekday() == 2 else 1

        for pos in self.positions.values():
            rate = self.config.swap_rates.get((pos.symbol, pos.direction), 0.0)
            swap = pos.lot * rate * multiplier
            pos.swap_total += swap
            self.balance += swap

    # ── Margin & equity ──────────────────────────────────────────────────────

    def free_margin(self) -> float:
        """
        Bo'sh margin — yangi orderlar uchun mavjud kapital.

        :return: equity - band qilingan margin
        """
        used_margin = sum(
            self._calc_required_margin(p.symbol, p.lot, p.entry_price)
            for p in self.positions.values()
        )
        return self.equity - used_margin

    def margin_level(self) -> float:
        """
        Margin darajasi (foizda).

        :return: (equity / used_margin) * 100, yoki float('inf') agar margin = 0
        """
        used_margin = sum(
            self._calc_required_margin(p.symbol, p.lot, p.entry_price)
            for p in self.positions.values()
        )
        if used_margin == 0:
            return float("inf")
        return (self.equity / used_margin) * 100.0

    # ── Summary helpers ──────────────────────────────────────────────────────

    def get_summary(self) -> dict:
        """
        Backtest yakuniy statistikasi.

        :return: Balans, equity, pozitsiyalar, savdolar va asosiy metrikalar
        """
        total_pnl = sum(t.pnl for t in self.history)
        wins = [t for t in self.history if t.pnl > 0]
        losses = [t for t in self.history if t.pnl <= 0]
        win_rate = len(wins) / len(self.history) * 100 if self.history else 0.0

        return {
            "balance": self.balance,
            "equity": self.equity,
            "open_positions": len(self.positions),
            "pending_orders": len(self.pending_orders),
            "total_trades": len(self.history),
            "total_pnl": total_pnl,
            "win_rate_pct": win_rate,
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "free_margin": self.free_margin(),
            "margin_level_pct": self.margin_level(),
        }

    # ── Private: order filling ────────────────────────────────────────────────

    def _fill_market_order(
        self,
        ticket: int,
        symbol: str,
        direction: str,
        lot: float,
        sl: float | None,
        tp: float | None,
    ) -> OrderResult:
        """
        Market orderni keyingi bar open narxida to'ldiradi.

        :param ticket: Order ticket raqami
        :param symbol: Savdo instrumenti
        :param direction: 'buy' | 'sell'
        :param lot: Lot hajmi
        :param sl: Stop Loss narxi
        :param tp: Take Profit narxi
        :return: OrderResult to'ldirish tafsilotlari bilan
        """
        next_bar = self._get_next_bar(symbol)
        if next_bar is None:
            return OrderResult(success=False, error="NO_DATA")

        slippage = self._calc_slippage(symbol, lot, direction)
        spread = self._get_spread(symbol, next_bar.name)

        if direction == "buy":
            fill_price = next_bar["open"] + spread / 2.0 + slippage
        else:
            fill_price = next_bar["open"] - spread / 2.0 - slippage

        commission = lot * self.config.commission_per_lot

        pos = Position(
            ticket=ticket,
            symbol=symbol,
            direction=direction,
            lot=lot,
            entry_price=fill_price,
            entry_time=self.clock.now(),
            sl=sl,
            tp=tp,
            commission=commission,
            slippage=slippage,
            current_price=fill_price,
        )
        self.positions[ticket] = pos
        self.balance -= commission

        return OrderResult(
            success=True,
            ticket=ticket,
            fill_price=fill_price,
            slippage=slippage,
            commission=commission,
        )

    def _fill_pending_order(
        self,
        ticket: int,
        order: Order,
        bar: pd.Series,
    ) -> None:
        """
        Triggered bo'lgan pending orderni pozitsiyaga aylantiradi.

        Narx order.entry_price da to'ldiriladi (slip yo'q — narx allaqachon kelishilgan).
        Komissiya balansdan ayiriladi.

        :param ticket: Order ticket raqami
        :param order: Filled bo'ladigan Order obyekti
        :param bar: Trigger bo'lgan bar ma'lumotlari
        """
        commission = order.lot * self.config.commission_per_lot

        pos = Position(
            ticket=ticket,
            symbol=order.symbol,
            direction=order.direction,
            lot=order.lot,
            entry_price=order.entry_price,
            entry_time=self.clock.now(),
            sl=order.sl,
            tp=order.tp,
            commission=commission,
            slippage=0.0,
            current_price=order.entry_price,
        )

        del self.pending_orders[ticket]
        self.positions[ticket] = pos
        self.balance -= commission

    # ── Private: position closing ─────────────────────────────────────────────

    def _close_position(
        self,
        ticket: int,
        pos: Position,
        exit_price: float,
        close_reason: tuple[str, float],
        exit_time: datetime,
    ) -> ClosedTrade:
        """
        Pozitsiyani yopadi, ClosedTrade yaratadi va tarixga qo'shadi.

        PnL hisoblash:
        - buy: (exit_price - entry_price) * lot * contract_size
        - sell: (entry_price - exit_price) * lot * contract_size

        Balans yangilanishi:
        - balance += pnl  (swap allaqachon apply_overnight_swap'da qo'shilgan)
        - Komissiya kirish paytida ayirilgan — qayta ayirish yo'q

        :param ticket: Pozitsiya ticket raqami
        :param pos: Yopiladigan Position obyekti
        :param exit_price: Chiqish narxi
        :param close_reason: (sabab_str, narx) tuple
        :param exit_time: Yopish vaqti
        :return: Yaratilgan ClosedTrade
        """
        reason_str, _ = close_reason
        pip_size = self._pip_size(pos.symbol)

        if pos.direction == "buy":
            pnl = (exit_price - pos.entry_price) * pos.lot * self.config.contract_size
            pnl_pips = (exit_price - pos.entry_price) / pip_size
        else:
            pnl = (pos.entry_price - exit_price) * pos.lot * self.config.contract_size
            pnl_pips = (pos.entry_price - exit_price) / pip_size

        trade = ClosedTrade(
            ticket=ticket,
            symbol=pos.symbol,
            direction=pos.direction,
            lot=pos.lot,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            entry_time=pos.entry_time,
            exit_time=exit_time,
            pnl=pnl,
            pnl_pips=pnl_pips,
            commission=pos.commission,
            slippage=pos.slippage,
            swap=pos.swap_total,
            close_reason=reason_str,
        )

        self.history.append(trade)
        del self.positions[ticket]

        # Realized pnl balansga — komissiya kirish paytida ayirilgan
        self.balance += pnl

        return trade

    # ── Private: SL/TP logic ─────────────────────────────────────────────────

    def _check_sl_tp(
        self,
        pos: Position,
        bar: pd.Series,
    ) -> tuple[str, float] | None:
        """
        Conservative SL/TP tekshiruvi — SL birinchi (worst-case scenario).

        Buy pozitsiya uchun:
        - Low <= SL → SL triggered (slippage bilan)
        - High >= TP → TP triggered

        Sell pozitsiya uchun:
        - High >= SL → SL triggered (slippage bilan)
        - Low <= TP → TP triggered

        :param pos: Tekshiriladigan pozitsiya
        :param bar: Joriy bar (open, high, low, close)
        :return: (sabab, exit_narx) tuple yoki None
        """
        if pos.direction == "buy":
            # SL birinchi tekshiriladi (worst-case)
            if pos.sl is not None and bar["low"] <= pos.sl:
                slippage = self._calc_slippage(pos.symbol, pos.lot, "sell")
                return ("sl", pos.sl - slippage)
            if pos.tp is not None and bar["high"] >= pos.tp:
                return ("tp", pos.tp)

        else:  # sell
            # SL birinchi tekshiriladi (worst-case)
            if pos.sl is not None and bar["high"] >= pos.sl:
                slippage = self._calc_slippage(pos.symbol, pos.lot, "buy")
                return ("sl", pos.sl + slippage)
            if pos.tp is not None and bar["low"] <= pos.tp:
                return ("tp", pos.tp)

        return None

    # ── Private: pending order trigger ───────────────────────────────────────

    def _is_pending_triggered(self, order: Order, bar: pd.Series) -> bool:
        """
        Pending order trigger bo'lganligini tekshiradi.

        Trigger qoidalari:
        - limit buy:  bar low  <= order.entry_price
        - limit sell: bar high >= order.entry_price
        - stop buy:   bar high >= order.entry_price
        - stop sell:  bar low  <= order.entry_price

        :param order: Tekshiriladigan pending order
        :param bar: Joriy bar ma'lumotlari
        :return: True — triggered, False — yo'q
        """
        if order.type == "limit":
            if order.direction == "buy":
                return bar["low"] <= order.entry_price
            else:  # sell
                return bar["high"] >= order.entry_price

        elif order.type == "stop":
            if order.direction == "buy":
                return bar["high"] >= order.entry_price
            else:  # sell
                return bar["low"] <= order.entry_price

        return False

    # ── Private: equity & margin call ────────────────────────────────────────

    def _update_equity(self) -> None:
        """
        Barcha ochiq pozitsiyalar bo'yicha equity'ni qayta hisoblaydi.

        Har bir pozitsiya unrealized_pnl yangilanadi, so'ngra
        equity = balance + sum(unrealized_pnl).

        on_bar_closed oxirida chaqiriladi.
        """
        total_unrealized = 0.0
        for pos in self.positions.values():
            if pos.direction == "buy":
                pos.unrealized_pnl = (
                    (pos.current_price - pos.entry_price)
                    * pos.lot
                    * self.config.contract_size
                )
            else:
                pos.unrealized_pnl = (
                    (pos.entry_price - pos.current_price)
                    * pos.lot
                    * self.config.contract_size
                )
            total_unrealized += pos.unrealized_pnl

        self.equity = self.balance + total_unrealized

    def _handle_margin_call(self) -> None:
        """
        Margin call — barcha pozitsiyalarni joriy narxda yopadi.

        Trigger sharti: equity / balance < 0.5 (50% margin level).
        Barcha pozitsiyalar 'margin_call' sababi bilan yopiladi.
        """
        for ticket in list(self.positions.keys()):
            pos = self.positions.get(ticket)
            if pos is None:
                continue
            bar = self._get_current_bar(pos.symbol)
            if bar is not None:
                exit_price = bar["close"]
            else:
                exit_price = pos.current_price

            self._close_position(
                ticket,
                pos,
                exit_price,
                ("margin_call", exit_price),
                self.clock.now(),
            )

    # ── Private: slippage & spread ────────────────────────────────────────────

    def _calc_slippage(self, symbol: str, lot: float, direction: str) -> float:
        """
        Slippage miqdorini hisoblaydi (price unitlarda).

        Modelllar:
        - 'fixed': config.fixed_slippage_pips * pip_size
        - 'volatility': ATR * 0.05
        - 'liquidity': ATR * 0.05 * (1 + (lot - 0.01) * 0.5)

        :param symbol: Savdo instrumenti
        :param lot: Lot hajmi
        :param direction: Yo'nalish (hozircha foydalanilmaydi, kelajak uchun)
        :return: Slippage miqdori (price unitlarda, musbat)
        """
        model = self.config.slippage_model

        if model == "fixed":
            return self.config.fixed_slippage_pips * self._pip_size(symbol)

        elif model in ("volatility", "liquidity"):
            atr = self._get_recent_atr(symbol)
            base_slip = atr * 0.05
            if model == "liquidity":
                lot_factor = 1.0 + (lot - 0.01) * 0.5
                return base_slip * lot_factor
            return base_slip

        return 0.0

    def _get_spread(self, symbol: str, timestamp: object) -> float:
        """
        Joriy spread'ni qaytaradi (price unitlarda).

        Birinchi navbatda tick data'dan ask-bid farqini oladi.
        Tick data yo'q bo'lsa — tahminiy spread ishlatiladi.
        Minimal spread: pip_size * 1.0.

        :param symbol: Savdo instrumenti
        :param timestamp: Spread olish vaqti
        :return: Spread miqdori (price unitlarda)
        """
        min_spread = self._pip_size(symbol) * 1.0

        try:
            ts = timestamp if isinstance(timestamp, datetime) else None
            if ts is not None:
                tick = self.data.get_tick(symbol, ts)
                if tick and "ask" in tick and "bid" in tick:
                    spread = tick["ask"] - tick["bid"]
                    return max(spread, min_spread)
        except Exception:
            pass

        estimated = self.data._estimate_spread(symbol, self.clock.now())
        return max(estimated, min_spread)

    # ── Private: data access ──────────────────────────────────────────────────

    def _get_next_bar(self, symbol: str) -> pd.Series | None:
        """
        Keyingi M1 bar'ni qaytaradi (market order uchun fill narxi).

        count=2 bilan so'rov yuboriladi: oxirgi yopilgan bar (index -1) qaytariladi.
        Bu simulyatorda "keyingi bar open" ga fill qilish uchun ishlatiladi.

        :param symbol: Savdo instrumenti
        :return: pd.Series (bar) yoki None
        """
        try:
            candles = self.data.get_candles_at(symbol, "M1", self.clock.now(), count=2)
            if candles.empty:
                return None
            return candles.iloc[-1]
        except (KeyError, Exception):
            return None

    def _get_current_bar(self, symbol: str) -> pd.Series | None:
        """
        Joriy M1 bar'ni qaytaradi (yopish narxi uchun).

        :param symbol: Savdo instrumenti
        :return: pd.Series (bar) yoki None
        """
        try:
            candles = self.data.get_candles_at(symbol, "M1", self.clock.now(), count=1)
            if candles.empty:
                return None
            return candles.iloc[-1]
        except (KeyError, Exception):
            return None

    # ── Private: calculations ─────────────────────────────────────────────────

    def _calc_required_margin(
        self,
        symbol: str,
        lot: float,
        price: float,
    ) -> float:
        """
        Pozitsiya uchun talab qilinadigan margin'ni hisoblaydi.

        Formulasi: (lot * contract_size * price) / leverage

        :param symbol: Savdo instrumenti (hozircha foydalanilmaydi — kelajak uchun)
        :param lot: Lot hajmi
        :param price: Narx
        :return: Talab qilinadigan margin (USD)
        """
        return (lot * self.config.contract_size * price) / self.config.leverage

    def _pip_size(self, symbol: str) -> float:
        """
        Instrument uchun pip hajmini qaytaradi (price unitlarda).

        XAUUSD (gold): 0.10  — bu loyiha XAUUSD birinchi instrument sifatida ishlatadi
                              (CLAUDE.md: PIP = 0.10).
        JPY juftliklari: 0.01
        Boshqa barcha juftliklar: 0.0001

        :param symbol: Savdo instrumenti
        :return: Pip hajmi
        """
        sym = symbol.upper()
        if sym == "XAUUSD" or sym.startswith("XAU"):
            return 0.10
        if "JPY" in sym:
            return 0.01
        return 0.0001

    def _get_recent_atr(self, symbol: str) -> float:
        """
        Oxirgi 14 M15 bar bo'yicha ATR'ni hisoblaydi.

        ATR = o'rtacha (high - low) — soddalashtirilgan hisoblash.
        Ma'lumot yo'q bo'lsa yoki xato yuz bersa — 0.0001 qaytaradi.

        :param symbol: Savdo instrumenti
        :return: ATR miqdori (price unitlarda)
        """
        try:
            candles = self.data.get_candles_at(
                symbol, "M15", self.clock.now(), count=14
            )
            if candles.empty:
                return 0.0001
            atr = (candles["high"] - candles["low"]).mean()
            return float(atr) if atr > 0 else 0.0001
        except Exception:
            return 0.0001
