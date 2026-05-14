from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class TradeOrder(BaseModel):
    symbol: str
    direction: str  # buy, sell
    type: str = "MARKET"  # MARKET, LIMIT, STOP
    lot_size: float
    sl: float
    tp: float
    signal_id: str = ""
    price: Optional[float] = None


class OrderResult(BaseModel):
    ticket: int
    price: float
    volume: float
    retcode: int
    symbol: str = ""
    direction: str = ""


class TradeAction(BaseModel):
    action: str  # partial_close, modify_sl, modify_tp, close
    percent: Optional[float] = None
    move_sl_to: Optional[float] = None
    new_sl: Optional[float] = None
    new_tp: Optional[float] = None
