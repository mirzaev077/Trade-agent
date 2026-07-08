"""
TradingConfig + RiskConfig — Pydantic v2 strict validation.

F1-2: Silent fallback olib tashlandi. Noto'g'ri env qiymat (masalan
RISK_PER_TRADE=abc) startupda ValidationError'ga olib keladi — degraded
operatsiya bo'lishi mumkin emas.

Maydon chegaralari (Field gt/ge/le) — typo'larga qarshi soft-guardrail.
Cross-field invariants @model_validator orqali tekshiriladi:
  • daily_max_risk    >= risk_per_trade   (Tasks.md F1-2 talabi)
  • max_drawdown      >= daily_max_risk   (DD breaker daily breaker'dan kichik bo'lmaydi)
  • tp1+tp2+tp3       ≈ 100               (partial close 100% chiqishi shart)

Pydantic v2 BaseSettings env vars'ni avtomatik field nomi bo'yicha
(case-insensitive) mapping qiladi — `env=` parametri kerak emas.
`extra='ignore'` — `.env`'dagi yot o'zgaruvchilarni (OPENCLAW_STATE_DIR,
AUTO_APPLY_LEARNING va h.k.) qabul qilmasdan jim o'tkazib yuborish.
"""
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv

load_dotenv()


class RiskConfig(BaseModel):
    """Pure-domain risk parametrlari — TradingConfig.get_risk_config() qaytaradi."""

    model_config = ConfigDict(extra="forbid", validate_default=True)

    risk_per_trade:         float = Field(1.0,  gt=0,  le=5.0)   # % per trade
    daily_max_risk:         float = Field(5.0,  gt=0,  le=10.0)  # % daily loss circuit
    daily_profit_target:    float = Field(10.0, gt=0,  le=50.0)  # % daily profit auto-stop
    max_positions:          int   = Field(2,    ge=1,  le=10)    # concurrent trades
    max_trades_per_day:     int   = Field(25,   ge=1,  le=100)   # daily trade cap
    max_drawdown:           float = Field(10.0, gt=0,  le=50.0)  # % session DD breaker
    consecutive_loss_pause: int   = Field(3,    ge=1,  le=20)    # losses before pause
    tp1_close_pct:          float = Field(40.0, ge=0,  le=100)
    tp2_close_pct:          float = Field(30.0, ge=0,  le=100)
    tp3_close_pct:          float = Field(30.0, ge=0,  le=100)

    @model_validator(mode="after")
    def _check_invariants(self) -> "RiskConfig":
        if self.daily_max_risk < self.risk_per_trade:
            raise ValueError(
                f"daily_max_risk ({self.daily_max_risk}%) must be >= "
                f"risk_per_trade ({self.risk_per_trade}%)"
            )
        if self.max_drawdown < self.daily_max_risk:
            raise ValueError(
                f"max_drawdown ({self.max_drawdown}%) must be >= "
                f"daily_max_risk ({self.daily_max_risk}%)"
            )
        tp_sum = self.tp1_close_pct + self.tp2_close_pct + self.tp3_close_pct
        if abs(tp_sum - 100.0) > 0.1:
            raise ValueError(
                f"tp1+tp2+tp3 must sum to 100% (got {tp_sum:.2f}%)"
            )
        return self


