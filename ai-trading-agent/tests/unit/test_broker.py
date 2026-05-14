"""
Unit tests for PaperBroker.

Tests cover:
- Market order fill (buy/sell, commission, slippage, OrderResult, position storage)
- Limit/stop pending orders (storage, triggering, non-triggering)
- SL/TP triggering (conservative: SL wins when both hit same bar)
- PnL calculation (buy/sell profit/loss, pip PnL, balance update)
- Equity and margin (equity formula, free margin, margin check)
- Margin call (closes all positions, sets close_reason)
- Overnight swap (balance update, triple Wednesday, no-positions guard)
- Slippage models (fixed, volatility, liquidity)
- Pip size by symbol
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pandas as pd
import pytest

from apps.api.src.agents.trader.core.broker import (
    BrokerConfig,
    PaperBroker,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def config():
    return BrokerConfig(
        initial_balance=10_000.0,
        commission_per_lot=7.0,
        slippage_model="fixed",
        swap_rates={("EURUSD", "buy"): -0.5, ("EURUSD", "sell"): 0.2},
        fixed_slippage_pips=1.0,
        leverage=100,
        contract_size=100_000,
    )


@pytest.fixture
def clock():
    mock = MagicMock()
    mock.now.return_value = datetime(2024, 1, 15, 10, 0, tzinfo=timezone.utc)
    return mock


@pytest.fixture
def make_bar():
    """Factory to create a pd.Series bar."""

    def _make(
        open_=1.1000,
        high=1.1050,
        low=1.0950,
        close=1.1020,
        ts=datetime(2024, 1, 15, 10, 15, tzinfo=timezone.utc),
    ):
        return pd.Series(
            {"open": open_, "high": high, "low": low, "close": close},
            name=ts,
        )

    return _make


@pytest.fixture
def broker(config, clock):
    data = MagicMock()
    data._estimate_spread.return_value = 0.00020
    data.get_tick.return_value = None  # no tick data → use estimate
    return PaperBroker(config, clock, data)


# ── TestMarketOrderFill ───────────────────────────────────────────────────────


class TestMarketOrderFill:
    """Market orders should fill immediately at next bar open ± spread/2 ± slippage."""

    def test_market_buy_fill_at_next_bar_open(self, broker, make_bar):
        """Buy fills at next_bar open + spread/2 + slippage."""
        bar = make_bar(open_=1.1000)
        broker._get_next_bar = MagicMock(return_value=bar)
        # spread = 0.00020, half = 0.00010; slippage = 1 pip = 0.0001
        # expected fill = 1.1000 + 0.00010 + 0.0001 = 1.10020
        result = broker.place_order(
            symbol="EURUSD",
            direction="buy",
            order_type="market",
            lot=0.1,
        )
        assert result.success is True
        assert result.fill_price == pytest.approx(1.1000 + 0.00010 + 0.0001, abs=1e-6)

    def test_market_sell_fill_at_next_bar_open(self, broker, make_bar):
        """Sell fills at next_bar open - spread/2 - slippage."""
        bar = make_bar(open_=1.1000)
        broker._get_next_bar = MagicMock(return_value=bar)
        # expected fill = 1.1000 - 0.00010 - 0.0001 = 1.09980
        result = broker.place_order(
            symbol="EURUSD",
            direction="sell",
            order_type="market",
            lot=0.1,
        )
        assert result.success is True
        assert result.fill_price == pytest.approx(1.1000 - 0.00010 - 0.0001, abs=1e-6)

    def test_commission_deducted_on_fill(self, broker, make_bar):
        """Balance should decrease by commission after fill."""
        bar = make_bar(open_=1.1000)
        broker._get_next_bar = MagicMock(return_value=bar)
        initial_balance = broker.balance
        broker.place_order(
            symbol="EURUSD",
            direction="buy",
            order_type="market",
            lot=1.0,
        )
        # commission = commission_per_lot * lot = 7.0 * 1.0 = 7.0
        assert broker.balance == pytest.approx(initial_balance - 7.0, abs=1e-6)

    def test_fill_returns_correct_orderresult(self, broker, make_bar):
        """OrderResult has correct ticket, fill_price, slippage, commission."""
        bar = make_bar(open_=1.1000)
        broker._get_next_bar = MagicMock(return_value=bar)
        result = broker.place_order(
            symbol="EURUSD",
            direction="buy",
            order_type="market",
            lot=0.5,
        )
        assert result.success is True
        assert result.ticket is not None
        assert isinstance(result.ticket, int)
        assert result.fill_price is not None
        assert result.slippage is not None
        # commission = 7.0 * 0.5 = 3.5
        assert result.commission == pytest.approx(3.5, abs=1e-6)

    def test_fill_no_data_returns_error(self, broker):
        """When _get_next_bar returns None → OrderResult(success=False, error='NO_DATA')."""
        broker._get_next_bar = MagicMock(return_value=None)
        result = broker.place_order(
            symbol="EURUSD",
            direction="buy",
            order_type="market",
            lot=0.1,
        )
        assert result.success is False
        assert result.error == "NO_DATA"

    def test_position_stored_after_fill(self, broker, make_bar):
        """Position should be in broker.positions after a successful market fill."""
        bar = make_bar(open_=1.1000)
        broker._get_next_bar = MagicMock(return_value=bar)
        result = broker.place_order(
            symbol="EURUSD",
            direction="buy",
            order_type="market",
            lot=0.1,
        )
        assert result.success is True
        assert result.ticket in broker.positions
        pos = broker.positions[result.ticket]
        assert pos.symbol == "EURUSD"
        assert pos.direction == "buy"
        assert pos.lot == pytest.approx(0.1, abs=1e-9)

    def test_ticket_increments(self, broker, make_bar):
        """Each order gets a unique, incrementing ticket number."""
        bar = make_bar(open_=1.1000)
        broker._get_next_bar = MagicMock(return_value=bar)
        result1 = broker.place_order("EURUSD", "buy", "market", 0.1)
        result2 = broker.place_order("EURUSD", "sell", "market", 0.1)
        assert result1.ticket != result2.ticket
        assert result2.ticket == result1.ticket + 1


# ── TestLimitStopOrders ───────────────────────────────────────────────────────


class TestLimitStopOrders:
    """Limit and stop orders should be stored as pending and trigger on price levels."""

    def test_limit_buy_stored_as_pending(self, broker):
        """A limit buy order should be placed in broker.pending_orders."""
        result = broker.place_order(
            symbol="EURUSD",
            direction="buy",
            order_type="limit",
            lot=0.1,
            entry=1.0950,
        )
        assert result.success is True
        assert result.ticket in broker.pending_orders

    def test_stop_buy_stored_as_pending(self, broker):
        """A stop buy order should be placed in broker.pending_orders."""
        result = broker.place_order(
            symbol="EURUSD",
            direction="buy",
            order_type="stop",
            lot=0.1,
            entry=1.1100,
        )
        assert result.success is True
        assert result.ticket in broker.pending_orders

    def test_pending_order_result_has_status_pending(self, broker):
        """OrderResult.status should be 'pending' for limit/stop orders."""
        result = broker.place_order(
            symbol="EURUSD",
            direction="buy",
            order_type="limit",
            lot=0.1,
            entry=1.0950,
        )
        assert result.status == "pending"

    def test_pending_order_triggered_limit_buy(self, broker, make_bar):
        """Limit buy fills when bar_low <= entry_price."""
        result = broker.place_order(
            symbol="EURUSD",
            direction="buy",
            order_type="limit",
            lot=0.1,
            entry=1.0980,
        )
        ticket = result.ticket
        # bar low = 1.0950 <= 1.0980 → should trigger
        bar = make_bar(open_=1.1000, high=1.1050, low=1.0950, close=1.1020)
        broker.on_bar_closed("EURUSD", bar)
        assert ticket not in broker.pending_orders
        assert ticket in broker.positions

    def test_pending_order_triggered_stop_buy(self, broker, make_bar):
        """Stop buy fills when bar_high >= entry_price."""
        result = broker.place_order(
            symbol="EURUSD",
            direction="buy",
            order_type="stop",
            lot=0.1,
            entry=1.1040,
        )
        ticket = result.ticket
        # bar high = 1.1050 >= 1.1040 → should trigger
        bar = make_bar(open_=1.1000, high=1.1050, low=1.0950, close=1.1020)
        broker.on_bar_closed("EURUSD", bar)
        assert ticket not in broker.pending_orders
        assert ticket in broker.positions

    def test_pending_order_not_triggered(self, broker, make_bar):
        """Pending order stays in pending_orders when price is not reached."""
        result = broker.place_order(
            symbol="EURUSD",
            direction="buy",
            order_type="limit",
            lot=0.1,
            entry=1.0900,  # bar low = 1.0950, won't reach 1.0900
        )
        ticket = result.ticket
        bar = make_bar(open_=1.1000, high=1.1050, low=1.0950, close=1.1020)
        broker.on_bar_closed("EURUSD", bar)
        assert ticket in broker.pending_orders
        assert ticket not in broker.positions


# ── TestSLTPTriggering ────────────────────────────────────────────────────────


class TestSLTPTriggering:
    """SL and TP should trigger correctly; SL wins over TP in the same bar (conservative)."""

    def _open_buy(self, broker, make_bar, entry=1.1000, sl=1.0950, tp=1.1100):
        """Helper: open a buy position and return its ticket."""
        bar = make_bar(open_=entry)
        broker._get_next_bar = MagicMock(return_value=bar)
        result = broker.place_order(
            symbol="EURUSD",
            direction="buy",
            order_type="market",
            lot=0.1,
            sl=sl,
            tp=tp,
        )
        return result.ticket

    def _open_sell(self, broker, make_bar, entry=1.1000, sl=1.1050, tp=1.0900):
        """Helper: open a sell position and return its ticket."""
        bar = make_bar(open_=entry)
        broker._get_next_bar = MagicMock(return_value=bar)
        result = broker.place_order(
            symbol="EURUSD",
            direction="sell",
            order_type="market",
            lot=0.1,
            sl=sl,
            tp=tp,
        )
        return result.ticket

    def test_buy_sl_triggered(self, broker, make_bar):
        """Bar low <= sl triggers close at (sl - slippage) for buy."""
        ticket = self._open_buy(broker, make_bar, entry=1.1000, sl=1.0960, tp=1.1100)
        # bar low = 1.0950 <= sl 1.0960 → SL hit
        bar = make_bar(open_=1.1000, high=1.1020, low=1.0950, close=1.0980)
        broker._get_current_bar = MagicMock(return_value=bar)
        broker.on_bar_closed("EURUSD", bar)
        assert ticket not in broker.positions
        assert len(broker.history) == 1
        trade = broker.history[0]
        # exit price = sl - slippage (1 pip = 0.0001)
        assert trade.exit_price == pytest.approx(1.0960 - 0.0001, abs=1e-6)

    def test_buy_tp_triggered(self, broker, make_bar):
        """Bar high >= tp triggers close at tp for buy."""
        ticket = self._open_buy(broker, make_bar, entry=1.1000, sl=1.0950, tp=1.1040)
        # bar high = 1.1050 >= tp 1.1040 → TP hit
        bar = make_bar(open_=1.1000, high=1.1050, low=1.0970, close=1.1030)
        broker._get_current_bar = MagicMock(return_value=bar)
        broker.on_bar_closed("EURUSD", bar)
        assert ticket not in broker.positions
        assert len(broker.history) == 1
        trade = broker.history[0]
        assert trade.exit_price == pytest.approx(1.1040, abs=1e-6)

    def test_sell_sl_triggered(self, broker, make_bar):
        """Bar high >= sl triggers close at (sl + slippage) for sell."""
        ticket = self._open_sell(broker, make_bar, entry=1.1000, sl=1.1040, tp=1.0900)
        # bar high = 1.1050 >= sl 1.1040 → SL hit
        bar = make_bar(open_=1.1000, high=1.1050, low=1.0970, close=1.1020)
        broker._get_current_bar = MagicMock(return_value=bar)
        broker.on_bar_closed("EURUSD", bar)
        assert ticket not in broker.positions
        assert len(broker.history) == 1
        trade = broker.history[0]
        # exit price = sl + slippage (1 pip = 0.0001)
        assert trade.exit_price == pytest.approx(1.1040 + 0.0001, abs=1e-6)

    def test_sell_tp_triggered(self, broker, make_bar):
        """Bar low <= tp triggers close at tp for sell."""
        ticket = self._open_sell(broker, make_bar, entry=1.1000, sl=1.1050, tp=1.0960)
        # bar low = 1.0950 <= tp 1.0960 → TP hit
        bar = make_bar(open_=1.1000, high=1.1020, low=1.0950, close=1.0980)
        broker._get_current_bar = MagicMock(return_value=bar)
        broker.on_bar_closed("EURUSD", bar)
        assert ticket not in broker.positions
        assert len(broker.history) == 1
        trade = broker.history[0]
        assert trade.exit_price == pytest.approx(1.0960, abs=1e-6)

    def test_sl_before_tp_conservative(self, broker, make_bar):
        """When both SL and TP hit in same bar, SL wins (conservative)."""
        # buy: sl=1.0960, tp=1.1040
        # bar: low=1.0950 (hits sl) AND high=1.1050 (hits tp) in same bar
        ticket = self._open_buy(broker, make_bar, entry=1.1000, sl=1.0960, tp=1.1040)
        bar = make_bar(open_=1.1000, high=1.1050, low=1.0950, close=1.1000)
        broker._get_current_bar = MagicMock(return_value=bar)
        broker.on_bar_closed("EURUSD", bar)
        assert ticket not in broker.positions
        assert len(broker.history) == 1
        trade = broker.history[0]
        assert trade.close_reason == "sl"

    def test_trade_moves_to_history(self, broker, make_bar):
        """Closed trade appears in broker.history after SL/TP close."""
        ticket = self._open_buy(broker, make_bar, entry=1.1000, sl=1.0950, tp=1.1040)
        bar = make_bar(open_=1.1000, high=1.1050, low=1.0970, close=1.1030)
        broker._get_current_bar = MagicMock(return_value=bar)
        broker.on_bar_closed("EURUSD", bar)
        assert len(broker.history) == 1
        assert broker.history[0].ticket == ticket

    def test_close_reason_sl(self, broker, make_bar):
        """ClosedTrade.close_reason == 'sl' when closed by stop loss."""
        ticket = self._open_buy(broker, make_bar, entry=1.1000, sl=1.0960, tp=1.1100)
        bar = make_bar(open_=1.1000, high=1.1020, low=1.0950, close=1.0980)
        broker._get_current_bar = MagicMock(return_value=bar)
        broker.on_bar_closed("EURUSD", bar)
        assert broker.history[0].close_reason == "sl"

    def test_close_reason_tp(self, broker, make_bar):
        """ClosedTrade.close_reason == 'tp' when closed by take profit."""
        ticket = self._open_buy(broker, make_bar, entry=1.1000, sl=1.0950, tp=1.1040)
        bar = make_bar(open_=1.1000, high=1.1050, low=1.0970, close=1.1030)
        broker._get_current_bar = MagicMock(return_value=bar)
        broker.on_bar_closed("EURUSD", bar)
        assert broker.history[0].close_reason == "tp"


# ── TestPnLCalculation ────────────────────────────────────────────────────────


class TestPnLCalculation:
    """PnL should be calculated correctly for buy and sell positions."""

    def _place_and_close_buy(self, broker, make_bar, entry, exit_price, lot=0.1):
        """Helper: open a buy at entry and close at exit_price via public API."""
        open_bar = make_bar(open_=entry)
        broker._get_next_bar = MagicMock(return_value=open_bar)
        broker._calc_slippage = MagicMock(return_value=0.0)
        broker.data._estimate_spread.return_value = 0.0
        result = broker.place_order("EURUSD", "buy", "market", lot)
        # close_position uses _get_current_bar(close) for exit price
        close_bar = make_bar(open_=exit_price, high=exit_price, low=exit_price, close=exit_price)
        broker._get_current_bar = MagicMock(return_value=close_bar)
        broker.close_position(result.ticket, "manual")
        return broker.history[-1]

    def _place_and_close_sell(self, broker, make_bar, entry, exit_price, lot=0.1):
        """Helper: open a sell at entry and close at exit_price via public API."""
        open_bar = make_bar(open_=entry)
        broker._get_next_bar = MagicMock(return_value=open_bar)
        broker._calc_slippage = MagicMock(return_value=0.0)
        broker.data._estimate_spread.return_value = 0.0
        result = broker.place_order("EURUSD", "sell", "market", lot)
        close_bar = make_bar(open_=exit_price, high=exit_price, low=exit_price, close=exit_price)
        broker._get_current_bar = MagicMock(return_value=close_bar)
        broker.close_position(result.ticket, "manual")
        return broker.history[-1]

    def _suppress_costs(self, broker):
        """Mock away spread and slippage for clean PnL arithmetic."""
        broker._calc_slippage = MagicMock(return_value=0.0)
        broker._get_spread = MagicMock(return_value=0.0)

    def test_buy_profit(self, broker, make_bar):
        """Buy fills at 1.1000, closes at 1.1100, lot=0.1 → pnl = 100."""
        # pnl = (exit - entry) * lot * contract_size = 0.01 * 0.1 * 100_000 = 100
        self._suppress_costs(broker)
        open_bar = make_bar(open_=1.1000)
        broker._get_next_bar = MagicMock(return_value=open_bar)
        result = broker.place_order("EURUSD", "buy", "market", 0.1)
        assert result.success is True
        close_bar = make_bar(open_=1.1100, high=1.1100, low=1.1100, close=1.1100)
        broker._get_current_bar = MagicMock(return_value=close_bar)
        broker.close_position(result.ticket, "manual")
        trade = broker.history[-1]
        assert trade.pnl == pytest.approx(100.0, abs=0.01)

    def test_buy_loss(self, broker, make_bar):
        """Buy fills at 1.1100, closes at 1.1000 → pnl ≈ -100."""
        self._suppress_costs(broker)
        open_bar = make_bar(open_=1.1100)
        broker._get_next_bar = MagicMock(return_value=open_bar)
        result = broker.place_order("EURUSD", "buy", "market", 0.1)
        close_bar = make_bar(open_=1.1000, high=1.1000, low=1.1000, close=1.1000)
        broker._get_current_bar = MagicMock(return_value=close_bar)
        broker.close_position(result.ticket, "manual")
        trade = broker.history[-1]
        assert trade.pnl == pytest.approx(-100.0, abs=0.01)

    def test_sell_profit(self, broker, make_bar):
        """Sell fills at 1.1100, closes at 1.1000 → pnl ≈ 100."""
        self._suppress_costs(broker)
        open_bar = make_bar(open_=1.1100)
        broker._get_next_bar = MagicMock(return_value=open_bar)
        result = broker.place_order("EURUSD", "sell", "market", 0.1)
        close_bar = make_bar(open_=1.1000, high=1.1000, low=1.1000, close=1.1000)
        broker._get_current_bar = MagicMock(return_value=close_bar)
        broker.close_position(result.ticket, "manual")
        trade = broker.history[-1]
        assert trade.pnl == pytest.approx(100.0, abs=0.01)

    def test_pnl_pips_buy(self, broker, make_bar):
        """pnl_pips for buy = (exit - entry) / pip_size; EURUSD pip_size = 0.0001."""
        self._suppress_costs(broker)
        open_bar = make_bar(open_=1.1000)
        broker._get_next_bar = MagicMock(return_value=open_bar)
        result = broker.place_order("EURUSD", "buy", "market", 0.1)
        close_bar = make_bar(open_=1.1100, high=1.1100, low=1.1100, close=1.1100)
        broker._get_current_bar = MagicMock(return_value=close_bar)
        broker.close_position(result.ticket, "manual")
        trade = broker.history[-1]
        # 100 pips profit: (1.1100 - 1.1000) / 0.0001 = 100.0
        assert trade.pnl_pips == pytest.approx(100.0, abs=0.1)

    def test_balance_updated_after_close(self, broker, make_bar):
        """Balance should increase after a profitable close."""
        self._suppress_costs(broker)
        open_bar = make_bar(open_=1.1000)
        broker._get_next_bar = MagicMock(return_value=open_bar)
        result = broker.place_order("EURUSD", "buy", "market", 0.1)
        balance_after_open = broker.balance
        close_bar = make_bar(open_=1.1100, high=1.1100, low=1.1100, close=1.1100)
        broker._get_current_bar = MagicMock(return_value=close_bar)
        broker.close_position(result.ticket, "manual")
        assert broker.balance > balance_after_open


# ── TestEquityAndMargin ───────────────────────────────────────────────────────


class TestEquityAndMargin:
    """Equity = balance + unrealized PnL; margin checks on oversized orders."""

    def test_equity_equals_balance_with_no_positions(self, broker):
        """equity == balance when there are no open positions."""
        assert broker.equity == pytest.approx(broker.balance, abs=1e-6)

    def test_equity_includes_unrealized_pnl(self, broker, make_bar):
        """equity > balance when an open buy has current_price > entry_price."""
        open_bar = make_bar(open_=1.1000)
        broker._get_next_bar = MagicMock(return_value=open_bar)
        broker._calc_slippage = MagicMock(return_value=0.0)
        broker.data._estimate_spread.return_value = 0.0
        result = broker.place_order("EURUSD", "buy", "market", 0.1)
        # Simulate price increase
        pos = broker.positions[result.ticket]
        pos.current_price = 1.1100
        pos.unrealized_pnl = (1.1100 - pos.entry_price) * pos.lot * broker.config.contract_size
        assert broker.equity > broker.balance

    def test_free_margin(self, broker, make_bar):
        """free_margin = equity - used_margin for all open positions."""
        open_bar = make_bar(open_=1.1000)
        broker._get_next_bar = MagicMock(return_value=open_bar)
        broker._calc_slippage = MagicMock(return_value=0.0)
        broker._get_spread = MagicMock(return_value=0.0)
        result = broker.place_order("EURUSD", "buy", "market", 1.0)
        # fill_price = 1.1000 (spread=0, slippage=0)
        required = broker._calc_required_margin("EURUSD", 1.0, 1.1000)
        expected_free = broker.equity - required
        assert broker.free_margin() == pytest.approx(expected_free, abs=1e-4)

    def test_margin_check_rejects_oversized_order(self, broker, make_bar):
        """OrderResult(success=False, error='NOT_ENOUGH_MONEY') when free_margin < required."""
        open_bar = make_bar(open_=1.1000)
        broker._get_next_bar = MagicMock(return_value=open_bar)
        # Attempt to buy 1000 lots (way beyond available margin)
        result = broker.place_order("EURUSD", "buy", "market", lot=1000.0)
        assert result.success is False
        assert result.error == "NOT_ENOUGH_MONEY"

    def test_calc_required_margin(self, broker):
        """required_margin = (lot * contract_size * price) / leverage."""
        margin = broker._calc_required_margin("EURUSD", 1.0, 1.1000)
        expected = (1.0 * 100_000 * 1.1000) / 100
        assert margin == pytest.approx(expected, abs=1e-4)


# ── TestMarginCall ────────────────────────────────────────────────────────────


class TestMarginCall:
    """Margin call closes all positions when equity/balance drops below 50%."""

    def test_margin_call_closes_all_positions(self, broker, make_bar):
        """When equity / balance < 0.5, all positions are force-closed."""
        open_bar = make_bar(open_=1.1000)
        broker._get_next_bar = MagicMock(return_value=open_bar)
        broker._calc_slippage = MagicMock(return_value=0.0)
        broker.data._estimate_spread.return_value = 0.0
        result = broker.place_order("EURUSD", "buy", "market", 0.1)
        assert result.success is True
        # Simulate catastrophic loss: manually set balance so equity < 50% of balance
        pos = broker.positions[result.ticket]
        pos.current_price = 0.5000  # extreme loss
        pos.unrealized_pnl = (0.5000 - pos.entry_price) * pos.lot * broker.config.contract_size
        broker._handle_margin_call()
        assert len(broker.positions) == 0

    def test_margin_call_sets_close_reason(self, broker, make_bar):
        """Force-closed trades from margin call should have close_reason == 'margin_call'."""
        open_bar = make_bar(open_=1.1000)
        broker._get_next_bar = MagicMock(return_value=open_bar)
        broker._calc_slippage = MagicMock(return_value=0.0)
        broker.data._estimate_spread.return_value = 0.0
        result = broker.place_order("EURUSD", "buy", "market", 0.1)
        pos = broker.positions[result.ticket]
        pos.current_price = 0.5000
        pos.unrealized_pnl = (0.5000 - pos.entry_price) * pos.lot * broker.config.contract_size
        broker._handle_margin_call()
        assert len(broker.history) > 0
        assert broker.history[-1].close_reason == "margin_call"


# ── TestOvernightSwap ─────────────────────────────────────────────────────────


class TestOvernightSwap:
    """Overnight swap is applied to balance and position swap_total; 3x on Wednesday."""

    def _open_eurusd_buy(self, broker, make_bar):
        """Helper: open a EURUSD buy position."""
        open_bar = make_bar(open_=1.1000)
        broker._get_next_bar = MagicMock(return_value=open_bar)
        broker._calc_slippage = MagicMock(return_value=0.0)
        broker.data._estimate_spread.return_value = 0.0
        result = broker.place_order("EURUSD", "buy", "market", 1.0)
        return result.ticket

    def test_swap_applied_to_balance(self, broker, make_bar, clock):
        """apply_overnight_swap adds swap to balance and pos.swap_total."""
        ticket = self._open_eurusd_buy(broker, make_bar)
        balance_before = broker.balance
        # Monday (weekday=0) → multiplier = 1
        clock.now.return_value = datetime(2024, 1, 15, 22, 0, tzinfo=timezone.utc)  # Monday
        broker.apply_overnight_swap()
        assert broker.balance != balance_before
        pos = broker.positions[ticket]
        assert pos.swap_total != 0.0

    def test_swap_negative_for_buy(self, broker, make_bar, clock):
        """EURUSD buy swap_rate = -0.5 → balance decreases after swap."""
        ticket = self._open_eurusd_buy(broker, make_bar)
        balance_before = broker.balance
        clock.now.return_value = datetime(2024, 1, 15, 22, 0, tzinfo=timezone.utc)  # Monday
        broker.apply_overnight_swap()
        assert broker.balance < balance_before

    def test_triple_swap_wednesday(self, broker, make_bar, clock):
        """Wednesday (weekday=2) → swap multiplier = 3."""
        ticket = self._open_eurusd_buy(broker, make_bar)
        wednesday = datetime(2024, 1, 17, 22, 0, tzinfo=timezone.utc)  # Wednesday
        assert wednesday.weekday() == 2  # sanity check
        balance_before = broker.balance

        clock.now.return_value = wednesday
        broker.apply_overnight_swap()
        balance_after_wednesday = broker.balance
        wednesday_swap = balance_after_wednesday - balance_before

        # Open a new position to measure normal (1x) swap
        broker2_config = BrokerConfig(
            initial_balance=10_000.0,
            commission_per_lot=7.0,
            slippage_model="fixed",
            swap_rates={("EURUSD", "buy"): -0.5, ("EURUSD", "sell"): 0.2},
            fixed_slippage_pips=1.0,
            leverage=100,
            contract_size=100_000,
        )
        clock2 = MagicMock()
        clock2.now.return_value = datetime(2024, 1, 15, 10, 0, tzinfo=timezone.utc)
        data2 = MagicMock()
        data2._estimate_spread.return_value = 0.0
        from apps.api.src.agents.trader.core.broker import PaperBroker as PB

        broker2 = PB(broker2_config, clock2, data2)
        open_bar = make_bar(open_=1.1000)
        broker2._get_next_bar = MagicMock(return_value=open_bar)
        broker2._calc_slippage = MagicMock(return_value=0.0)
        broker2.place_order("EURUSD", "buy", "market", 1.0)
        balance_before2 = broker2.balance
        monday = datetime(2024, 1, 15, 22, 0, tzinfo=timezone.utc)
        clock2.now.return_value = monday
        broker2.apply_overnight_swap()
        normal_swap = broker2.balance - balance_before2

        assert abs(wednesday_swap) == pytest.approx(abs(normal_swap) * 3, abs=1e-6)

    def test_normal_swap_not_wednesday(self, broker, make_bar, clock):
        """Non-Wednesday days → swap multiplier = 1 (not 3)."""
        ticket = self._open_eurusd_buy(broker, make_bar)
        monday = datetime(2024, 1, 15, 22, 0, tzinfo=timezone.utc)
        assert monday.weekday() == 0  # sanity: Monday
        balance_before = broker.balance
        clock.now.return_value = monday
        broker.apply_overnight_swap()
        balance_after = broker.balance
        # swap should be swap_rate * lot * 1 (not 3)
        # swap_rate for EURUSD buy = -0.5; lot = 1.0 → swap = -0.5
        expected_swap = -0.5 * 1.0
        assert broker.balance == pytest.approx(balance_before + expected_swap, abs=1e-4)

    def test_swap_no_open_positions(self, broker, clock):
        """apply_overnight_swap does nothing if there are no open positions."""
        balance_before = broker.balance
        clock.now.return_value = datetime(2024, 1, 15, 22, 0, tzinfo=timezone.utc)
        broker.apply_overnight_swap()
        assert broker.balance == pytest.approx(balance_before, abs=1e-9)


# ── TestSlippageModels ────────────────────────────────────────────────────────


class TestSlippageModels:
    """Slippage calculations for fixed, volatility, and liquidity models."""

    def test_fixed_slippage(self, config, clock):
        """Fixed slippage = fixed_slippage_pips * pip_size."""
        data = MagicMock()
        data._estimate_spread.return_value = 0.00020
        config_fixed = BrokerConfig(
            initial_balance=10_000.0,
            commission_per_lot=7.0,
            slippage_model="fixed",
            swap_rates={},
            fixed_slippage_pips=1.0,
            leverage=100,
            contract_size=100_000,
        )
        from apps.api.src.agents.trader.core.broker import PaperBroker as PB

        broker = PB(config_fixed, clock, data)
        # EURUSD pip_size = 0.0001; fixed slippage = 1.0 * 0.0001 = 0.0001
        slippage = broker._calc_slippage("EURUSD", lot=0.1, direction="buy")
        assert slippage == pytest.approx(0.0001, abs=1e-8)

    def test_volatility_slippage(self, clock):
        """Volatility slippage = 5% of ATR."""
        data = MagicMock()
        data._estimate_spread.return_value = 0.00020
        config_vol = BrokerConfig(
            initial_balance=10_000.0,
            commission_per_lot=7.0,
            slippage_model="volatility",
            swap_rates={},
            fixed_slippage_pips=1.0,
            leverage=100,
            contract_size=100_000,
        )
        from apps.api.src.agents.trader.core.broker import PaperBroker as PB

        broker = PB(config_vol, clock, data)
        broker._get_recent_atr = MagicMock(return_value=0.0020)  # 20 pips ATR
        slippage = broker._calc_slippage("EURUSD", lot=0.1, direction="buy")
        # 5% of 0.0020 = 0.0001
        assert slippage == pytest.approx(0.0020 * 0.05, abs=1e-8)

    def test_liquidity_slippage_scales_with_lot(self, clock):
        """Liquidity slippage increases with larger lot size."""
        data = MagicMock()
        data._estimate_spread.return_value = 0.00020
        config_liq = BrokerConfig(
            initial_balance=10_000.0,
            commission_per_lot=7.0,
            slippage_model="liquidity",
            swap_rates={},
            fixed_slippage_pips=1.0,
            leverage=100,
            contract_size=100_000,
        )
        from apps.api.src.agents.trader.core.broker import PaperBroker as PB

        broker = PB(config_liq, clock, data)
        broker._get_recent_atr = MagicMock(return_value=0.0020)
        slip_small = broker._calc_slippage("EURUSD", lot=0.1, direction="buy")
        slip_large = broker._calc_slippage("EURUSD", lot=10.0, direction="buy")
        assert slip_large > slip_small


# ── TestPipSize ───────────────────────────────────────────────────────────────


class TestPipSize:
    """Pip size should be 0.0001 for 4-decimal pairs and 0.01 for JPY pairs."""

    def test_pip_size_eurusd(self, broker):
        """EURUSD pip_size = 0.0001."""
        assert broker._pip_size("EURUSD") == pytest.approx(0.0001, abs=1e-9)

    def test_pip_size_usdjpy(self, broker):
        """USDJPY pip_size = 0.01."""
        assert broker._pip_size("USDJPY") == pytest.approx(0.01, abs=1e-9)

    def test_pip_size_gbpusd(self, broker):
        """GBPUSD pip_size = 0.0001."""
        assert broker._pip_size("GBPUSD") == pytest.approx(0.0001, abs=1e-9)
