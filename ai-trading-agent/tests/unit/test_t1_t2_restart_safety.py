"""
T1 + T2: uzluksiz bo'lmagan ishlash uchun restart xavfsizligi (2026-08-03).

KONTEKST: foydalanuvchi kompyuterni 30 kun uzluksiz yoqib tura olmaydi, ya'ni
F5-5 (100+ savdo) davomida bot kuniga bir necha marta o'chib-yonadi. Kodni
tekshirganda uzluksiz ishlashga MO'LJALLANGAN ikki joy topildi:

T1 — kunlik hisoblagichlar har restartda NOLLANADI
     `RiskManagement._today_trades` va `_daily_open_count` faqat xotirada
     (risk/manager.py:11-12) va ular jonli yopilish hodisasidan to'ldiriladi
     (agent.py `add_closed_trade`). Startda MT5 tarixidan tiklanmasdi.
     Natija: bot 4% yo'qotib breaker ishlagach restart qilinsa, yana 4%
     yo'qotishi mumkin edi — "kunlik 4%" amalda "seansiga 4%" ga aylanardi.
     `_starting_balance` ham joriy balansga tushib, drawdown bazasini
     surib yuborardi.

T2 — bot o'chganda pending orderlar brokerda QOLARDI
     `_pending_zones` xotirada (agent.py:202), shutdown'da tozalash yo'q edi.
     O'chiq paytda narx zonaga yetib order ochilardi va uni hech kim
     boshqarmasdi (trailing SL yo'q, TP1 qisman yopish yo'q).
     `recover_from_mt5` faqat POZITSIYALARNI tiklaydi, pendinglarni emas.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apps.api.src.agents.trader import mt5_connector as mt5_mod
from apps.api.src.agents.trader.agent import TraderAgent
from apps.api.src.agents.trader.core.clock import get_clock
from apps.api.src.agents.trader.models.config import TradingConfig

OPENCLAW_MAGIC = 20240101
_IN = getattr(mt5_mod.mt5, "DEAL_ENTRY_IN", 0)
_OUT = getattr(mt5_mod.mt5, "DEAL_ENTRY_OUT", 1)


class _FakeDeal:
    """MT5 deal — `get_todays_activity` o'qiydigan maydonlar bilan."""

    def __init__(self, position_id, entry, ts, *, magic=OPENCLAW_MAGIC,
                 profit=0.0, swap=0.0, commission=0.0, price=4000.0, volume=0.01):
        self.position_id = position_id
        self.entry = entry
        self.magic = magic
        self.time = int(ts.timestamp())
        self.profit = profit
        self.swap = swap
        self.commission = commission
        self.price = price
        self.volume = volume


def _conn(monkeypatch, deals):
    c = mt5_mod.MT5Connector()
    c._sim_mode = False
    monkeypatch.setattr(mt5_mod.mt5, "history_deals_get", lambda *a, **k: deals)
    return c


def _now():
    return get_clock().now()


# ══════════ T1: kunlik holatni MT5 tarixidan tiklash ══════════

def test_sim_mode_returns_empty():
    """Sim rejimda tarix yo'q — bo'sh natija, xato emas."""
    c = mt5_mod.MT5Connector()
    c._sim_mode = True
    assert c.get_todays_activity() == {"closed": [], "opened_count": 0}


def test_no_deals_returns_empty(monkeypatch):
    assert _conn(monkeypatch, []).get_todays_activity() == {"closed": [], "opened_count": 0}


def test_sl_tp_close_still_counted_despite_zero_magic(monkeypatch):
    """ENG NOZIK JOY: SL/TP bilan yopilgan deal'da magic 0 bo'lishi mumkin.

    U server tomonidan yaratiladi va order magic'ini olib qolmasligi mumkin.
    Faqat yopilish deal'iga qarab filtrlash bunday savdoni butunlay
    o'tkazib yuborardi — ya'ni eng ko'p uchraydigan yopilish turi (stop-loss)
    kunlik zarar hisobiga KIRMASDI.

    Shuning uchun "bizniki" pozitsiyalar OCHILISH deal'i bo'yicha aniqlanadi.
    """
    t = _now()
    deals = [
        _FakeDeal(100, _IN, t - timedelta(hours=2), magic=OPENCLAW_MAGIC),
        _FakeDeal(100, _OUT, t - timedelta(hours=1), magic=0, profit=-40.0),
    ]
    res = _conn(monkeypatch, deals).get_todays_activity()
    assert len(res["closed"]) == 1
    assert res["closed"][0]["pnl"] == -40.0


