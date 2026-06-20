# ai-trade-agent — Live Trading Yo'l Xaritasi

> **Asosiy maqsad:** Bot'ni real pul bilan **xavfsiz ishga tushirish**.
> **Hozirgi holat:** 6/10 live-ready — strategiya bor, lekin restart/disconnect/crash bo'shliqlari mavjud.
> **Reja:** 4 faza, taxminan 3-4 hafta demo + 1 hafta real (kichik kapital).
> **TMAS'dan ko'chirilayotgan ishlar:** Bu maqsadga xizmat qilganlari — hammasi emas.

---

## 📊 Hozirgi holatning xulosasi

### ✅ Allaqachon ishlaydi
- Daily max loss circuit breaker (`risk/manager.py:68-71`, 5.0%)
- Max drawdown breaker (`agent.py:199-211`, 10.0%)
- Weekend / news blackout (`agent.py:173-195`)
- Max positions limit (`agent.py:426`, 2 ta)
- Consecutive loss pause (`agent.py:2074-2080`, 5 zarar → 1h)
- Magic number filtering (`mt5_connector.py:212`, `20240101`)
- Telegram trade alerts (`telegram_bot.py`, 7 ta notify funksiya)
- Trade journal (`trades.csv`, 18 ustun)
- Hot reload (`main.py:269`)

### ✅ F0'da hal qilindi (2026-05-14, Done.md ga ko'chirildi)
1. ~~Restart xavfsizligi yo'q~~ → `state/persistence.py` + 9 ta save call
2. ~~MT5 disconnect handling yo'q~~ → `ensure_connected()` exponential backoff
3. ~~Crash Telegram alert yo'q~~ → `sys.excepthook` + 4 ta sync notify
4. ~~News paytida pending orders bekor qilinmaydi~~ → `_cancel_all_pending_orders()` idempotent
5. ~~`lessons.json` vs `learned.json` konflikti~~ → 3-bosqichli priority + mtime cache

### ✅ F1'da hal qilindi (2026-05-14, commit b95d01c)
6. ~~Self-learner avto-mutation~~ → `state/db.py` + `tools/admin_cli.py` approval gate, `AUTO_APPLY_LEARNING=false` default
7. ~~Config silent fallback~~ → Pydantic v2 strict (Field bounds + `@model_validator`), `RISK_PER_TRADE=abc` → ValidationError
8. ~~0 ta unit test~~ → `tests/unit/test_smoke.py` 22 test + 132 TMAS port test + pyproject.toml ruff/pytest config

### 🔴 Hali qoldi
- ~~F1-1 oxirgi test: 50-trade end-to-end scenario~~ → **2026-05-23 yakunlandi**: `tests/unit/test_self_learner_50_trades.py` (3 ta test, 284/284 pytest pass)
- ~~**F2-3.1 ICTAnalyst limit-order fix**~~ → **2026-06-20 YAKUNLANDI.** Variant B+C port qilindi; 24-oy v5rr acceptance TUGADI: **3015 savdo, net +$644.23, 7 WARN / 1 REJECT** (q2_2025). Formal validation (2026-06-09): robust+safe, low-edge, demo-ready. Batafsil: F2-3.1 bo'limi quyida.

### 🟡 Live'ga halaqit qilmaydi, lekin operatsion xavfli
- Healthcheck endpoint yo'q
- `.env.example` da `TELEGRAM_*`, `CLAUDE_API_KEY` yo'q
- Heartbeat / watchdog yo'q
- 36 ta `datetime.utcnow()` chaqiruvi (testability + look-ahead)

---

## ✅ FAZA 0 — Yakunlandi (2026-05-14)

> 5/5 task bajarildi, 68/68 test passed. Batafsil: **`Done.md`**.

Quyidagi muammolar hal qilindi:
- Restart safety (`state/persistence.py`)
- MT5 disconnect + reconnect (`ensure_connected()` exponential backoff)
- Crash/disconnect/shutdown Telegram alerts (sys.excepthook + 4 sync notify)
- News blackout'da pending orders'ni idempotent cancel
- Self-learner `lessons.json` priority (mtime cache, 3-bosqich)

**Qoldi:** Demo'da real stress test (WiFi uzish, restart, sun'iy crash) — F3 (deployment) fazasida.

---

## 🛡 FAZA 1 — Real pul oldidan safety upgrades (1 hafta)

### F1-1: Self-learner admin approval gate 🟠
> **TMAS pattern:** `pending_adjustments` jadvali + admin CLI

- [x] `migrations/002_pending_adjustments.sql`:
  - `schema_migrations` versioning jadvali (idempotent)
  - `pending_adjustments`: id (UUID), proposed_at, param, old_value, new_value, reason, status, expires_at (24 soat)
  - `active_adjustments`: id, approved_at, param, value, applied_by
