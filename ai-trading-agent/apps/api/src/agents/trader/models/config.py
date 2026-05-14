from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

load_dotenv()


class RiskConfig(BaseModel):
    risk_per_trade: float = 1.0       # 1% per trade
    daily_max_risk: float = 5.0       # -5% daily loss → STOP ALL
    daily_profit_target: float = 10.0 # +10% daily profit → STOP ALL
    max_positions: int = 2            # 1–2 active trades per pair
    max_trades_per_day: int = 25      # hard limit
    max_drawdown: float = 10.0
    consecutive_loss_pause: int = 3   # 3 losses → 1h pause
    tp1_close_pct: float = 40.0
    tp2_close_pct: float = 30.0
    tp3_close_pct: float = 30.0


class TradingConfig(BaseSettings):
    # MT5
    mt5_login: int = Field(default=0, env="MT5_LOGIN")
    mt5_password: str = Field(default="", env="MT5_PASSWORD")
    mt5_server: str = Field(default="Exness-MT5Real34", env="MT5_SERVER")

    # Trading
    symbol: str = Field(default="XAUUSD", env="SYMBOL")
    scan_interval: int = Field(default=30, env="SCAN_INTERVAL")

    # Risk
    risk_per_trade: float = Field(default=1.0, env="RISK_PER_TRADE")
    daily_max_risk: float = Field(default=5.0, env="DAILY_MAX_RISK")
    daily_profit_target: float = Field(default=10.0, env="DAILY_PROFIT_TARGET")
    max_positions: int = Field(default=2, env="MAX_POSITIONS")
    max_trades_per_day: int = Field(default=25, env="MAX_TRADES_PER_DAY")
    max_drawdown: float = 10.0

    # AI Brain
    claude_api_key: str = Field(default="", env="CLAUDE_API_KEY")
    min_ai_confidence: float = 0.70

    # Telegram
    telegram_bot_token: str = Field(default="", env="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str   = Field(default="", env="TELEGRAM_CHAT_ID")

    # Database / Socket
    database_url: str = Field(default="", env="DATABASE_URL")
    redis_url: str = Field(default="redis://localhost:6379", env="REDIS_URL")
    socket_url: str = Field(default="http://localhost:8000", env="SOCKET_URL")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    def get_risk_config(self) -> RiskConfig:
        return RiskConfig(
            risk_per_trade=self.risk_per_trade,
            daily_max_risk=self.daily_max_risk,
            daily_profit_target=self.daily_profit_target,
            max_positions=self.max_positions,
            max_trades_per_day=self.max_trades_per_day,
            max_drawdown=self.max_drawdown,
        )