def test_foreign_positions_ignored(monkeypatch):
    """Boshqa magic bilan ochilgan (qo'lda savdo) — bizning hisobga kirmaydi."""
    t = _now()
    deals = [
        _FakeDeal(200, _IN, t - timedelta(hours=2), magic=999),
        _FakeDeal(200, _OUT, t - timedelta(hours=1), magic=999, profit=-500.0),
    ]
    res = _conn(monkeypatch, deals).get_todays_activity()
    assert res == {"closed": [], "opened_count": 0}


def test_yesterdays_close_excluded(monkeypatch):
    """Kecha yopilgan savdo bugungi zarar hisobiga kirmaydi."""
    t = _now()
    y = t - timedelta(days=1)
    deals = [
        _FakeDeal(300, _IN, y - timedelta(hours=2)),
        _FakeDeal(300, _OUT, y, profit=-99.0),
    ]
    res = _conn(monkeypatch, deals).get_todays_activity()
    assert res["closed"] == []


def test_opened_yesterday_closed_today(monkeypatch):
    """Kecha ochilib bugun yopilgan: yopilish HISOBGA KIRADI, ochilish YO'Q.

    `_daily_open_count` bugungi kunlik savdo limitini (MAX_TRADES_PER_DAY)
    kuzatadi — kechagi ochilish uni yemasligi kerak.
    """
    t = _now()
    deals = [
        _FakeDeal(400, _IN, t - timedelta(days=1), commission=-0.2),
        _FakeDeal(400, _OUT, t - timedelta(minutes=30), profit=-15.0),
    ]
    res = _conn(monkeypatch, deals).get_todays_activity()
    assert res["opened_count"] == 0
    assert len(res["closed"]) == 1
    # PnL ochilish deal'idagi komissiyani ham qamrab oladi
    assert res["closed"][0]["pnl"] == pytest.approx(-15.2)


def test_open_position_not_in_closed(monkeypatch):
    """Bugun ochilgan, hali yopilmagan: opened_count=1, closed bo'sh."""
    t = _now()
    deals = [_FakeDeal(500, _IN, t - timedelta(minutes=10))]
    res = _conn(monkeypatch, deals).get_todays_activity()
    assert res["opened_count"] == 1
    assert res["closed"] == []


def test_pnl_includes_swap_and_commission(monkeypatch):
    t = _now()
    deals = [
        _FakeDeal(600, _IN, t - timedelta(hours=3), commission=-0.5),
        _FakeDeal(600, _OUT, t - timedelta(hours=1), profit=20.0, swap=-1.5, commission=-0.5),
    ]
    res = _conn(monkeypatch, deals).get_todays_activity()
    assert res["closed"][0]["pnl"] == pytest.approx(17.5)


def test_closed_sorted_by_time(monkeypatch):
    t = _now()
    deals = [
        _FakeDeal(2, _IN, t - timedelta(hours=4)),
        _FakeDeal(2, _OUT, t - timedelta(hours=1), profit=-5.0),
        _FakeDeal(1, _IN, t - timedelta(hours=5)),
        _FakeDeal(1, _OUT, t - timedelta(hours=3), profit=-10.0),
    ]
    res = _conn(monkeypatch, deals).get_todays_activity()
    assert [c["position_id"] for c in res["closed"]] == [1, 2]


# ── _restore_daily_state (agent) ────────────────────────────────────────────

def _agent_with_activity(activity):
    a = TraderAgent(config=TradingConfig(_env_file=None))
    a.mt5 = MagicMock()
    a.mt5.get_todays_activity.return_value = activity
    return a


