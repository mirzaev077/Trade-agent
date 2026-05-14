# ai-trade-agent — Bajarilgan ishlar

> Bu fayl Tasks.md dan ko'chirilgan tugatilgan ishlarni saqlaydi. Har faza/batch o'z bo'limiga ega — sana, fayl o'zgarishlari, test natijalari, va asosiy qarorlar.

---

## 🚀 FAZA 0 — Live blokerlarini ochish — 2026-05-14

> **Maqsad:** Real pul oldidan demo'da xavfsiz ishlay olish uchun 5 ta kritik bug'ni bartaraf qilish.
> **Yondashuv:** Multi-agent parallel (3 batch).
> **Yutuq:** +807 satr / -3 satr, 5 ta yangi fayl, 68 ad-hoc test passed.
> **Vaqt:** ~1 sessiya (avval audit + reja, keyin 3 batch implementation + 2 batch test).

### Multi-agent batch'lari

| Batch | Tartib | Agentlar | Tegilgan fayllar |
|---|---|---|---|
| 1 | Paralel | F0-2 + F0-3 + F0-5 | mt5_connector.py / main.py+telegram_bot.py / self_learner.py — fully isolated |
| 2 | Sequential | F0-1 | agent.py heavy (+72) + state/persistence.py (yangi 170 satr) + F0-2/F0-3 integratsiya |
| 3 | Sequential | F0-4 | agent.py kichik patch (+70 satr) — `_cancel_all_pending_orders` |
| Test A | Paralel | Test F0-1/F0-2/F0-4 | 30 ta assert-test |
| Test B | Paralel | Test F0-3/F0-5 | 38 ta assert-test |

---

### F0-1: Restart safety — `_trade_meta` persistence ✅

> **Muammo:** Bot qayta ishga tushganda `_trade_meta` xotirada yo'qoladi. PnL hisoblash, partial close (TP1/TP2/TP3), trailing SL buziladi.

