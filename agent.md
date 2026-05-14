# OpenClaw Trading Agent — To'liq Texnik Hujjat

> **MetaTrader 5 + Claude AI** orqali ishlaydigan institutional-grade ICT trading bot.
> Symbol: **XAUUSD** | Broker: **Exness** | Strategiya: ICT / SMC / MMXM

---

## Arxitektura — Umumiy Ko'rinish

```
┌─────────────────────────────────────────────────────────────────┐
│                        TraderAgent (agent.py)                   │
│                         Asosiy boshqaruvchi                     │
└──┬──────────┬──────────┬──────────┬──────────┬─────────────────┘
   │          │          │          │          │
   ▼          ▼          ▼          ▼          ▼
MT5        ICT        Brain      Risk       Specialist
Connector  Analysis   Modules    Manager    Pipeline (7 agent)
```

**Pipeline qadamlari (har tickda):**
```
1. Weekend / News blackout → skip
2. MT5: account + candles (W1→D1→H4→H1→M30→M15→M5)
3. ICTAnalysis: har TF uchun 35+ level hisoblash
4. HTFBiasAgent → D1+H4 alignment → SNIPER yoki FLOW mode
5. LiquidityHunterAgent → BSL/SSL/EQH/EQL/PDH/PDL map
6. ManipulationAgent → sweep tasdiqlash (NO SWEEP = NO TRADE)
7. EntryAgent → OB/FVG/BB/OTE zonalarni tanlash
8. SniperAgent yoki FlowAgent → mode scoring
9. ConfluenceAgent → final gate (0–20 ball)
10. SessionGuard → KZ/SB filter
11. AIBrain (Claude) → final approval
12. MT5: LIMIT order qo'yish
13. _manage_positions: partial close + trailing SL
```

---

## ICT Key Levels — Barcha 35+ Konsept

### Bloklar (Blocks)

| Level | Nima | Qanday ishlaydi |
|---|---|---|
| **OB** — Order Block | Institutional buy/sell boshlagan oxirgi teskari rangli candle | Narx qaytib kelganda entry. Bullish OB = bearish candle before bullish BOS. Bearish OB = bullish candle before bearish BOS |
| **BB** — Breaker Block | Avval mitigation qilingan va narx orqasiga qaytgan OB | Kuchli reversal zona, OB'dan ham ishonchli |
| **MB** — Mitigation Block | Narx bir marta test qilgan OB (weakened, lekin hali aktiv) | Ikkinchi test entry uchun ishlatiladi |
| **RB** — Rejection Block | Kuchli wick bilan rad etilgan zona (doji, pin bar) | Narx qaytib kelganda rejection zone sifatida ishlaydi |

### Gap va Imbalance

| Level | Nima | Qanday ishlaydi |
|---|---|---|
| **FVG** — Fair Value Gap | 3 candleda hosil bo'lgan bo'sh gap (candle[i-1].high < candle[i+1].low) | Narx bu gapni to'ldirmoqchi bo'ladi. Bullish FVG = buy entry, Bearish FVG = sell entry |
| **IFVG** — Inverse FVG | To'ldirilgan FVG keyin price orqasiga qaytadi | FVG to'lgandan keyin reversal signal |
| **BISI** — Buy-Side Imbalance, Sell-Side Inefficiency | Kuchli bullish displacement bilan hosil bo'lgan gap | Smart money buy qilgan zona — narx qaytib keladi |
| **SIBI** — Sell-Side Imbalance, Buy-Side Inefficiency | Kuchli bearish displacement bilan hosil bo'lgan gap | Smart money sell qilgan zona — narx qaytib keladi |
| **BPR** — Balanced Price Range | Bullish FVG + Bearish FVG kesishgan zona | Ikki tomonlama liquidity bor = kuchli zona |

### Liquidity (Likvidlik)