- [x] `brain/self_learner.py`:
  - `evolve()` endi `learned.json` ga to'g'ridan-to'g'ri yozmaydi
  - `propose_adjustment(param, old, new, reason)` — `pending_adjustments` ga yozadi
  - `AUTO_APPLY` env flag default `False` (back-compat'siz buzilmaydi)
- [x] `tools/admin_cli.py` (yangi):
  - `python -m tools.admin_cli list` — pending'lar
  - `... approve <id>` — `active_adjustments` ga ko'chirish + `learned.json` yangilash
  - `... reject <id> --reason "..."` 
  - `... auto-reject` — 24 soat o'tganlarni
- [x] Telegram: yangi `pending_adjustment` paydo bo'lsa admin'ga xabar
- [x] **Test:** 50 trade'dan keyin `learned.json` o'zgarmasligini, lekin `pending_adjustments` ga yozilishini tekshirish. *(2026-05-23: `tests/unit/test_self_learner_50_trades.py` — 3 ta scenario: gate ON learned.json unchanged + DB pending + Telegram notify; AUTO_APPLY=true learned.json mutated + audit; admin_cli approve → learned.json yangilanadi)*

### F1-2: Config validation — silent fallback'ni o'chirish 🟠
> **TMAS pattern:** `src/tmas/core/settings.py` (pydantic v2, ValidationError)

- [x] `models/config.py`:
  - `TradingConfig`'ga `model_config = ConfigDict(validate_default=True, extra='forbid')`
  - Sonli maydonlarga chegaralar: `risk_per_trade: float = Field(0.5, gt=0, le=5.0)`, `daily_max_risk: float = Field(2.0, gt=0, le=10.0)`, `max_positions: int = Field(2, ge=1, le=10)`
  - `@model_validator(mode='after')` invariant: `daily_max_risk >= risk_per_trade`
- [x] **Test:** `RISK_PER_TRADE=abc` bilan ishga tushirib, ValidationError olish.

### F1-3: `.env.example` to'liqlash 🟠
- [x] Hozir bor: `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, `SYMBOL`
- [x] Qo'shish: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `CLAUDE_API_KEY`, `EXECUTION_MODE=demo`, `AUTO_APPLY_LEARNING=false`, `MAX_DAILY_LOSS_PCT=2.0`, `MAX_DRAWDOWN_PCT=10.0` *(11→19 key; `DAILY_MAX_RISK`/`MAX_DRAWDOWN` nomi ishlatilgan)*
- [x] `start.bat`'da `.env` mavjudligini tekshirish, yo'q bo'lsa `.env.example` ni nusxa qilib o'tish va admin'ga aniqlik kiritishi haqida xabar

### F1-4: Healthcheck endpoint + watchdog 🟠
- [x] `apps/api/src/agents/trader/health.py` (FastAPI):
  - `GET /health` — `{status, uptime_sec, mt5_connected, last_tick_age_sec, open_positions, daily_pnl_pct}`
  - `GET /health/live` — Kubernetes liveness (bot loop hayotmi?)
  - `GET /health/ready` — Kubernetes readiness (MT5 ulanganmi?)
- [x] `main.py` da FastAPI'ni alohida thread'da ishga tushirish (port 8080)
- [x] Tashqi watchdog: `scripts/watchdog.ps1` — har 60 soniyada `/health` ni chaqirish, javob yo'q bo'lsa Telegram alert
- [x] **Test:** `curl localhost:8080/health` natija olish.

### F1-5: Smoke testlar + pyproject + lint 🟠
> **TMAS pattern:** `pyproject.toml` + `tests/unit/` + ruff DTZ001-007

- [x] `ai-trading-agent/pyproject.toml`:
  - `[tool.ruff]` — `select = ["E", "F", "DTZ"]`
  - `[tool.pytest.ini_options]` — `testpaths = ["tests"]`
  - `[project.optional-dependencies] dev = ["pytest", "pytest-asyncio", "pytest-mock", "ruff"]`
- [x] `tests/conftest.py`:
  - `mock_mt5_connector`, `sample_candles_xauusd_m15`, `frozen_now` *(+ `clean_config_env`, `tmp_state_dir`)*
- [x] `tests/unit/test_smoke.py` (minimum 20 test): *(22 ta yozildi)*
  - `ICTAnalysis.analyze()` crash bermaydi (5 tf)
  - `RiskManagement.calculate_position_size()` lot > 0
  - `TradingAIBrain.validate()` API key yo'q — auto-approve
  - `MT5Connector` `_sim_mode` ishlaydi
  - Config validation: invalid env → ValidationError
  - State persistence: save/load round-trip
- [ ] CI yo'q hozircha — keyingi fazada. Hozir `pytest` qo'l bilan.

### Faza 1 Acceptance
- [x] `pytest tests/` → 20/20 passed *(243/243 passed: 22 smoke + 132 TMAS port + ...)*
- [x] `ruff check apps/` → 0 warning *(commit b95d01c: 0 errors)*
- [x] Self-learner 50 trade'dan keyin `pending_adjustments` ga yozadi, `learned.json` o'zgarmaydi *(2026-05-23: `test_self_learner_50_trades.py` 3/3 pass)*
- [x] Invalid `.env` bilan bot ishga tushmaydi *(ValidationError test passed)*
- [x] `/health` endpoint ishlaydi *(main.py:356-362 daemon thread)*

---

## 📈 FAZA 2 — Strategiyani tarixda tasdiqlash (1 hafta)

> **Maqsad:** Real pul oldidan strategiyaning oxirgi 1-2 yilda foyda berganini ko'rsatish. **To'liq walk-forward shart emas** — qisqa, ishonchli backtest yetarli.

### F2-1: Minimal backtest infrastruktura 🟡
> **TMAS'dan ko'chiriladi, lekin minimum scope:** VirtualClock, HistoricalDataManager, PaperBroker.

- [x] `core/clock.py` — TMAS'dan to'g'ridan-to'g'ri ko'chirish (89 satr) *(122 satr: VirtualClock + RealClock + singleton)*
- [x] `core/data.py` — TMAS'dan ko'chirish (286 satr)
- [x] `core/broker.py` — TMAS'dan ko'chirish (902 satr), XAUUSD adapter *(906 satr)*
- [x] **36 ta `datetime.utcnow()` chaqiruvi → `self.clock.now()`** (agent.py, self_learner.py, trade_analytics.py) *(40 ta migratsiya: 24 → clock.now(), 12 → tz-aware, 4 test)*
- [x] `pyproject.toml`'da DTZ rules yoqish — yangi `datetime.utcnow()` import bo'lsa CI fail
- [x] **Test:** TMAS testlarini ko'chirish — 52+44+15 = 111 test *(132 test ko'chirildi)*

### F2-2: Backtest runner (bizning ICT bilan) 🟡
- [x] `engine/config.py`, `engine/result.py`, `engine/engine.py` — TMAS'dan ko'chirish
- [x] `engine/engine.py`'da `ConfigurableStubAnalyst` o'rniga bizning **haqiqiy `ICTAnalysis` + `TraderAgent._find_all_ict_zones()` logikasi** ulanadi *(MVP scope: ICTAnalyst → H4 trend → H1 OB → PD-zone → 1:2 RR; to'liq `_find_all_ict_zones()` keyingi iteratsiya)*
- [x] `TradingAIBrain` (Claude) — backtest'da OFF default (har bar Claude chaqirish qimmat)
- [x] CLI: `python -m apps.api.src.agents.trader.engine.engine --start 2024-01-01 --end 2025-12-31 --symbol XAUUSD`

### F2-3: Minimal performance hisobot 🟡
- [x] `analysis/performance.py` (mini):
  - Win rate (Wilson CI bilan) — TMAS `stats.py` ko'chirish
  - Profit factor
  - Max drawdown %
  - Sharpe ratio (oddiy, bootstrap shart emas)
  - Avg RR
  - Setup breakdown (qaysi ICT setup yutyapti)
- [x] HTML hisobot: `reports/<run_id>.html` — equity curve + monthly table *(self-contained inline SVG, jinja2/plotly yo'q)*
- [x] **Acceptance:** 2024-2025 XAUUSD M15 backtest (real MT5 data) **run qilindi 2026-05-19** — verdict **REJECT (0 trade)**. Batafsil: `reports/f2-3_acceptance_2024-2025.md`. Sabab: MVP `ICTAnalyst` eng kuchli OB tanlaydi (max strength), so'ng narx shu OB ichida bo'lishini talab qiladi → 49.7% bar'da narx OB'dan $20-$30 uzoq → 0 signal. Live trader `_place_zone_limits` orqali limit order qo'yadi — boshqa paradigma.
  - Win rate CI butunlay 50% dan yuqorida bo'lishi (`CI low > 0.50`) — ❌ 0 trade
  - Profit factor > 1.5 — ❌ 0 trade
  - Max DD < 15% — n/a
  - Min 100 ta trade (statistik ahamiyat uchun) — ❌ 0

### F2-3.1: ICTAnalyst limit-order fix (F2-3 REJECT natijasidan) 🟢
- [x] **Variant B:** `Signal.is_limit` + analyst dedup + engine routing + journal pending-status (2026-05-21, +5 yangi test, 248/248 pass)
- [x] Wire-fix: `__main__.py` endi `journal.get_closed_trades()`'dan o'qiydi (broker.history o'rniga) — chunki `ClosedTrade`'da `setup_type/sl` yo'q.
- [x] **Smoke B 1 oy:** 4 trade (0→4), PF=0.60, setup=H1_OB ✓
- [x] **Variant C:** live `_find_all_ict_zones` to'liq port (2026-05-21):
  - `engine/zone_finder.py` (~700 satr) — 12 pure helper + main dispatcher (15 ICT zone turi)
  - `ICTAnalyst.analyze_market` qayta yozildi — D1/H4/H1/M30/M15 multi-TF, `list[Signal]` qaytaradi, graceful TF degradation, OB dedup
  - `BacktestEngine._trading_cycle` `list[Signal]`'ni iteratsiya qiladi (har biri alohida marshrutlanadi)
  - `blocked_setups` mexanizmi — SelfLearner o'rnini bosadi (backtest'da learner loop yo'q)
  - +30 yangi test (25 zone_finder, 5 list-routing, 3 filter); 281/281 pytest pass
- [x] **Smoke C 1 oy (filterless):** 314 trade, WR=42%, PF=0.72, PnL=-$99 — multi-TF infra ishlayapti
- [x] **Smoke C 1 oy (filtered M15_OTE+M15_DR_Eq):** 223 trade, WR=47.5%, PF=0.96, PnL=-$10.20 (breakeven yaqin)
- [x] **Engine hot-fix — progress logging** *(2026-05-23: F2 silent 5h run muammosini hal qildi — `engine/engine.py` ga `_estimate_total_bars()` + `_log_progress()` + `progress_every_bars` constructor param; `engine/__main__.py` ga `--progress-every-bars` CLI flag (default 1000); 1-haftalik synthetic'da `[progress] bars 300/672 (44.6%) | trades 64 | elapsed 163s | ETA 202s` ko'rinishi tasdiqlangan; 0 regression)*
- [x] **Acceptance:** 24 oy 2024-2025 acceptance run — **TUGADI (v5rr, hujjatlashtirildi 2026-06-20)** *(Eski 5h muammosi engine perf-fix (1308d10, ~9x tez) bilan hal bo'ldi. Yakuniy authoritative config = v5rr (`run_v5rr.ps1`): v4 blocklist + regime gate @0.18 + H1_OB DEFAULT_BLOCKED + structural min-RR floor 1.0. Natija (`_parsed_v5rr.json`, 8 chorak): **3015 savdo, net +$644.23, 7 WARN / 1 REJECT** (faqat q2_2025 −$94, adverse high-vol trend regime — ACCEPT). Formal validation (walk-forward 6 OOS + MC 10k) 2026-06-09 da: OOS PF 1.164≈in-sample 1.175 (overfit yo'q), worst-DD 2.34%, p_ruin 0 → strategiya ROBUST+SAFE, low-edge, demo-ready. F5 o'zgarishlaridan faqat F5-4 (M15_OB blok) backtest'ga ta'sir qiladi: first-order +$644→+$629 (−$15, M15_OB net-musbat bo'lgani uchun; foydalanuvchi xavfsizlik uchun blokni tanladi). F5-3 (regime gate @0.18) ALLAQACHON v5rr ichida. F5-1 (risk) / F5-6 (lot) backtest PnL'ga ta'sirsiz. Authoritative F5-4-variant run foydalanuvchi tanlovi bilan O'TKAZILMADI (verdict o'zgarishi kam ehtimol).)*
- [ ] (Optional) D1 + M30 ma'lumotini yuklash → to'liq paradigma (`scripts/download_mt5_history.py`)

### F2-4: Demo natijalarini hisobot qilish 🟡
- [ ] Hozirgi `learned.json` + `trades.csv` ni o'qib, real demo natijalarini hisoblash:
  - Demo'da nechta trade bo'lgan?
  - Real win rate (CI bilan)?
  - PF, max DD?
  - 30 kun davomida ishlaganmi? (CLAUDE.md talabi)
- [ ] Hisobot: `reports/demo_results.md`
- [ ] **Agar demo natijalari past bo'lsa** → backtest bilan strategiya tuzatish, real'ga emas

### Faza 2 Acceptance
- [ ] Backtest oxirgi 2 yilda ACCEPT verdict beradi (Wilson CI low > 50% WR)
- [ ] Demo natijalari real backtest bilan **mos** (overfit yo'q)
- [ ] Hisobotni admin (siz) ko'rib chiqib, "ha, live'ga tayyor" deydi

---

## 🚀 FAZA 3 — Live deployment (3-5 kun)

### F3-1: Demo'da yakuniy stress test 🔵
- [ ] 7 kun **uzluksiz** demo (Faza 0-1 fix'lari bilan)
- [ ] Sun'iy test'lar:
  - WiFi'ni 3 marta uzish — bot recover bo'lsin
  - Bot'ni 2 ochiq pozitsiya bilan kill qilib qayta yoqish — meta saqlangan bo'lsin
  - Crash sun'iy qilish (`raise` ichida) — Telegram'da xabar kelsin
- [ ] **Acceptance:** 7 kun ichida hech qanday qo'lda aralashish kerak bo'lmasin

### F3-2: Real account — kichik kapital bilan 🔵
- [ ] Real account ochish (Exness recommended — agent.md spec'da)
- [ ] **Boshlang'ich kapital: $500** (10% DD = $50 max yo'qotish)
- [ ] **Risk per trade: 0.5%** ($2.50 per trade)
- [ ] **Lot size**: avto-hisoblanadi, lekin maksimum `0.05 lot` cap qo'yish (config'da)
- [ ] `MT5_SERVER` o'zgartirish (demo → live), `.env` da `EXECUTION_MODE=live`
- [ ] **Birinchi 3 kun:** har trade Telegram'ga **MANUAL CONFIRM** so'rab yuborilsin (`confirm_before_trade=True`)
- [ ] **4-7 kun:** auto-mode, lekin har trade'da Telegram detail bilan xabar

### F3-3: Live monitoring 🔵
- [x] Telegram bot'ga `/status`, `/positions`, `/pause`, `/resume` commandlari qo'shish *(2026-05-23: `telegram_bot.py` ga inbound polling + command registry + auth; yangi `telegram_commands.py` 4 ta handler; `tests/unit/test_telegram_commands.py` 19 test pass)*
- [x] **main.py wire-up** *(2026-05-30: `telegram_commands.wire_up(agent)` helper — register_all + start_polling birga; idempotent (start_polling internal dedup); TELEGRAM_BOT_TOKEN yo'q bo'lsa graceful no-op (`_ENABLED=False` tekshiruv); main.py `_run_agent()` ga ulandi, finally'da stop_polling; +5 test (registers 4 cmd, skip polling when disabled, start when enabled, idempotent across reloads, custom bot_module injection); 396/396 pytest pass)*
- [ ] Watchdog ishga tushirilgan bo'lsin (`scripts/watchdog.ps1`)
- [x] **Daily summary generator** *(2026-05-30: `analysis/daily_summary.py` (~190 satr) — `DailySummaryConfig`/`DailySummaryResult`, UTC day window (half-open 00:00→24:00), reuses weekly_report CSV+filter+equity infra, PnL/WR/PF/intraday DD/best-setup, BE trades shown separately from L; CLI `python -m apps.api.src.agents.trader.analysis.daily_summary --balance X [--date YYYY-MM-DD]`; 14 test pass; 410/410 total)*
- [ ] **Scheduler wire-up:** 23:00 UTC'da daily_summary.generate() + telegram_bot.send — F4 scheduler fazasida

### Faza 3 Acceptance
- [ ] 7 kun real'da uzluksiz ishlaydi
- [ ] Birinchi haftada DD < 5%
- [ ] Hech qanday "ghost trade", "lost meta", "crash uzoq vaqt sezilmagan" hodisalar yo'q

---

## 🔄 FAZA 4 — Post-live: kuzatuv va yaxshilash (continuous)

> Bu ishlar live ishlashga **halaqit qilmaydi**, parallel davom etadi.

### F4-1: Haftalik hisobotlar 🟢
- [x] `analysis/weekly_report.py` (yangi, ~380 satr): `WeeklyReportConfig`, `WeeklyReportResult`, CSV → performance.compute schema mapping (entry→entry_price, exit→exit_price, label→setup_type), running-balance equity curve, session breakdown, `generate()` saves `reports/weekly_<date>.html` + compact HTML Telegram text (Wilson CI, PF, Sharpe, Max DD, best/worst session, best setup) *(2026-05-30; reuses `analysis/performance.compute` + `analysis/report_html.render`; performance.py:Sharpe nan-guard for small windows; CLI `python -m apps.api.src.agents.trader.analysis.weekly_report --balance X [--end YYYY-MM-DD] [--days 7]`)*
- [x] CLI smoke against live `brain/trades.csv` (1 trade window) — HTML 4.7 KB written, Telegram text formatted, verdict propagates
- [x] **Test:** `tests/unit/test_weekly_report.py` — 28 test (config validation, CSV load, normalization, window filter half-open, equity curve drawdown, session breakdown, Telegram zero-trade + populated, generate() integration writes HTML, CLI happy path + invalid date + print-telegram). **391/391 pytest pass (363 + 28 new), 0 regression.**
- [ ] **Scheduler wire-up:** Har juma 23:00 UTC `generate()` + `telegram_bot.send(result.telegram_text, parse_mode='HTML')` — F4 deployment fazasida (live MT5 paytida)

### F4-2: Walk-forward (oylik) 🟢
> **TMAS pattern:** `WalkForwardValidator` — bu yerda **bir marta oyiga** ishlatiladi

- [x] `analysis/walk_forward.py` (TMAS spec'idan ko'chirish) *(2026-05-23: ~280 satr; `WalkForwardConfig/Window/Result`, `build_windows`, `calc_overfit_score`, `_verdict`, `WalkForwardValidator.run()` engine_factory pattern bilan; parameter optimization YO'Q — joriy parametrlar bilan train/test drift o'lchanadi; `tests/unit/test_walk_forward.py` 20 test pass)*
- [x] Oyiga 1 marta: oxirgi 12 oyda walk-forward, overfit score chiqarish *(2026-06-20: scheduler wire-up. `scripts/monthly_validation.ps1` — Windows Task Scheduler skript (trailing -MonthsBack oyna, default 12; -Start/-End override; schtasks ro'yxatdan o'tkazish buyrug'i izohda). JONLI LOOP'da EMAS — walk-forward ko'p backtest (daqiqa-soat), tick'ni bloklardi; offline_validation.py muallifi shu uchun offline mo'ljallagan.)*
- [x] Overfit > 0.6 bo'lsa — Telegram OGOHLANTIRISH *(2026-06-20: `offline_validation.py` ga `--telegram` flag + `--overfit-threshold` (default 0.6) + pure `build_validation_alert(report) -> (is_warning, text)` (overfit>0.6 YOKI verdict REJECT → ⚠️; MC p_ruin + risk-calib seksiyalarni ko'rsatadi). Sync yuborish uchun `telegram_bot.send_sync()` qo'shildi (async `send` juftligi). cp1251 console footgun: CLI help ASCII-only (emoji faqat Telegram matnida, HTTP/UTF-8). +9 test (8 alert builder + 1 CLI flag), 532/532, ruff 0 (touched files).)*

### F4-3: Reflector A/B test 🟢
> **TMAS pattern:** `ReflectorBacktester` — self-learner haqiqatan foyda berayotganini tekshirish

- [x] `analysis/reflector_backtest.py` (TMAS spec'idan ko'chirish) *(2026-05-23: ~240 satr; `ReflectorABConfig` (thresholds), `ReflectorABResult`, `_verdict` heuristic (HARMFUL/BENEFICIAL/NEUTRAL/WARN), `ReflectorBacktester.run_comparison()` engine_factory `(start, end, treatment_on)` pattern bilan; paired t-test daily_returns talab qiladi — bizning BacktestResult'da hozircha yo'q, aggregate Sharpe/DD ratio'lar bilan ishlatildi; `tests/unit/test_reflector_backtest.py` 16 test pass)*
- [⛔] Oyiga 1 marta: SelfLearner ON vs OFF backtest *(2026-06-20: BLOKLANGAN — scheduler wire-up MUMKIN EMAS. Sabab: backtest engine `StubReflector` ishlatadi (engine.py:103) va SelfLearner loop yo'q → "treatment ON vs OFF" AYNAN bir xil ikki backtest → har doim NEUTRAL no-op. offline_validation.py muallifi shu sabab reflectorni ataylab chiqargan. Soxta NEUTRAL hisobot yuborish = soxta ishonch. F4-3 ni ulashdan OLDIN haqiqiy Reflector/SelfLearner engine'ga port qilinishi shart (alohida, katta task — scheduler wire-up emas). reflector_backtest.py + reflector_backtest A/B logikasi TAYYOR, faqat engine treatment yo'q.)*
- [⛔] Agar verdict HARMFUL — Telegram'ga "SelfLearner foyda bermayapti" *(Yuqoridagi sabab bilan bloklangan. Reflector engine'ga ulangach, build_validation_alert pattern'i (F4-2) bilan oson qo'shiladi.)*

### F4-4: Monte Carlo robustness 🟢
- [x] `analysis/monte_carlo.py` (TMAS spec'idan ko'chirish) *(2026-05-23: ~180 satr; `MonteCarloConfig` (n_sims/initial/ruin_threshold/seed), `MonteCarloResult` (p5/p50/p95 + worst_case + p_ruin + streak), helpers `equity_curve`/`max_drawdown_pct`/`longest_losing_streak`, `MonteCarloSimulator.run(pnls)` permutation-based bootstrap; `tests/unit/test_monte_carlo.py` 24 test pass)*
- [x] Worst-case drawdown ko'rsatkichi — risk per trade ni qayta sozlash uchun *(2026-06-13: `analysis/risk_calibration.py` (~290 satr). Engine `StubRisk` qat'iy lot ishlatadi → $-PnL `risk_per_trade`'ga bog'liq EMAS; to'g'ri ko'prik **R-multiple** (`pnl/(|entry-sl|*lot*contract_size)` — lot va contract_size qisqaradi, sizing'dan mustaqil). MC 1% reference risk'da → `worst_dd_per_1pct`; linear (fixed-fractional) yaqinlashish: `worst_dd(r) ≈ worst_dd_per_1pct*r`; `recommended_risk = dd_budget/worst_dd_per_1pct` bounds'ga clamp. RiskConfig wire-up: budjet=`RiskConfig.max_drawdown` (10%), joriy=`risk_per_trade` (1%). Verdict: OK/REDUCE_RISK/ROOM_TO_INCREASE/INSUFFICIENT_DATA; AVTO-MUTATSIYA YO'Q (F1-1 approval-gate falsafasi, faqat tavsiya). offline_validation'ga ulandi: bitta umumiy backtest MC+calib uchun, `--dd-budget-pct/--current-risk-pct/--calib-sims/--skip-risk-calibration` flaglar, `report["risk_calibration"]`. Sanity (46%WR/+0.095R thin edge): worst_dd@1%=21.4% → 1% risk budjetdan oshadi → tavsiya 0.46%. cp1251 console footgun tuzatildi (notes ASCII-only, +regression test). +27 test 508/508, ruff 0.)*

### F4-5: Strategiya yaxshilashlari 🟢
- [ ] Yangi setup type'lar — `pending_adjustments` orqali approval bilan
- [ ] Multi-symbol (XAUUSD + EURUSD + BTCUSD) — alohida faza

---

## 💹 FAZA 5 — Foydani xavfsiz oshirish + jonli kuzatuv (2026-06-18)

> **Maqsad:** foydani oshirish (xavf 2% + yomon davr filtri) va jonli demo kuzatuvni boshlash.
> **Asos:** 5-agentli Monte-Carlo halokat tahlili (2026-06-18) + 2 yillik tarixiy sinov.
> **Tasdiqlangan qaror:** xavf **max 2%**, yomon davr filtri, demo'da 100+ savdo kuzatuv.
> **Asosiy xulosa:** "kuniga 2%" MUMKIN EMAS (~2.8% xavf kerak → bitta yomon chorakda hisob yarmini yo'qotish ehtimoli 24–42%). 2%/savdo = yillik ~6.4%, eng yomon cho'kish ~4.6%.

### F5-1: Jonli xavfni nazoratga olish (3% → 2%) ✅ — 2026-06-20 BAJARILDI
- [x] ⚠️ **TOPILMA (2026-06-18):** jonli bot `agent.py:79` `RISK_SNIPER=0.03` va `agent.py:87` `RISK_FLOW=0.03` —
  ya'ni **3% xavf** bilan ishlayapti. `.env` `RISK_PER_TRADE=1.0` va `agent.py:94` `RISK_PCT=0.01` **e'tiborga olinmayapti** (sizing faqat RISK_SNIPER/FLOW dan).
- [x] `RISK_SNIPER`/`RISK_FLOW` ni `.env` `RISK_PER_TRADE` dan o'qiydigan qilish (qattiq yozilgan emas). *(2026-06-20: `agent.py.__init__` da `risk_frac = config.risk_per_trade/100.0` → `self.RISK_SNIPER/FLOW/PCT` instance ustiga yoziladi; class-konstantalar default 0.02 ga tushirildi + izoh ".env bilan ustiga yoziladi")*
- [x] Qiymatni **2%** ga o'rnatish (hozirgi 3% dan PASAYTIRADI — xavfsizroq). *(`.env` `RISK_PER_TRADE=1.0→2.0`; invariant `DAILY_MAX_RISK=5.0 >= 2.0` OK)*
- [x] **Tekshirish:** sizing 2% berishini bitta savdoda tasdiqlash (`Risk: 2.0%` log'da). *(startup log: `Risk per trade: 2.00% (SNIPER=FLOW=0.0200, .env'dan)`; instance RISK_SNIPER/FLOW/PCT=0.02 tasdiqlandi; smoke 24/24 pass)*

### F5-2: Kunlik xavf chegarasini qayta ko'rish 🟠
- [ ] Hozir `.env` `DAILY_MAX_RISK=5.0`. 2%/savdo da bu ~2.5 to'liq zararga teng.
- [ ] Qaror: 4% atrofida (≈2 zararli savdodan keyin "faqat boshqarish" rejimi).

### F5-3: Yomon davr (trend rejimi) filtrini jonli botga ulash ✅ — 2026-06-20 BAJARILDI
- [x] ⚠️ **TOPILMA:** jonli bot faqat `regime == "chop"` da to'xtaydi (`agent.py:669`), **"trend" da EMAS**.
  Lekin 2025-Q2 halokati (−$94, yutuq 28%) aynan KUCHLI TREND rejimida bo'lgan.
- [x] Backtest'da tayyor filtr bor: `engine/__main__.py:197` `--regime-atr-threshold` (yuqori volatillikda
  M15_OB/BB/CISD bloklaydi). Shu mantiqni jonli `agent.py` ga ko'chirish/ulash. *(2026-06-20: backtest'dagi pure `engine/regime.py:RegimeGate` to'g'ridan-to'g'ri qayta ishlatildi (kod takrori YO'Q — modul aynan "live trader uchun" yozilgan edi). `TraderAgent.__init__` da `self.regime_gate = RegimeGate(atr_threshold, atr_period)`; `_tick` da chop-check'dan keyin M15'da `regime_atr_pct` bir marta hisoblanadi (`m15` DataFrame, ATR% _calc_atr bilan byte-mos); `_place_zone_limits` ga `regime_atr_pct` param sifatida uzatiladi va placement loop'da `should_block(label, atr%)` bo'lsa zona skip + log. Config: `TradingConfig.regime_atr_threshold/period` (`.env` REGIME_ATR_THRESHOLD/PERIOD; default 0.0=o'chiq back-compat). Jonli `.env`=0.18 (production calibrated). Tasdiq: startup log `Regime gate ON: M15 ATR% > 0.18 → blok [M15_BB, M15_CISD, M15_OB]`; live labellar (agent.py:1141 M15_OB / 1146 M15_BB / 1243 M15_CISD) gate bilan AYNAN mos; should_block semantikasi (high-vol blok / normal o'tkazadi / H1_OB o'tkazadi) tasdiqlandi; 519/519 pytest, ruff 0.)*

