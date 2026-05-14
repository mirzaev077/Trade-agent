# OpenClaw Trading Agent — Claude Context

## Proyekt haqida
MetaTrader 5 + Claude API orqali ishlaydigan avtomatik ICT/MMXM trading bot.
**Birlamchi spec**: `agent.md` — to'liq arxitektura, modullar, UI flow.
**Haqiqiy implementatsiya**: `ai-trading-agent/` papkasidagi Python kodi (bu faylda ko'rsatilgan).

---

## Stack

| Layer | Texnologiya |
|---|---|
| **Backend** | Python 3.11+, FastAPI, pydantic-settings |
| **MT5** | MetaTrader5==5.0.5735 |
| **AI** | anthropic>=0.39.0 (claude-sonnet-4) |
| **Data** | pandas>=2.2.3, numpy>=2.1.0, ta>=0.11.0 |
| **Network** | python-socketio, aiohttp, uvicorn |
| **Logging** | loguru |
| **DB/Cache** | PostgreSQL + pgvector, Redis |
| **Frontend** | Next.js (dashboard, hali to'liq emas) |
| **Broker** | Exness (MT5 Demo/Real) |

---

## Haqiqiy fayl strukturasi

```
ai-agent/
├── agent.md                          ← Spetsifikatsiya (to'liq)
├── CLAUDE.md                         ← Bu fayl
└── ai-trading-agent/
    ├── requirements.txt
    ├── docker-compose.yml
    ├── setup.bat / start.bat
    └── apps/api/src/agents/trader/
        ├── agent.py                  ← TraderAgent (asosiy loop + tick logic)
        ├── mt5_connector.py          ← MT5 ulanish, order execution
        ├── analysis/
        │   ├── __init__.py
        │   └── ict.py               ← ICTAnalysis (35+ ICT level, yagona modul)
        ├── brain/
        │   ├── ai_validator.py      ← TradingAIBrain (Claude API)
        │   └── lessons.json         ← AI learning history
        ├── risk/
        │   └── manager.py           ← RiskManagement + PositionSizing
        ├── models/
        │   ├── config.py            ← TradingConfig, RiskConfig (pydantic)
        │   ├── signals.py           ← ICTSignal, TradeSignal, AIDecision, ...
        │   └── orders.py            ← TradeOrder, OrderResult
        └── utils/
            ├── session_times.py     ← Kill zone, session, news blackout, weekend
            └── timeframes.py        ← TF mapping
```

---

## Asosiy sinf: TraderAgent (`agent.py`)

**Konstantalar (o'zgartirma!):**
```python
PIP         = 0.10   # XAUUSD uchun 1 pip
SL_MIN_PIPS = 8
SL_MAX_PIPS = 50
TP_MIN_PIPS = 12
TP_MAX_PIPS = 150
RISK_PCT    = 0.005  # 0.5% per trade

TF_WEIGHTS = {"D1": 10, "H4": 8, "H1": 6, "M30": 4, "M15": 5, "M5": 3}
```

**Asosiy loop (`_tick`) qadamlari:**
1. Weekend / news blackout → skip
2. MT5 account refresh
3. Candles: D1 (150) → H4 (300) → H1 (500) → M30 (300) → M15 (500) → M5 (300)
4. ICT analysis har bir timeframe uchun
5. D1 bias → H4 → H1 fallback orqali `want_dir` aniqlash
6. Daily risk limits tekshirish (≥2% loss yoki ≥15 trades → manage only)
7. `_manage_pending_zones()` — mavjud limit orderlarni boshqarish
8. D1 valid zone bo'lsa → `_find_all_ict_zones()` → `_place_zone_limits()`
9. `_manage_positions()` — ochiq pozitsiyalar (partial close, trailing SL)

**Quality scoring (0–5):**
- `mmxm_ok` — H4/H1 da MMXM sweep bo'lgan
- `disp_ok` — M5/M15 da displacement
- `kz_ok` — hozir Kill Zone ichida
- `judas_ok` — H1/M15 da Judas swing
- `sh_ok` — H1/M15 da Stop Hunt

---

## ICT Analysis Engine (`analysis/ict.py`)

**35+ ICT konsept, yagona `ICTAnalysis` sinfi:**

| Kategoriya | Nima hisoblanadi |
|---|---|
| **Bloklar** | OB (Order Block), BB (Breaker Block), MB (Mitigation Block), RB (Rejection Block) |
| **Gap / Imbalance** | FVG, IFVG, BISI, SIBI, BPR (Balanced Price Range) |
| **Liquidity** | BSL, SSL, EQH/EQL, IRL/ERL, LiqVoid, Inducement |
| **Structure** | BOS, CHoCH, MSS, Displacement, CISD |
| **Model** | MMXM phase, Power of 3, Judas Swing, Stop Hunt |
| **Zones** | Premium/Discount, OTE, Dealing Range |
| **Session** | PDH/PDL, Asian Range, Kill Zone, Silver Bullet |
| **Patterns** | Reaccumulation, Redistribution, Trendlines |

`analyze(candles, timeframe)` → `ICTSignal` object qaytaradi.

---

## AI Brain (`brain/ai_validator.py`)

**Model**: `claude-sonnet-4-20250514` (yoki config'dagi versiya)

**Validation qoidalari (Claude system prompt):**
- H4 trend va signal yo'nalishi mos bo'lishi shart
- Buy → discount zone, Sell → premium zone
- SL H4 yoki H1 structural level'da bo'lishi shart
- TP real liquidity'ga yo'naltirilgan bo'lishi shart
- Min RR: 1.5:1
- Counter-trend faqat MMXM sweep + CHoCH bo'lganda

**Response format (JSON):**
```json
{
  "approved": true/false,
  "confidence": 0.0-1.0,
  "risk_level": 1-10,
  "reasoning": "...",
  "adjustments": {"entry": null, "sl": null, "tp1": null, "lot_size_multiplier": 1.0},
  "market_context": "...",
  "warnings": []
}
```

**Muhim**: `api_key` bo'lmasa → auto-approve (confidence=0.75), log warning.

---

## Risk Management (`risk/manager.py`)

**RiskConfig default qadriyatlari:**
```python
risk_per_trade  = 0.5   # % (agent.py da RISK_PCT = 0.005 bilan ham aniqlanadi)
daily_max_risk  = 2.0   # % (≥2% bo'lsa faqat manage mode)
max_positions   = 3
max_trades_per_day = 15
max_drawdown    = 10.0  # %
min_rr          = 1.5
tp1_close_pct   = 40.0  # TP1 da 40% yopish
tp2_close_pct   = 30.0
tp3_close_pct   = 30.0
```

**Position sizing formula:**
```
lot_size = (balance * risk%) / (SL_points * tick_value)
```

---

## TradingConfig — `.env` o'zgaruvchilari

```env
MT5_LOGIN=12345678
MT5_PASSWORD=yourpassword
MT5_SERVER=Exness-MT5Trial7
SYMBOL=XAUUSD
SCAN_INTERVAL=15          # soniya (default 15s)
RISK_PER_TRADE=0.5
DAILY_MAX_RISK=2.0
MAX_POSITIONS=3
MAX_TRADES_PER_DAY=15
CLAUDE_API_KEY=sk-ant-...
DATABASE_URL=postgresql://...
REDIS_URL=redis://localhost:6379
SOCKET_URL=http://localhost:8000
```

---

## Muhim qoidalar va cheklovlar

```
Demo first:       Real accountdan oldin 30 kun demo + 60% win rate
Min RR:           1.5:1 (AI validator ham tekshiradi)
Min AI conf:      0.65 (config'da min_ai_confidence)
Daily max loss:   2% (oshsa — faqat manage mode, yangi order yo'q)
Daily max trades: 15 ta
Max positions:    3 ta bir vaqtda
Max drawdown:     10% (circuit breaker)
Spread guard:     SL masofasining 30% dan oshsa → trade olmaydi
Weekend:          Juma 22:00 UTC → yangi order yo'q
News blackout:    High-impact news oldida/keyin window
Kill Zone:        London 08-11, NY 13-16, Asia 00-03 UTC
```

---

## Fibonacci levels (o'zgartirma!)

```
SL     = 0.170
Entry1 = 0.210   (primary entry)
Entry2 = 0.267   (secondary)
TP1    = 0.618
TP2    = 1.000
TP3    = 1.618
```

---

## MT5 Magic Number
```
20240101  → barcha OpenClaw orderlari shu magic bilan belgilanadi
Comment:  "OpenClaw|<signal_id>"
```

---

## Obsidian integration

Muhim qarorlar, baglar, g'oyalarni avtomatik saqlash:
```powershell
powershell -ExecutionPolicy Bypass -File ~\.claude\scripts\obsidian-note.ps1 decision ai-agent "<title>" "<body>"
powershell -ExecutionPolicy Bypass -File ~\.claude\scripts\obsidian-note.ps1 bug ai-agent "<title>" "<body>"
powershell -ExecutionPolicy Bypass -File ~\.claude\scripts\obsidian-note.ps1 idea ai-agent "<title>" "<body>"
powershell -ExecutionPolicy Bypass -File ~\.claude\scripts\obsidian-note.ps1 todo ai-agent "<title>" "<body>"
```

Vault joylashuvi: `~/Documents/Obsidian Vault/PROJECTS/ai-agent/`

---

## Ishga tushirish

```bash
# 1. Dependencies
pip install -r requirements.txt

# 2. .env sozlash
cp .env.example .env   # MT5 credentials + Claude API key

# 3. Agent ishga tushirish
python -m apps.api.src.agents.trader.agent

# yoki Windows batch
start.bat
```
