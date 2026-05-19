# F2-3 Acceptance — XAUUSD 2024-2025 (24 oy)

**Sana:** 2026-05-19
**Run ID:** `47d34230-8801-41a3-9f54-f829fe3ab02f`
**Verdict:** ❌ **REJECT** (0 trade)

## Konfiguratsiya
- Symbol: XAUUSD (real MT5 data, 47,098 M15 + 11,788 H1 + 3,190 H4 bar)
- Range: 2024-01-01 → 2025-12-31
- Initial balance: $10,000 · Risk/trade: 1.0% · 1:2 RR
- Engine: MVP `ICTAnalyst` (H4 trend → H1 OB → PD-zone, 1:2 RR)
- Wall-clock: ~3 soat 4 minut

## Natijalar

| Metrika | Qiymat | Gate |
|---|---|---|
| total_trades | **0** | ≥ 100 ❌ |
| win_rate | n/a | CI low > 50% ❌ |
| profit_factor | n/a | > 1.5 ❌ |
| max_dd_pct | 0% | < 15% — (ma'noli emas, 0 trade) |
| final_balance | $10,000.00 | — |

**Verdict logikasi:** `n < 100` → REJECT (avtomatik, `analysis/performance.py`).

## Sabab — diagnostika (`scripts/probe_ict_gates.py`)

185 ta tasodifiy timestamp bo'yicha gate-by-gate sanash:

| Gate | Foiz | Izoh |
|---|---|---|
| H4 sideways | 47.6% | XAUUSD ko'p vaqt H4'da range'da |
| **price outside OB zone** | **49.7%** | ⚠️ asosiy qotil |
| no unmitigated OB | 2.2% | OB'lar bor (avg 5.8/sample) |
| pd_zone mismatch | 0.5% | yetib bormaydi |
| **signal emitted** | **0%** | 0/185 |

### Logika nuqsoni
`analyst_ict.py:181` da `max(strength)` orqali **eng kuchli OB** tanlanadi.
Lekin eng kuchli OB ko'pincha bir necha kun avval shakllangan — narx undan $20–$30 uzoqlashgan. Keyin `analyst_ict.py:189` `ob_bot <= price <= ob_top` shartini talab qiladi → hech qachon true bo'lmaydi.

**Misol** (probe sample):
```
2024-02-05 12:15 UTC  trend=bearish  OB=[$2055.24..$2057.62]  price=$2026.26  dist=−$30.17
```

### Live trader bilan farq
Live `TraderAgent._place_zone_limits` (`agent.py:1659`) **limit orderlar** qo'yadi
va narx zonaga qaytishini kutadi. Backtest MVP esa **market signal** chiqaradi va
narx allaqachon zonada bo'lishini talab qiladi → ikki xil paradigma.

## Tuzatish variantlari

| # | Variant | Effort | Faithful'lik |
|---|---|---|---|
| A | OB tanlashda `max(strength)` o'rniga `min(distance)` (eng yaqin OB) | 5 satr | past — MVP'ni patch |
| B | Signal'ga `is_limit=True` qo'shib PaperBroker'da limit order support | ~50 satr | o'rta |
| C | Live `_find_all_ict_zones` ni to'liq port qilish | ~200+ satr | yuqori — production parity |

## Tavsiya
F2-3 Acceptance gate'ni passlash uchun **B variant** eng pragmatik:
- Limit order paradigm production bilan to'g'ri keladi
- Engine + Signal'ga minimal o'zgarish
- F2-4 (demo natijalarini taqqoslash) uchun teng asos beradi

C varianti uzoqroq, lekin **F3 demo'gacha** zarur — chunki MVP analyst live xulqini to'liq aks ettirmaydi.

## Artefaktlar
- `reports/47d34230-8801-41a3-9f54-f829fe3ab02f.json` (raw result)
- `reports/47d34230-8801-41a3-9f54-f829fe3ab02f.html` (empty equity curve, 0 trade)
- `scripts/probe_ict_gates.py` (gate diagnostika)
- `data/historical/XAUUSD_{M15,H1,H4}.parquet` (~62k bar, qayta foydalanish mumkin)