### F5-4: `M15_OB` sifatsiz signalini cheklash ✅ — 2026-06-20 BAJARILDI (butunlay blok)
- [x] Eng katta zarar manbai: 2025-Q2 da 120 savdo, WR 26%, −$40 (chorak zararining yarmidan ko'pi).
  2025-Q1 da ham zararda (−$10). Yuqori volatillikda butunlay bloklash yoki sifat balini oshirish.
- **📊 DATA (v5rr, 24oy per-setup):** M15_OB 1006 savdo, net **+$15.38** (foydali!). Foydali choraklar: q2_2024 +$30, q3_2024 +$15, q3_2025 +$38. Eng katta zarar q2_2025 −$40 = **F5-3 high-vol gate allaqachon bloklaydigan** chorak (~50% barlar). Qolgan normal-regime zararlar kichik (−$2..−$10). Data **butunlay blokni rad etadi** (+$83 foyda yo'qoladi, F5-3 mantiqiga zid). Foydalanuvchiga AskUserQuestion bilan ko'rsatildi.
- **QAROR (2026-06-20):** Foydalanuvchi dalillarni ko'rib ham **"M15_OB butunlay blok"** ni tanladi (maksimal xavfsizlik, past-WR setupni umuman xohlamaydi).
- [x] Implementatsiya: config-driven block list (backtest `DEFAULT_BLOCKED_SETUPS` mexanizmiga o'xshash). `TradingConfig.blocked_setups` (`.env BLOCKED_SETUPS`, vergul bilan) + `get_blocked_setups()` parser; `TraderAgent.__init__` da `self.blocked_setups`; `_place_zone_limits` placement loop'da (yagona chokepoint — barcha zona manbalari + market/limit yo'llar shu yerdan o'tadi) F5-3 gate yonida `label in blocked_setups` → skip+log `⛔`. Jonli `.env BLOCKED_SETUPS=M15_OB`; default bo'sh (back-compat). +4 test (parser empty/csv, regime defaults off, range reject) 523/523, ruff 0.

### F5-5: Jonli demo kuzatuv — 100+ savdo (≈30 kun) 🔵 — HOZIR BOSHLANDI
- [ ] Demo'da uzluksiz ishlatish. Sabab: hozir atigi **13 ta haqiqiy savdo** — juda kam, bitta savdo ($58) natijani buzadi.
- [ ] Kunlik: nechta savdo, yutuq/zarar, sof PnL, joriy yutuq ulushi (`brain/trades.csv` dan).
- [ ] Bot ichidagi kunlik/haftalik Telegram hisobotidan foydalanish.
- [ ] **Baseline (2026-06-18):** 13 savdo, 2 yutuq / 11 zarar (WR ~15%), sof **+$37.48** ($88.86 − $51.38).
  $58 savdosiz → −$21 (zararda). 1 savdo = shovqin, signal emas.

### F5-6: `lot` (pozitsiya hajmi) yozuvini tuzatish ✅ — 2026-06-20 BAJARILDI
- [x] ⚠️ Hozir `trades.csv` da `lot=0.0` yoziladi (saqlanmaydi). *(2026-06-20: ildiz sabab — `log_trade_csv(... lot=0.0 ...)` chaqiruvida QATTIQ YOZILGAN 0.0 (`agent.py:2472`), meta'da lot saqlanmas edi. `get_closed_position` faqat {profit, price_close} qaytaradi (volume yo'q) → lot order qo'yish/fill paytida saqlanishi shart. 3 ta meta-yaratish yo'li: (1) market `_trade_meta` → `"lot": lot_each`; (2) limit fill `_trade_meta` → `"lot": float(filled["volume"])` (MT5 haqiqiy hajmi); (3) `recover_from_mt5` ALLAQACHON `"lot": pos["volume"]` saqlardi. CSV log + CLOSED Telegram notify endi `meta.get("lot")` o'qiydi (avval qattiq 0.0). +1 test (CSV lot column persists 0.05). 524/524, ruff 0.)*
- [⚠️] $58 yutuq −$5 zararlardan ~7x katta hajmda bo'lgan — nazoratsiz hajm bo'lishi mumkin. Tekshirish + to'g'ri yozish. *(Yozish TUZATILDI. Tekshirish: tarixiy trades.csv da lot=0.0 → o'tmishni qayta tiklab bo'lmaydi (close history'da volume yo'q). Bundan keyin haqiqiy hajm ko'rinadi. Hajm pozitsiya-sizing'dan (`risk_amt_z/(sl_pts*tick_val)`, agent.py:2019) — SL masofasi va risk%'ga bog'liq; F5-1 endi 2% qildi. Real account'da F3-2 da 0.05 lot cap rejalashtirilgan. Live lot cap qo'shish ixtiyoriy keyingi qadam.)*

### F5-7: Edge oshirish (kuzatuvdan KEYIN) 🟢
- [ ] 2025-Q2 filtri ko'rmagan davrlarda (OOS) ishlashini sinash — overfit bo'lmasligi uchun.
- [ ] Foydani uzoqroq yetkazish (TP masofasi) — yutuqlarni kattalashtirib edge oshirish.

### Faza 5 Acceptance
- [ ] Jonli xavf `.env` orqali boshqariladi va 2% (3% emas).
- [ ] Trend rejimida himoya yoqilgan (2025-Q2 takrorlanmaydi).
- [ ] Demo'da 100+ savdodan keyin real WR + sof PnL backtest bilan **mos** (overfit yo'q).

---

## 🎯 Qaror nuqtalari (Decision Gates)

Har faza oxirida **GO / NO-GO** qaror:

| Faza | GO sharti | NO-GO bo'lsa |
|---|---|---|
| 0 → 1 | ✅ 2026-05-14: 68/68 offline test passed. Demo stress test F3'da qoladi | — |
| 1 → 2 | ✅ 2026-05-14: 243/243 pytest passed, config validation ishlaydi, ruff 0 errors | — |
| 2 → 3 | 🟡 Backtest TUGADI (v5rr 24oy: +$644, 7WARN/1REJ, PF1.175). Formal validation: ROBUST+SAFE, low-edge → **DEMO-ready** (real kapitalga edge yupqa). Strict "CI low > 50% WR" SHARTI BAJARILMAYDI (past-WR/PF-musbat mean-reversion kitob) — demo uchun ACCEPT, real uchun ehtiyot. Demo natijalari mosligi F5-5 (100+ savdo) da tekshiriladi | Strategiya tuzatish (Faza 2 da qoladi) |
| 3 → Real | 7 kun demo uzluksiz, manual review bo'lib o'tdi | Demo'da qolish, fix qilib qaytarish |
| 3 (Demo) → 3 (Real) | Birinchi 7 kun real'da DD < 5% | Real'ni to'xtatib, root cause analiz |

---

## ❌ Tasks.md'dan chiqarib tashlangani

Bular kelajakka, **live'dan keyin** (Faza 4 yoki undan keyin):

- ~~To'liq TMAS port (InMemoryBus, BacktestJournal, admin CLI Click)~~ — minimum scope
- ~~PerformanceAnalyzer (30 maydon, bootstrap CI hamma metrikada)~~ — mini variant yetarli
- ~~Schema migrations versioning (alembic)~~ — keyingi DB upgrade'da
- ~~Frozen dataclass har joyda~~ — faqat Signal va Config
- ~~Lazy import / circular dependency tozalash~~ — bizda hozir muammo yo'q
- ~~Next.js dashboard~~ — Telegram + healthcheck yetarli

---

## 📅 Taxminiy vaqt jadvali

| Hafta | Faza | Asosiy ish | Holat |
|---|---|---|---|
| 1 | F0 | Restart/disconnect/crash safety | ✅ 2026-05-14 |
| 2 | F1 | Approval gate + config + tests + healthcheck | ✅ 2026-05-14 (commit b95d01c) |
| 3 | F2 | Backtest setup + tarixda tasdiqlash | ⏳ Kod tayyor, real data bilan run kerak (F2-3, F2-4) |
| 4 | F3 (demo) | 7 kun uzluksiz demo + stress test | — |
| 5 | F3 (real) | Real $500, manual confirm → auto | — |
| 6+ | F4 | Walk-forward, A/B, yaxshilashlar | — |

---

## 📚 Reference

- **TMAS framework:** `C:\Users\Game PC 2026\Desktop\TRD\Trx-YouTube-automation\` (Tasks.md va Done.md)
- **Bizning spec:** `agent.md` (asosiy arxitektura)
- **Bizning context:** `CLAUDE.md`