class TradingConfig(BaseSettings):
    """
    Bot konfiguratsiyasi — `.env`'dan o'qiladi (pydantic-settings v2).
    Noto'g'ri qiymat → ValidationError startupda.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",          # .env'da yot vars (OPENCLAW_*, AUTO_APPLY_*) bo'lishi mumkin
        validate_default=True,   # default qiymatlar ham bounds'ga mos kelishi shart
    )

    # ── MT5 credentials — 0/empty = "not configured" (main.py setup tekshiradi) ──
    mt5_login:    int = Field(0,  ge=0)
    mt5_password: str = Field("")
    mt5_server:   str = Field("Exness-MT5Real34", min_length=1)

    # ── T-02: EXECUTION_MODE hard safety switch ('demo'|'live') ──
    # Default 'demo' = mavjud xulq. REAL MT5 account (trade_mode==Real)
    # bilan ulanishda execution_mode 'live' bo'lmasa connect()/order RAD
    # etiladi (agent.py guard). Demo/sim account'da guard inert (back-compat).
    execution_mode: str = Field("demo")

    # ── Trading ──
    symbol:        str = Field("XAUUSD", min_length=3)
    scan_interval: int = Field(30, ge=1, le=600)

    # ── Risk — bounds RiskConfig bilan parallel ──
    risk_per_trade:      float = Field(1.0,  gt=0, le=5.0)
    daily_max_risk:      float = Field(5.0,  gt=0, le=10.0)
    daily_profit_target: float = Field(10.0, gt=0, le=50.0)
    max_positions:       int   = Field(2,    ge=1, le=10)
    max_trades_per_day:  int   = Field(25,   ge=1, le=100)
    max_drawdown:        float = Field(10.0, gt=0, le=50.0)   # F1-2 fix: avval env binding yo'q edi

    # ── F5-3: High-vol regime gate (engine/regime.py RegimeGate) ──
    # M15 ATR% (ATR/price*100) shu chegaradan oshsa → mo'rt reversion
    # setuplar (M15_OB/BB/CISD) bloklanadi. 2025-Q2 (yuqori-vol trend)
    # halokati himoyasi. 0.0 = o'chirilgan (back-compat); jonli production
    # qiymati 0.18 (calibrated, engine PRODUCTION_REGIME_ATR bilan bir xil).
    regime_atr_threshold: float = Field(0.0, ge=0.0, le=5.0)
    regime_atr_period:    int   = Field(14,  ge=1,  le=200)

    # ── F5-4: Setup-level block list (.env BLOCKED_SETUPS, vergul bilan) ──
    # Bu label'lardagi setuplar jonli botda UMUMAN savdo qilinmaydi (regime'dan
    # qat'i nazar), backtest DEFAULT_BLOCKED_SETUPS mexanizmiga o'xshash.
    # Default bo'sh = hech narsa bloklanmaydi (back-compat). Jonli .env'da
    # "M15_OB" (past-WR setup, foydalanuvchi qarori 2026-06-20).
    blocked_setups: str = Field("")

    # ── T-03: per-order lot hard cap. 0.0 = o'chiq (back-compat).
    # >0 bo'lsa sizing + BE re-entry + AI multiplier'dan KEYIN
    # lot_each = min(lot_each, max_lot_cap) (agent.py _place_zone_limits).
    # Eslatma: cap vol_min (0.01) dan past bo'lmasin (broker reject).
    max_lot_cap: float = Field(0.0, ge=0.0, le=500.0)

    # ── Market-near-zone entry (limit o'rniga darhol market) ──
    # Joriy narx zona entry'siga shu pip-masofa ICHIDA bo'lsa, bot pending
    # limit qo'yish o'rniga DARHOL market bilan kiradi (narx zonaga deyarli
    # yetgan — kutishga hojat yo'q, limit fill'ni o'tkazib yubormaslik uchun).
    # 0.0 = o'chiq (faqat limit, eski xulq). Jonli tavsiya: 5.0 pip (XAUUSD:
    # 5 pip = $0.50 erta kirish). Katta qiymat = ko'proq market, kamroq
    # limit — RR biroz yomonlashadi, fill ehtimoli oshadi.
    market_near_pips: float = Field(5.0, ge=0.0, le=50.0)

    # ── AI Brain ──
    claude_api_key:    str   = Field("")
    min_ai_confidence: float = Field(0.70, ge=0.0, le=1.0)    # F1-2 fix: avval env binding yo'q edi

    # ── Telegram (optional — bo'sh = notify off) ──
    telegram_bot_token: str = Field("")
    telegram_chat_id:   str = Field("")

    # ── Database / Socket ──
    database_url: str = Field("")
    redis_url:    str = Field("redis://localhost:6379")
    socket_url:   str = Field("http://localhost:8000")

    @field_validator("execution_mode", mode="before")
    @classmethod
    def _normalize_execution_mode(cls, v):
        # Case/space-insensitive: 'Demo', ' LIVE ' → 'demo'/'live'.
        s = str(v).strip().lower()
        if s not in {"demo", "live"}:
            raise ValueError(
                f"execution_mode must be 'demo' or 'live' (got {v!r})"
            )
        return s

    @model_validator(mode="after")
    def _check_invariants(self) -> "TradingConfig":
        if self.daily_max_risk < self.risk_per_trade:
            raise ValueError(
                f"daily_max_risk ({self.daily_max_risk}%) must be >= "
                f"risk_per_trade ({self.risk_per_trade}%)"
            )
        if self.max_drawdown < self.daily_max_risk:
            raise ValueError(
                f"max_drawdown ({self.max_drawdown}%) must be >= "
                f"daily_max_risk ({self.daily_max_risk}%)"
            )
        return self

    def get_blocked_setups(self) -> set[str]:
        """F5-4: `.env BLOCKED_SETUPS` ("M15_OB,H1_OB") → normalized set.

        Bo'sh/oraliq probellar tashlanadi; bo'sh string → bo'sh set.
        """
        return {s.strip() for s in self.blocked_setups.split(",") if s.strip()}

    def get_risk_config(self) -> RiskConfig:
        return RiskConfig(
            risk_per_trade=self.risk_per_trade,
            daily_max_risk=self.daily_max_risk,
            daily_profit_target=self.daily_profit_target,
            max_positions=self.max_positions,
            max_trades_per_day=self.max_trades_per_day,
            max_drawdown=self.max_drawdown,
        )