def test_restore_populates_risk_state():
    t = _now()
    a = _agent_with_activity({
        "closed": [
            {"position_id": 1, "pnl": -20.0, "close_time": t - timedelta(hours=2)},
            {"position_id": 2, "pnl": 5.0, "close_time": t - timedelta(hours=1)},
        ],
        "opened_count": 3,
    })
    a._restore_daily_state(1000.0)
    assert [x["pnl"] for x in a.risk._today_trades] == [-20.0, 5.0]
    assert a.risk._daily_open_count == 3
    assert a.risk._count_date == t.date()


def test_restore_sets_count_date_so_reset_does_not_wipe():
    """`_reset_if_new_day()` tiklangan holatni o'chirib yubormasin.

    `check_trade_allowed` uni har chaqiruvda ishga soladi (manager.py:78) —
    `_count_date` bugunga qo'yilmasa tiklangan savdolar darhol yo'qolardi.
    """
    t = _now()
    a = _agent_with_activity({
        "closed": [{"position_id": 1, "pnl": -30.0, "close_time": t}],
        "opened_count": 1,
    })
    a._restore_daily_state(1000.0)
    a.risk._reset_if_new_day()
    assert len(a.risk._today_trades) == 1, "tiklangan holat reset bilan o'chib ketdi"


def test_restore_adjusts_starting_balance_to_day_open():
    """`_starting_balance` kun BOSHIDAGI balansga tuzatiladi.

    Aks holda restartdan keyin drawdown bazasi pastroq balansga tushib,
    kunlik zarar foizi kichik ko'rinardi.
    """
    t = _now()
    a = _agent_with_activity({
        "closed": [{"position_id": 1, "pnl": -50.0, "close_time": t}],
        "opened_count": 1,
    })
    a._restore_daily_state(950.0)          # 50 dollar yo'qotilgandan keyingi balans
    assert a._starting_balance == pytest.approx(1000.0)


def test_restore_makes_daily_breaker_block():
    """ASOSIY MAQSAD: tiklangandan keyin kunlik zarar darvozasi ISHLASIN.

    Bugungi zarar 4% ni bosib o'tgan — restart uni nolga qaytarmasligi kerak.
    """
    t = _now()
    a = _agent_with_activity({
        "closed": [{"position_id": 1, "pnl": -45.0, "close_time": t}],
        "opened_count": 1,
    })
    a.risk.cfg.daily_max_risk = 4.0
    a._restore_daily_state(955.0)          # kun boshi 1000, zarar 4.5%

    perm = a.risk.check_trade_allowed({"balance": 1000.0, "equity": 1000.0}, [], None)
    assert perm.checks["daily_limit"] is False, "kunlik zarar darvozasi ochiq qoldi"


def test_restore_respects_trade_cap():
    t = _now()
    a = _agent_with_activity({"closed": [], "opened_count": 12})
    a.risk.cfg.max_trades_per_day = 12
    a._restore_daily_state(1000.0)
    perm = a.risk.check_trade_allowed({"balance": 1000.0, "equity": 1000.0}, [], None)
    assert perm.checks["daily_trade_cap"] is False


def test_restore_noop_when_nothing_today():
    """Bugun savdo bo'lmasa holat tegilmaydi (va balans surilmaydi)."""
    a = _agent_with_activity({"closed": [], "opened_count": 0})
    a._starting_balance = 1234.0
    a._restore_daily_state(1234.0)
    assert a.risk._today_trades == []
    assert a._starting_balance == 1234.0


def test_restore_survives_mt5_error():
    """Tarix o'qib bo'lmasa bot BARIBIR ishga tushsin — start buzilmasin."""
    a = TraderAgent(config=TradingConfig(_env_file=None))
    a.mt5 = MagicMock()
    a.mt5.get_todays_activity.side_effect = RuntimeError("MT5 terminal javob bermadi")
    a._restore_daily_state(1000.0)          # otmasligi kerak
    assert a.risk._today_trades == []


# ══════════ T2: chiqishda pending orderlarni bekor qilish ══════════