| Level | Nima | Qanday ishlaydi |
|---|---|---|
| **BSL** — Buy-Side Liquidity | Swing high'lardan yuqorida to'plangan stop loss buyurtmalar | Narx BSL'ni sweep qiladi → keyin SELL direction |
| **SSL** — Sell-Side Liquidity | Swing low'lardan pastda to'plangan stop loss buyurtmalar | Narx SSL'ni sweep qiladi → keyin BUY direction |
| **EQH** — Equal Highs | Bir xil darajadagi bir nechta high (trap zona) | Retail trader'lar bu highs'ni breakout deb o'ylaydi, smart money qaytaradi |
| **EQL** — Equal Lows | Bir xil darajadagi bir nechta low (trap zona) | EQH bilan bir xil, pastga yo'nalishda |
| **IRL** — Internal Range Liquidity | Range ichidagi FVG / OB zonalar | Kichik retracement maqsad |
| **ERL** — External Range Liquidity | Range tashqarisidagi swing high/low | Katta move maqsad (external target) |
| **LiqVoid** — Liquidity Void | Narx tez o'tib ketgan, hech qanday candle body yo'q zona | Narx bu bo'sh joyni qayta to'ldiradi |
| **Inducement** — Inducement | Kichik swing high/low keyingi katta sweep uchun bait | Smart money retail'ni bir yo'nalishga jalb qiladi, keyin sens yo'nalishda harakat |

### Struktura (Structure)

