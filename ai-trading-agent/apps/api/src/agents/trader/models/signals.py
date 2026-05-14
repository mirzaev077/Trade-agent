from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
import uuid


class ICTSignal(BaseModel):
    signal_type: str = "none"   # none, buy, sell, strong_buy, strong_sell
    structure: dict = {}
    order_blocks: list = []
    breaker_blocks: list = []
    fvgs: list = []
    liquidity_zones: list = []
    kill_zone: str = "none"
    pd_zone: str = "neutral"
    confidence: float = 0.0


class TradePermission(BaseModel):
    allowed: bool
    checks: dict = {}
    reason: str = "OK"


class AIDecision(BaseModel):
    approved: bool
    confidence: float = 0.0
    risk_level: int = 5
    reasoning: str = ""
    adjustments: dict = {}
    market_context: str = ""
    warnings: list = []


class TradeSignal(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    symbol: str
    direction: str
    timeframe: str
    entry: float
    sl: float
    tp1: float
    tp2: Optional[float] = None
    tp3: Optional[float] = None
    rr_ratio: float = 0.0
    confluence_score: float = 0.0
    ict_analysis: Optional[ICTSignal] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

    def to_dict(self):
        return self.model_dump()