- [x] `state/__init__.py` (yangi, bo'sh)
- [x] `state/persistence.py` (yangi, 170 satr) — `save_trade_meta()` atomik (temp file + `os.replace`), `load_trade_meta()` JSON round-trip + int key normalize, `recover_from_mt5()` magic filter (defense-in-depth)
- [x] `agent.py` (+72 satr): `__init__`'da `load_trade_meta()` → bo'sh bo'lsa `recover_from_mt5()`. **9 ta save chaqiruvi** quyidagi joylarda: market open (line 1921), limit fill (2053), position close (2216), sibling BE shift (2229), TP1 partial (2294), TP2 partial (2307), TP3 close (2333), trail to BE (2365).
- [x] `.gitignore` (yangi, 12 satr) — `data/state/`, `apps/data/state/`, atomic temp files (`.trade_meta_*.json`)
- [x] **Test:** `scripts/test_f0/test_persistence.py` — 11/11 passed (atomik yozish, round-trip, malformed JSON, recover magic filter, int↔string ticket)

**`_trade_meta` real strukturasi** (audit natijasi):
```python
{ticket: {
    "set_id": str, "tp_label": str, "tp/tp1/tp2/tp3": float,
    "sl": float, "sl_pips": float, "entry": float, "direction": str,
    "tf": str, "be_done": bool,
    "tp1_partial_done": bool, "tp2_partial_done": bool, "tp3_done": bool,
    "pnl": float, "mode": str, "label": str,
}}
```

**Qabul qilingan trade-off:** Tiklangan pozitsiyalar `partial_done` flag'larini `False` bilan keladi — agar MT5'da TP1 allaqachon urilgan bo'lsa, MT5 backend volume cap qiladi (xato emas, faqat log warning). State har mutationda yozilgani uchun bunday holat kam.

---

### F0-2: MT5 disconnect + reconnect ✅

> **Muammo:** Runtime'da MT5 uzilsa, qayta ulanish logikasi yo'q. Ghost trade ochilishi mumkin.

- [x] `mt5_connector.py` (+109 satr): 3 ta yangi metod
  - `is_connected() → bool` — `mt5.terminal_info().connected`
  - `ensure_connected(max_retries=5, initial_backoff=2.0) → bool` — exponential backoff 2/4/8/16/32s
  - `get_disconnect_duration_min() → int | None`
- [x] `__init__` ga qo'shildi: `_login`, `_password`, `_server` (credentials cache argumentsiz reconnect uchun), `_last_disconnect_at`, `_last_recovery_downtime_min`
- [x] `connect()` boshida credentials cache (mavjud signature buzilmadi)
- [x] **Integratsiya:** `_tick()` (line 172) boshida F0-1 agent tomonidan ulandi — disconnect → tick skip + Telegram alert, recovered → Telegram alert (flag boshqaruvi bilan qayta-qayta yubormaslik)
- [x] **Test:** `scripts/test_f0/test_reconnect.py` — 9/9 passed (`time.sleep` mock'langan — test 0.5s da bajarildi)

---

### F0-3: Telegram crash / disconnect / shutdown alerts ✅

> **Muammo:** Bot crash bo'lsa yoki SIGTERM bilan to'xtatilsa — log faylda yoziladi, lekin Telegram'da hech qanday xabar yo'q.

- [x] `telegram_bot.py` (+130 satr): 4 ta **sync** notify funksiya — `urllib.request` (stdlib, async dependency yo'q)
  - `notify_crash(exc_type, exc_value, tb_str)` — 🚨🚨🚨 + traceback oxirgi 30 satr, `_MAX_TG_LEN = 4000` trim
  - `notify_disconnect(duration_min)` — ⚠️ MT5 UZILDI
  - `notify_recovered(downtime_min)` — ✅ MT5 QAYTA ULANDI
  - `notify_shutdown(reason)` — 🛑 BOT TO'XTATILDI
  - `_resolve_token_chat()` — module globals → env vars fallback (init() chaqirilmasa ham ishlaydi)
- [x] `main.py` (+83 satr):
  - `sys.excepthook = _crash_handler` — uncaught exception → Telegram
  - `signal.SIGINT` handler (POSIX + Windows)
  - `signal.SIGTERM` (POSIX) yoki `signal.SIGBREAK` (Windows fallback, Ctrl+Break)
  - `KeyboardInterrupt` alohida ishlatildi — Ctrl+C ikki marta xabar yubormaslik uchun
  - `_shutdown_in_progress` flag — ikkinchi signal'da `os._exit(1)` darhol chiqaradi
- [x] **Test:** `scripts/test_f0/test_telegram.py` — 19/19 passed (token yo'q → graceful, URLError/OSError graceful, trim, excepthook o'rnatilgani, platform-mos signal handler)

**Hot reload bilan konflikt yo'q** — `sys.excepthook` global, `importlib.reload()` o'zgartirmaydi.

**Sync HTTP in async loop trade-off:** `notify_disconnect`/`notify_recovered` `_tick()` ichida 5 sek event loop bloklashi mumkin (Telegram API javob bermasa). Har disconnect uchun bir marta chaqiriladi, acceptable. Async variant F1+ da ko'rilishi mumkin.

---

### F0-4: News blackout'da pending orders bekor qilish ✅

> **Muammo:** News window paytida yangi order qo'yilmasa-da, oldindan qo'yilgan pending'lar ochiq qoladi va keng spread/volatilitet bilan execute bo'lib ketishi mumkin.

- [x] `agent.py` (+70 satr): yangi `_cancel_all_pending_orders(reason="NEWS_BLACKOUT") → int` method
  - `mt5.get_pending_orders(symbol)` chaqiradi — Python tarafida magic filter (`magic == 20240101`)
  - Cancel'da `_pending_zones` tracking lug'atidan ham olib tashlanadi (stale tracking yo'q)
  - Exception graceful: get/cancel xato bersa 0 yoki davom etadi (boshqalari to'xtamaydi)
- [x] News blackout joyiga (line 249-258) ulandi — `_news_cancel_done` flag bilan **idempotent** (window davomida bir marta cancel)
- [x] `__init__` ga qo'shildi: `self._news_cancel_done: bool = False`
- [x] **mt5_connector.py'ga tegilmadi** — `get_pending_orders()` va `cancel_pending_order()` allaqachon mavjud edi
- [x] **Test:** `scripts/test_f0/test_cancel_pending.py` — 10/10 passed (mixed magic filter, empty list, cancel False, exception graceful, `_pending_zones` cleanup, ticket=0 skip)

**Manage-only mode (daily loss limit) paytida cancel YO'Q** — bu xavf news'ga xos (spread spike). Daily loss limit normal market'da bo'lsa, pending'larni bekor qilish strategiyani buzadi. Weekend cancel ham yo'q (spec).

---

### F0-5: Self-learner `lessons.json` priority ✅

> **Muammo:** `learned.json` avto-mutation `lessons.json`'dagi `action: disable` qarorni e'tibordan chetda qoldirib, disabled setup'ni qayta yoqishi mumkin.

- [x] `self_learner.py` (+101 satr, -2 satr):
  - Modul docstring'ga schema qo'shildi (qabul qilingan `action: disable` field)
  - `__init__` yangi optional param `lessons_path` (default: `brain/lessons.json`)
  - `_load_disabled_setups() → set[str]` — `{entry.setup.upper() for entry in lessons if entry.action == "disable"}`
  - `_refresh_disabled_setups()` — `os.path.getmtime()` cache, faqat fayl o'zgarganda reload
  - `get_setup_weight(zone_label)` 3-bosqichli priority:
    1. `lessons.json` hard-disable → `0.0`
    2. `learned.json` `adapted.disabled` (auto-evolution) → `0.0`
    3. `learned.json` `setup_weights[typ]` → float (default 1.0)
- [x] `brain/lessons.example.json` (yangi, 34 satr) — schema namuna. **Production `lessons.json` ga tegilmadi.**
- [x] **Test:** `scripts/test_f0/test_self_learner.py` — 19/19 passed (disable → 0.0, mtime cache, case insensitive `rb`/`Rb`/`RB`, malformed JSON, backward compat warning-only entries)

**Backward compatibility:** Hozirgi `lessons.json` da `action` field yo'q (warning-only entries) — disabled set'ga qo'shilmaydi. Eski format ishlashda davom etadi.

**Race condition:** Telegram-thread `lessons.json` ga yozayotganda main-thread o'qisa JSONDecodeError ushlanadi → bo'sh set qaytaradi → keyingi tick'da retry. Crash yo'q. F1'da atomik yozish (`save_trade_meta` pattern) qo'shilishi mumkin.

---

### Faza 0 Acceptance (tugatildi)

| Kriteriy | Holat |
|---|---|
| Barcha 5 task kod sifatida bajarildi | ✅ |
| 68 ad-hoc test passed (0 fail) | ✅ |
| Production fayllariga (lessons.json, learned.json, trades.csv) tegilmagan | ✅ |
| `git commit` qilinmagan — admin ko'rib chiqishi shart | ⏳ |
| 24 soat demo uzluksiz ishlash | ⏳ Real demo'da kuzatuv hali qilinmagan |
| WiFi uzish, restart, sun'iy crash — manual stress test | ⏳ |

**Offline coverage 68/68** — kod darajasida ishonchli. Real demo stress test F3 (deployment) fazasida bajariladi.

---

### Yakuniy hisob (2026-05-14)

| Metric | Avval | Keyin | Farq |
|---|---|---|---|
| Live blokerlarini (KRITIK) | 5/5 | 0/5 | -5 ✅ |
| `_trade_meta` persistence | yo'q | 9 ta save call | ✅ |
| MT5 reconnect logic | yo'q | exponential backoff | ✅ |
| Crash Telegram alert | yo'q | 4 ta sync notify | ✅ |
| News pending cancel | yo'q | idempotent flag bilan | ✅ |
| lessons.json priority | yo'q | mtime cache + 3-bosqich | ✅ |
| Test scripts | 0 | 5 fayl, 68 test | +68 |
| Yangi src fayllar | mavjud | +2 (state/) | +2 |

---

### Keyingi sessiya uchun ochilgan yo'llar

- **F1** boshlashga tayyor — F0 KRITIK ishlar tugadi, F1 esa F1-1 (Reflector approval gate) — F0-5 bilan keyingi qadam mantiqiy davom etadi
- **F1-5** (pytest setup) — `scripts/test_f0/` ad-hoc testlarini `tests/unit/` ga ko'chirish, `pyproject.toml`'da pytest config
- **F2** (backtest) ham unblock — VirtualClock'ga `clock.now()` patterni F0-3 va F0-5 da allaqachon datetime ishlatishni ko'p joylarda ko'rdik (lekin abstraktsiya hali yo'q)

### Hal qilingan dizayn muammolari

- **Atomik file write Windows'da:** `os.replace()` POSIX `rename(2)` ga teng — atomik. Test bilan tasdiqlandi.
- **Async ↔ sync Telegram:** `urllib.request` sync — `sys.excepthook` ichida ham xavfsiz. asyncio loop'siz ishlaydi. Acceptable 5sek block trade-off.
- **Magic filter defense-in-depth:** `mt5_connector.get_open_positions()` allaqachon magic filter qiladi, lekin `recover_from_mt5()` ham qo'shimcha tekshiradi — soflik uchun.
- **`_news_cancel_done` flag boshqaruvi:** News window davomida har tick'da qayta-qayta cancel chaqirmaslik uchun bir marta cancel, news tugagach flag reset.
- **`lessons.json` mtime cache Windows FS:** `os.path.getmtime()` resolution ~10ms — testda `os.utime` bilan majburiy oshirildi. Production'da admin qo'lda fayl o'zgartirsa ko'rinarli kechikish bo'lmaydi.