def _agent_with_orders(orders, cancel_result=True):
    a = TraderAgent(config=TradingConfig(_env_file=None))
    a.mt5 = MagicMock()
    a.mt5.get_pending_orders.return_value = orders
    a.mt5.cancel_pending_order.return_value = cancel_result
    return a


def test_cancels_only_openclaw_orders():
    """XAVFSIZLIK: foydalanuvchi qo'lda qo'ygan orderlarga TEGILMAYDI.

    `get_pending_orders()` hisobdagi HAMMA orderni qaytaradi — magic
    filtri bo'lmasa bot odamning savdolarini o'chirib yuborardi.
    """
    a = _agent_with_orders([
        {"ticket": 111, "magic": OPENCLAW_MAGIC},
        {"ticket": 222, "magic": 0},            # qo'lda qo'yilgan
        {"ticket": 333, "magic": 999},          # boshqa bot
        {"ticket": 444, "magic": OPENCLAW_MAGIC},
    ])
    assert a.cancel_all_pending_orders() == 2
    cancelled = {c.args[0] for c in a.mt5.cancel_pending_order.call_args_list}
    assert cancelled == {111, 444}


def test_clears_pending_zones():
    a = _agent_with_orders([{"ticket": 1, "magic": OPENCLAW_MAGIC}])
    a._pending_zones = {1: {"label": "H1_OB"}}
    a.cancel_all_pending_orders()
    assert a._pending_zones == {}


def test_no_orders_returns_zero():
    a = _agent_with_orders([])
    assert a.cancel_all_pending_orders() == 0
    a.mt5.cancel_pending_order.assert_not_called()


def test_only_foreign_orders_returns_zero():
    a = _agent_with_orders([{"ticket": 9, "magic": 0}])
    assert a.cancel_all_pending_orders() == 0
    a.mt5.cancel_pending_order.assert_not_called()


def test_counts_only_successful_cancels():
    a = _agent_with_orders(
        [{"ticket": 1, "magic": OPENCLAW_MAGIC}, {"ticket": 2, "magic": OPENCLAW_MAGIC}],
        cancel_result=False,
    )
    assert a.cancel_all_pending_orders() == 0


def test_one_failure_does_not_stop_the_rest():
    """Bitta order xato bersa qolganlari baribir bekor qilinsin."""
    a = _agent_with_orders([
        {"ticket": 1, "magic": OPENCLAW_MAGIC},
        {"ticket": 2, "magic": OPENCLAW_MAGIC},
        {"ticket": 3, "magic": OPENCLAW_MAGIC},
    ])
    a.mt5.cancel_pending_order.side_effect = [True, RuntimeError("timeout"), True]
    assert a.cancel_all_pending_orders() == 2


def test_read_failure_returns_zero():
    a = TraderAgent(config=TradingConfig(_env_file=None))
    a.mt5 = MagicMock()
    a.mt5.get_pending_orders.side_effect = RuntimeError("MT5 uzildi")
    assert a.cancel_all_pending_orders() == 0


# ── Startda yetim pendinglarni tozalash (himoyaning 2-qavati) ──────────────
#
# Chiqishdagi tozalash signal handler'ga bog'liq va u FAQAT Ctrl+C /
# Ctrl+Break da ishlaydi. Oynani X bilan yopish, PC ni o'chirish yoki tok
# uzilishi uni chaqirmaydi — Windows'da CTRL_CLOSE_EVENT Python signaliga
# bog'lanmagan. Foydalanuvchi kompyuterni har kuni o'chiradi, ya'ni bu yo'l
# amalda eng ko'p uchraydigani. Shuning uchun startda ham tozalanadi.

def test_orphan_pendings_cancelled_on_start(monkeypatch):
    monkeypatch.delenv("CANCEL_PENDING_ON_EXIT", raising=False)
    a = _agent_with_orders([
        {"ticket": 11, "magic": OPENCLAW_MAGIC},
        {"ticket": 22, "magic": OPENCLAW_MAGIC},
    ])
    assert a._cancel_orphan_pendings() == 2