| Level | Nima | Qanday ishlaydi |
|---|---|---|
| **BOS** — Break of Structure | Narx oldingi swing high/low'dan o'tadi | Trend davom etish signali. Bullish BOS = buy, Bearish BOS = sell |
| **CHoCH** — Change of Character | Birinchi teskari BOS (trend o'zgarishi) | Reversal signali. Eng muhim struktura o'zgarishi |
| **MSS** — Market Structure Shift | CHoCH'dan kuchli tarkibiy o'zgarish | Institutional trend flip — CHoCH + displacement birgalikda |
| **Displacement** | Bir yoki bir necha kuchli impulse candle (FVG hosil qiladi) | Smart money harakati — sweep'dan keyin bo'lishi shart |
| **CISD** — Change in State of Delivery | Narx etkazib berish yo'nalishini o'zgartiradi | Internal structure break — kichik TF'da reversal belgisi |

### Model (Scenariy)

| Level | Nima | Qanday ishlaydi |
|---|---|---|
| **MMXM** — Market Maker Buy/Sell Model | To'liq institutional tsikl: Consolidation → Manipulation → Distribution → New Range | Narx hozir MMXM'ning qaysi fazasida ekanligini aniqlaydi. Swept = manipulation tugagan |
| **Power of 3 (PO3)** — AMD | Accumulation (to'plash) → Manipulation (fake move) → Distribution (real move) | Sessiya ichidagi narx harakati tuzilmasi |
| **Judas Swing** | Sessiya ochilishida yolg'on yo'nalishda harakat, keyin haqiqiy yo'nalish | London open'da ko'p: avval pastga (fake), keyin yuqoriga (real) |
| **Stop Hunt** | Key level'dan o'tib retail stop'larni olib, tezda qaytish | Kuchli reversal signal. Wick > 1.5× body bo'lsa = stop hunt |

### Zonalar (Premium/Discount)

| Level | Nima | Qanday ishlaydi |
|---|---|---|
| **Premium Zone** | Dealing range'ning yuqori yarmi (50% dan yuqori) | Bu yerda SELL qilinadi. Narx qimmat = smart money sotadi |
| **Discount Zone** | Dealing range'ning pastki yarmi (50% dan past) | Bu yerda BUY qilinadi. Narx arzon = smart money sotib oladi |
| **Equilibrium** | 50% Fibonacci — narx "adolatli qiymat" nuqtasi | Ne premium, ne discount — hech qanday entry yo'q |
| **OTE** — Optimal Trade Entry | Fibonacci 0.62–0.79 zonasi | Eng yaxshi entry nuqtasi. Swing retracement'da bu zona entry uchun |
| **Dealing Range** | Oxirgi CHoCH dan BOS gacha bo'lgan narx oralig'i | Premium/Discount hisoblash uchun asos |

### Sessiya Levellar

| Level | Nima | Qanday ishlaydi |
|---|---|---|
| **PDH** — Previous Day High | Kechagi kun'ning eng yuqori narxi | Kuchli liquidity target. BSL sifatida ishlaydi |
| **PDL** — Previous Day Low | Kechagi kun'ning eng past narxi | SSL sifatida ishlaydi. Narx PDL'ni sweep qilsa BUY signal |
| **Asian Range** — AR_HI / AR_LO | Asia sessiyasi (00:00–06:00 UTC) high va low | London/NY ochilishida bu range'dan chiqiladi → real direction |
| **Midnight Open** | 00:00 UTC narxi | Gunlik referans nuqtasi |
| **London Open** | 08:00 UTC narxi | London KZ boshlanishi — eng muhim sessiya narxi |
| **Kill Zone (KZ)** | Yuqori likvidli sessiya oynalari | Bu davrlarda smart money aktiv: London 08-11, NY 13-16, Asia 00-03 UTC |
| **Silver Bullet** | KZ ichidagi eng qisqa yuqori sifatli oyna | London 10-11, NY 14-15, Asia 02-04 UTC. Eng kuchli setup window |

### Pattern

| Level | Nima | Qanday ishlaydi |
|---|---|---|
| **Reaccumulation** | Trend yo'nalishida kichik konsolidatsiya, keyin davom | Bullish trendda → qayta buy qilish zonasi |
| **Redistribution** | Bearish trendda kichik konsolidatsiya | Bearish trendda → qayta sell qilish zonasi |
| **Trendlines** | Swing high/low'larni bog'lovchi chiziq | Dynamic support/resistance sifatida |

---

## Specialist Pipeline — 7 Agent

### 1. HTFBiasAgent

**Fayl:** `specialist/htf_bias.py`

**Maqsad:** Katta timeframe'lardan savdo yo'nalishini aniqlash.

**Qanday ishlaydi:**
```
W1 → D1 → H4 → H1 fallback zanjiri
Agar W1 "sideways" → D1 qaraladi
Agar D1 ham "sideways" → H4 qaraladi
Agar H4 ham "sideways" → H1 qaraladi
```

**Natija:**
- `bias`: "buy" | "sell" | "neutral"
- `mode`: **SNIPER** (D1 + H4 bir yo'nalishda) | **FLOW** (biri yoki ikkisi sideways)
- `confidence`: 0.0–1.0
  - D1 aligned → 0.45 ulush
  - H4 aligned → 0.30 ulush
  - W1 confirms → +0.12 bonus
  - Har BOS uchun → +0.04 bonus (max 0.20)

**Skilllar:**
- W1→D1→H4→H1 fallback chain
- SNIPER vs FLOW mode tanlash
- BOS va CHoCH hisobga olish
- W1 institutional bias tasdiqlash

---

### 2. LiquidityHunterAgent

**Fayl:** `specialist/liquidity_hunter.py`

**Maqsad:** Barcha likvidlik zonalarini xaritada ko'rsatish — narx qayerga ketishini bashorat qilish.

**Nima qidiradi:**
- BSL / SSL (buy-side va sell-side liquidity)
- EQH / EQL (equal highs/lows)
- PDH / PDL (previous day levels)
- AR_HI / AR_LO (Asian range)
- IRL / ERL (internal/external range liquidity)

**Vazn tizimi (Timeframe Weight):**
```
D1=3.5, H4=3.0, H1=2.5, M30=2.0, M15=1.5, M5=1.0, M1=0.7
Swept target → vazn × 2.2 (muhimroq)
PDH/PDL → vazn × 1.8
Asian Range → vazn × 1.4
```

**Natija:**
- `swept_targets`: allaqachon sweep bo'lgan pool'lar
- `pending_targets`: hali sweep bo'lmagan (narx borishi mumkin)
- `likely_next_sweep`: "up" | "down" — keyingi harakat bashorati

**Skilllar:**
- Multi-timeframe liquidity mapping
- Weight-based target ranking
- Next sweep direction prediction
- PDH/PDL + Asian Range tracking

---

### 3. ManipulationAgent

**Fayl:** `specialist/manipulation.py`

**Maqsad:** Likvidlik grab (sweep) ni tasdiqlash. **ASOSIY QOIDA: sweep yo'q = trade yo'q.**

**Qidirish tartibi:** M5 → M15 → M30 → H1 → H4 (eng yangi harakatdan boshlab)

**Nima aniqlaydi:**

| Tur | Kuch | Qanday |
|---|---|---|
| Stop Hunt | 0.90 | SSL/BSL sweep + tezda reversal |
| Judas Swing | 0.80 | Sessiya ochilishida yolg'on harakat |
| CHoCH + MMXM Sweep | 0.75–0.85 | Struktura flip + MMXM tugagan |
| MMXM Sweep (CHoCH'siz) | 0.65 | MMXM phase completed |
| Liquidity Pool Swept | 0.60 | Har qanday BSL/SSL swept |

**Natija:**
- `confirmed`: True/False
- `direction_after`: sweep'dan keyin savdo yo'nalishi
- `strength`: 0.0–1.0

**Skilllar:**
- Stop hunt detection (wick > 1.5× body)
- Judas swing identification
- MMXM phase completion check
- Multi-TF sweep confirmation

---

### 4. EntryAgent

**Fayl:** `specialist/entry.py`

**Maqsad:** Eng yaxshi entry zonani tanlash va aniqlash.

**Prioritet tartibi:**
```
OB > BB > OTE > FVG > IFVG > BPR > BISI > SIBI
```

**Qanday ishlaydi:**
- ICT'dan kelgan raw zonalarni filtrlab, vazn bo'yicha tartiblaydi
- SelfLearner'dan `entry_pct` oladi (default 30% — zone'ning 30% ichidan entry)
- HTF zone (D1/H4) va LTF zone (H1/M30/M15/M5) alohida ko'rsatadi

**Entry hisoblash:**
```python
# Buy: zone_lo + range * 0.30  (zone'ning pastki 30% qismidan)
# Sell: zone_hi - range * 0.30 (zone'ning yuqori 30% qismidan)
```

**Skilllar:**
- Zone quality ranking
- SelfLearner-adaptive entry precision
- HTF/LTF zone separation
- Dominant entry type detection

---

### 5. SniperAgent

**Fayl:** `specialist/sniper.py`

**Maqsad:** SNIPER mode'da (D1+H4 aligned) yuqori sifatli trend davomi tradelari.

**Faollashtirish:** Faqat `HTFBiasAgent.mode == "SNIPER"` bo'lganda.

**Scoring (max 10):**
```
HTF alignment:    max 4 ball (har aligned TF uchun 2 ball)
Liquidity sweep:  +2 ball
Displacement:     +2 ball (M1/M5 impulse)
OB/FVG zone:      +1 ball
Kill Zone:        +1 ball
```

**Minimum:** 7 ball kerak

**Parametrlar:**
```
Risk: 1% per trade
SL: 30–50 pip
TP1: 60 pip (2R)
TP2: 90 pip (3R)
TP3: 150 pip (5R)
```

**Skilllar:**
- Trend continuation trade selection
- HTF multi-timeframe alignment scoring
- High-probability entry validation

---

### 6. FlowAgent

**Fayl:** `specialist/flow.py`

**Maqsad:** FLOW mode'da (ranging/sideways market) manipulation zone tradelari.

**Faollashtirish:** `HTFBiasAgent.mode == "FLOW"` bo'lganda.

**Scoring (max 4):**
```
Liquidity sweep:  +1 ball (MAJBURIY — bo'lmasa avto-rad)
Displacement:     +1 ball (M5/M15 impulse)
OB/FVG zone:      +1 ball
Structure break:  +1 ball (BOS on H1/M15)
```

**Minimum:** 3 ball VA sweep = True (sweep yo'q bo'lsa ball qancha bo'lmasin — REJECT)

**Parametrlar:**
```
Risk: 1% per trade
SL: 30–50 pip
TP1: 60 pip (2R)
TP2: 90 pip (3R)
TP3: 120 pip (4R)
```

**Skilllar:**
- Range market trade filtering
- Mandatory sweep enforcement
- Micro-displacement detection

---

### 7. ConfluenceAgent

**Fayl:** `specialist/confluence.py`

**Maqsad:** Barcha agentlar natijalarini birlashtiruvchi **final gate** (oxirgi darvoza).

**Scoring tizimi (max 20):**

```
HTF Bias       (0–5):  D1=+2, H4=+2, H1=+1
Liquidity Sweep(0–5):  confirmed=+3, strength>0.75=+1, stop_hunt/judas=+1
Zone Quality   (0–5):  OB/FVG/BB/OTE=+2, KZ=+1, SB=+1, HTF depth=+1
Structure      (0–4):  BOS=+2, CHoCH=+1, Displacement=+1
DXY Correlation(0–1):  EURUSD H4 inversely confirms gold direction
```

**4 Ta Asosiy Shart (hammasi bo'lishi shart):**
1. HTF ≥ 2 ball (kamida H4 aligned)
2. Sweep > 0 (likvidlik grab tasdiqlangan)
3. Zone > 0 (OB/FVG/BB/OTE mavjud)
4. Structure > 0 (BOS yoki CHoCH bor)

**Minimumlar:**
```
SNIPER mode: ≥ 13/20
FLOW mode:   ≥ 10/20
```

**Gradlar:**
```
A+ = 18–20  |  A = 15–17  |  B = 12–14  |  C = 10–11  |  FAIL = <10
```

**Skilllar:**
- 4-pillar institutional validation
- DXY/EURUSD inverse correlation check
- Grade-based quality classification
- Mode-aware threshold adjustment

---

### 8. SessionGuard

**Fayl:** `specialist/session_guard.py`

**Maqsad:** Faqat yuqori ehtimollik sessiya oynalarida trade qilish.

**Sessiya oynalari (UTC):**
```
Asia KZ:            00:00 – 03:00  (min confluence +1, qiyinroq)
Silver Bullet Asia: 02:00 – 04:00  (min confluence -1, osonroq)
London Pre:         06:00 – 08:00  (faqat top setup)
London KZ:          08:00 – 11:00  ★ ENG YAXSHI
Silver Bullet London:10:00 – 11:00 ★★ ENG YUQORI
Overlap:            11:00 – 13:00  (normal)
NY KZ:              13:00 – 16:00  ★ ENG YAXSHI
Silver Bullet NY:   14:00 – 15:00  ★★ ENG YUQORI
NY Tail:            16:00 – 22:00  (ruxsat, +1 talabli)
Dead Zone:          22:00 – 00:00  BLOKLANGAN
```

**Quality Multiplier:**
```
Silver Bullet: 1.3× (min_conf -1)
London/NY KZ:  1.0× (normal)
Asia KZ:       0.9× (min_conf +1)
London Pre:    0.8× (min_conf +2)
NY Tail:       0.85× (min_conf +1)
Dead Zone:     0.0× (bloklangan)
```

**Skilllar:**
- Real-time session detection (UTC)
- Silver Bullet window identification
- Dynamic confluence threshold adjustment
- Dead zone enforcement

---

## Brain Modullari

### TradingAIBrain (Claude API)

**Fayl:** `brain/ai_validator.py`

**Model:** `claude-sonnet-4-20250514`

**Maqsad:** Har bir signal uchun Claude'dan final tasdiqlash olish.

**Non-Negotiable Qoidalar:**
```
1. RR ≥ 2.0 — pastroq bo'lsa RAD ETILADI
2. Counter-trend TAQIQLANGAN — H4 trend mos bo'lishi shart
3. Liquidity sweep MAJBURIY — sweep yo'q = rad
4. Entry OB/FVG/BB/OTE zonasidan bo'lishi shart
5. Noaniq setup → RAD ETILADI
```

**Tasdiqlash shartlari:**
```
1. H4 trend yo'nalishga mos (yoki CHoCH+BOS ikkisi bor)
2. SSL yoki BSL swept (liquidity grab bo'lgan)
3. Tozа zona: OB, FVG, BB, yoki OTE (mitigated emas)
4. BOS yoki CHoCH tasdiqlangan
5. RR ≥ 2.0:1
6. SL zona tashqarisida (buy: OB low'dan past; sell: OB high'dan yuqori)
7. Entry discount'da (buy) yoki premium'da (sell)
```

**Confidence tizimlari:**
```
0.90–1.00 → Mukammal: barcha qoidalar + MMXM + Silver Bullet
0.80–0.89 → Yaxshi: barcha qoidalar + sweep + zona tasdiqlangan
0.70–0.79 → O'rtacha: qoidalar bajarilgan, zaif tasdiqlash
< 0.70    → RAD ETILADI
```

**Javob formati (JSON):**
```json
{
    "approved": true/false,
    "confidence": 0.0-1.0,
    "risk_level": 1-10,
    "reasoning": "2 gapda tushuntirish",
    "adjustments": {
        "entry": null,
        "sl": null,
        "tp1": null,
        "lot_size_multiplier": 1.0
    },
    "market_context": "bozor holati",
    "warnings": []
}
```

**Fallback:** `api_key` bo'lmasa → auto-approve (confidence=0.75), log warning.

**Skilllar:**
- Institutional-grade signal validation
- SL/TP adjustment suggestions
- Risk level assessment (1–10)
- Context-aware reasoning

---

### SelfLearner

**Fayl:** `brain/self_learner.py`

**Maqsad:** Trade natijalaridan o'rganib, setup og'irliklari va entry precision'ni avtomatik moslash.

**Qanday ishlaydi:**
- Har 20 tradedan keyin → **Analyze**: zaif setup'larni aniqlash
- Har 50 tradedan keyin → **Evolve**: setup og'irliklarni moslash

**Nimani moslashtiradi:**
```python
setup_weights = {
    "OB": 1.0,   # Order Block (yaxshi ishlasa → 2.0 gacha, yomon → 0.1 gacha)
    "FVG": 1.0,  # Fair Value Gap
    "BB": 1.0,   # Breaker Block
    "OTE": 1.0,  # Optimal Trade Entry
    "IFVG": 1.0, "BISI": 1.0, "SIBI": 1.0, "BPR": 1.0,
    "PDH": 1.0, "PDL": 1.0, "CISD": 0.9, "LiqVoid": 0.9,
    # ...
}
entry_pct: 0.30     # Entry precision (0.30 = zone'ning 30%'idan entry)
sl_multiplier: 1.0  # SL kengligi
```

**Session/Day statistikasi:**
```json
"session_day_stats": {
    "london_monday": {"wins": 5, "losses": 2},
    "ny_tuesday": {"wins": 3, "losses": 1}
}
```

**Natija:** `learned.json` faylida saqlanadi.

**Skilllar:**
- Adaptive setup weight adjustment
- Entry precision optimization
- Session+day win rate tracking
- Setup disabling (consistently losing setups)

---

## ICT Analysis Engine

**Fayl:** `analysis/ict.py`

**Maqsad:** Berilgan OHLCV candles'dan 35+ ICT level hisoblash.

**`analyze(candles, timeframe)` → `ICTSignal`**

Hisoblash ketma-ketligi:
```python
structure    = market_structure(candles)      # BOS, CHoCH, trend
obs          = order_blocks(candles)           # OB zonalar
breakers     = breaker_blocks(candles, obs)    # BB (mitigated OB)
mitigations  = mitigation_blocks(candles, obs) # MB
rejections   = rejection_blocks(candles)       # RB (wick zonalar)
fvgs         = fair_value_gaps(candles)        # FVG
ifvgs        = inverse_fvgs(candles, fvgs)     # IFVG
bisi_sibi    = bisi_sibi(candles)              # Imbalance
bpr          = balanced_price_range(fvgs)       # BPR
liquidity    = liquidity_pools(candles)         # BSL/SSL
liq_voids    = liquidity_voids(candles)         # LiqVoid
inducement   = inducement(candles)              # Inducement
eq_zones     = equal_highs_lows(candles)        # EQH/EQL
dealing_rng  = dealing_range(candles)           # Dealing Range
pd_zone      = premium_discount(candles)        # P/D zone
mmxm         = mmxm_phase(candles)              # MMXM model
displacement = displacement(candles)            # Displacement
cisd         = cisd(candles)                    # CISD
judas        = judas_swing(candles)             # Judas Swing
stop_hunt    = stop_hunt(candles)               # Stop Hunt
reaccum      = reaccumulation(candles)          # Reaccumulation
pdh_pdl      = pdh_pdl(candles)                # PDH/PDL
asian_range  = asian_range(candles)             # Asian Range
ote          = ote_zone(candles)                # OTE 0.62–0.79
po3          = power_of_3(candles)              # PO3 / AMD
silver       = silver_bullet()                  # Silver Bullet check
trendlines   = trendlines(candles)              # Dynamic trendlines
```

**ICTSignal tarkibi:**
```python
signal_type:     "strong_buy" | "buy" | "sell" | "strong_sell" | "neutral"
confidence:      0.0–1.0
order_blocks:    List[dict]    # OB + BB + MB + RB
fvg_zones:       List[dict]    # FVG + IFVG + BISI + SIBI + BPR
liquidity_zones: List[dict]    # BSL + SSL + EQH + EQL + IRL + ERL
structure: {
    trend:            "bullish" | "bearish" | "sideways"
    bos:              int (BOS soni)
    choch:            bool
    mmxm:             "accumulation" | "manipulation" | "distribution" | "new_range"
    mmxm_swept:       bool
    pd_zone:          "premium" | "discount" | "equilibrium"
    displacement:     bool
    stop_hunt:        bool
    judas:            bool
    pdh:              float
    pdl:              float
    asian_high:       float
    asian_low:        float
}
```

---

## Risk Management

**Fayl:** `risk/manager.py`

**Position sizing formulasi:**
```
lot_size = (balance × risk%) ÷ (SL_points × tick_value)
```

**Default limitlar:**
```
RISK_PCT:          1% per trade
DAILY_MAX_RISK:    2% (oshsa → faqat manage mode)
MAX_POSITIONS:     3 ta bir vaqtda
MAX_TRADES_PER_DAY: 15 ta
MAX_DRAWDOWN:      10% (circuit breaker)
SPREAD_GUARD:      SL masofasining 30% dan ko'p bo'lsa → trade yo'q
```

**Partial Close strategiyasi:**
```
TP1 (60 pip) hit → 40% yopish, SL → Break Even
TP2 (90 pip) hit → 30% yopish, SL → TP1
TP3 (120/150 pip) → qolgan 30% yopish
```

**Trailing Stop:**
```
TP1 dan keyin: ATR × 1.5 trailing
TP2 dan keyin: ATR × 1.0 trailing
```

**Qo'shimcha tekshiruvlar:**
- Cooldown: bir yo'nalishda oxirgi orderdan 60 daqiqa o'tishi shart
- Weekend: Juma 22:00 UTC → yangi order yo'q
- News Blackout: High-impact news oldida/keyin window

---

## MT5 Connector

**Fayl:** `mt5_connector.py`

**Maqsad:** MetaTrader 5 bilan to'liq aloqa.

**Skilllar:**

| Funksiya | Nima qiladi |
|---|---|
| `connect(login, password, server)` | MT5'ga ulanish (Demo/Real) |
| `get_candles(symbol, tf, count)` | OHLCV ma'lumot olish (W1 dan M1 gacha) |
| `get_account_info()` | Balance, equity, margin |
| `place_order(order)` | LIMIT yoki MARKET order qo'yish |
| `modify_position(ticket, sl, tp)` | Ochiq pozitsiyada SL/TP o'zgartirish |
| `partial_close(ticket, percent)` | Pozitsiyani qisman yopish |
| `close_position(ticket)` | Pozitsiyani to'liq yopish |
| `get_open_positions()` | Ochiq pozitsiyalar ro'yxati |
| `get_pending_orders()` | Kutilayotgan orderlar |
| `cancel_order(ticket)` | Pending orderni bekor qilish |

**Magic Number:** `20240101` — barcha OpenClaw orderlari shu raqam bilan belgilanadi
**Comment:** `"OpenClaw|<signal_id>"` — barcha orderlarda

---

## TraderAgent — Asosiy Boshqaruvchi

**Fayl:** `agent.py`

**Modlar:**

| Mod | Shartlari | Risk | SL | TP1 | TP2 | TP3 |
|---|---|---|---|---|---|---|
| **SNIPER** | D1 + H4 aligned | 1% | 30–50 pip | 60 pip | 90 pip | 150 pip |
| **FLOW** | D1 yoki H4 sideways | 1% | 30–50 pip | 60 pip | 90 pip | 120 pip |

**Timeframe og'irliklari:**
```python
TF_WEIGHTS = {"D1": 10, "H4": 8, "H1": 6, "M30": 4, "M15": 3, "M5": 2}
```

**Candle sonlari (har tickda olinadi):**
```
W1:  52 candles (1 yil)
D1:  150 candles
H4:  300 candles
H1:  500 candles
M30: 300 candles
M15: 500 candles
M5:  300 candles
```

**Daily limitlar:**
- Kunlik yo'qotish ≥ 2% → yangi order yo'q, faqat manage
- 15 ta trade → yangi order yo'q
- 10% drawdown → circuit breaker, agent to'xtaydi

**Himoya:**
- `_used_zones`: bir zonadan faqat 1 ta order
- `_last_order_at`: 60 min cooldown har yo'nalishda
- `_touched_ranges`: virgin zone tracking (birinchi marta test qilinayotgan zona)
- `_pause_until`: streak'dan keyin vaqtincha to'xtatish

---

## Ishga Tushirish

```bash
# 1. Dependencies
pip install -r requirements.txt

# 2. .env sozlash
MT5_LOGIN=12345678
MT5_PASSWORD=yourpassword
MT5_SERVER=Exness-MT5Trial7
SYMBOL=XAUUSD
CLAUDE_API_KEY=sk-ant-...

# 3. Agent ishga tushirish
python -m apps.api.src.agents.trader.agent

# yoki Windows
start.bat
```

---

## Xavfsizlik Qoidalari

```
Demo First:       Real accountdan oldin 30 kun demo + 60% win rate
Min RR:           2.0:1 (AI validator majburiy tekshiradi)
Min AI Confidence: 0.70 (past bo'lsa → rad)
Daily Max Loss:   2% (oshsa → manage only mode)
Max Positions:    3 ta bir vaqtda
Max Drawdown:     10% (circuit breaker)
Spread Guard:     SL masofasining 30% dan oshsa → trade yo'q
Weekend:          Juma 22:00 UTC → yangi order yo'q
News Blackout:    High-impact news oynasida blok
Kill Zone Only:   Faqat London/NY/Asia KZ da full trade
Dead Zone:        22:00–00:00 UTC — hech qanday order
NO SWEEP:         Liquidity grab bo'lmasa → aslo trade yo'q
```

---

*OpenClaw AI Office — XAUUSD Institutional Trading Bot*