def test_orphan_cleanup_respects_env_switch(monkeypatch):
    """`CANCEL_PENDING_ON_EXIT=false` ikkala qavatni ham o'chiradi."""
    monkeypatch.setenv("CANCEL_PENDING_ON_EXIT", "false")
    a = _agent_with_orders([{"ticket": 11, "magic": OPENCLAW_MAGIC}])
    assert a._cancel_orphan_pendings() == 0
    a.mt5.cancel_pending_order.assert_not_called()


def test_orphan_cleanup_leaves_manual_orders(monkeypatch):
    """Startda ham faqat OpenClaw orderlariga tegiladi."""
    monkeypatch.delenv("CANCEL_PENDING_ON_EXIT", raising=False)
    a = _agent_with_orders([{"ticket": 77, "magic": 0}])
    assert a._cancel_orphan_pendings() == 0
    a.mt5.cancel_pending_order.assert_not_called()


def test_connect_calls_orphan_cleanup():
    """`connect()` haqiqatan startda tozalashni chaqiradi (source guard)."""
    agent_py = Path(__file__).resolve().parents[2] / "apps" / "api" / "src" / "agents" / "trader" / "agent.py"
    src = agent_py.read_text(encoding="utf-8")
    body = src.split("    def connect(")[1].split("\n    def ")[0]
    assert "_cancel_orphan_pendings()" in body, "startda yetim pending tozalanmayapti"
    assert "_restore_daily_state(" in body, "startda kunlik holat tiklanmayapti"


# ── main.py ulanishi (source guard) ─────────────────────────────────────────

_MAIN_PY = Path(__file__).resolve().parents[2] / "apps" / "api" / "main.py"


def test_shutdown_cleans_pending_before_telegram():
    """Tarmoq sekin bo'lsa ham brokerdagi orderlar qolib ketmasin."""
    src = _MAIN_PY.read_text(encoding="utf-8")
    body = src.split("def _shutdown_handler")[1].split("\n# Excepthook")[0]
    assert "_cleanup_pending(" in body, "shutdown pendinglarni tozalamayapti"
    assert body.index("_cleanup_pending(") < body.index("_tg_notify_shutdown"), (
        "pending tozalash Telegram xabaridan KEYIN turibdi"
    )


def test_run_agent_cleans_pending_in_finally():
    src = _MAIN_PY.read_text(encoding="utf-8")
    body = src.split("async def _run_agent")[1].split("\nasync def ")[0]
    assert "_cleanup_pending(" in body
    assert "_ACTIVE_AGENT = agent" in body, "signal handler agentga yeta olmaydi"


@pytest.mark.parametrize("value,expected", [("__UNSET__", True), ("false", False), ("off", False)])
def test_cancel_pending_env_switch(value, expected, tmp_path):
    """`CANCEL_PENDING_ON_EXIT` kaliti. main.py subprocess'da import qilinadi —
    u modul darajasida .env yuklaydi va global logger'ni o'zgartiradi."""
    root = _MAIN_PY.resolve().parents[2]
    probe = (
        "import json, os, sys, importlib\n"
        "from pathlib import Path\n"
        "root = Path(sys.argv[1]); sentinel = sys.argv[2]\n"
        "sys.path.insert(0, str(root)); sys.path.insert(0, str(root / 'apps' / 'api'))\n"
        "m = importlib.import_module('apps.api.main')\n"
        "os.environ.pop('CANCEL_PENDING_ON_EXIT', None) if sentinel == '__UNSET__' "
        "else os.environ.__setitem__('CANCEL_PENDING_ON_EXIT', sentinel)\n"
        "print('___R___' + json.dumps(m._cancel_pending_on_exit_enabled()))\n"
    )
    env = dict(os.environ)
    env["OPENCLAW_LOG_DIR"] = str(tmp_path / "logs")
    proc = subprocess.run(
        [sys.executable, "-c", probe, str(root), value],
        capture_output=True, text=True, timeout=180, env=env,
    )
    assert "___R___" in proc.stdout, proc.stderr[-2000:]
    assert proc.stdout.split("___R___", 1)[1].strip().startswith(str(expected).lower())
