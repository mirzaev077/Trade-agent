"""
A2: savdo jurnali (trades.csv) yaxlitligi — 2026-08-01 da topilgan 3 ta bug.

Hodisa: `brain/trades.csv` da ikkita ishonchsiz qator bor edi.

  1) 2026-07-08 14:46 — ARVOH SAVDO
     `buy XAUUSDm entry=2000.0 sl=1995.7 tp=2008.6 lot=0.25 pnl=-4.30 H1_OB`
     O'sha kuni real narx ~4043 edi, 2000 emas. Bu qiymatlar aynan
     `tests/unit/test_market_near_entry.py` mock'lariga teng.

     Zanjir: `state/persistence.py:get_state_dir()` `OPENCLAW_STATE_DIR` ni
     O'QIMAGAN (holbuki `state/db.py:get_db_path()` o'qiydi) → `tmp_state_dir`
     fixture'i testni izolyatsiya qilmagan → `pytest` PRODUCTION
     `apps/data/state/trade_meta.json` ga mock ticket=111 ni yozgan → jonli
     bot startda uni yuklagan, MT5'da topmagan → "yopilgan" deb hisoblab
     `trades.csv` ga soxta qator yozgan.

  2) O'SHA QATORNING PnL'i HAM NOTO'G'RI
     `agent.py` fallback formulasi `pnl = -sl_pips * 0.1` HAJMNI hisobga
     olmagan — u faqat 0.01 lot uchun to'g'ri. 43 pip × 0.25 lot ning
     haqiqiy qiymati $107.50, yozilgani -$4.30 (25 barobar kam).

  3) 2026-07-08 17:22 — exit == entry
     `entry=4069.99 exit=4069.99 pnl=+4.91` — narx siljimagan bo'lsa PnL
     nolga teng bo'lishi kerak edi. Sabab: `get_closed_position()` deal
     turini tekshirmagan va OCHILISH dealining narxini chiqish narxi
     sifatida yozgan ("Closing deal has non-zero price" izohi noto'g'ri —
     ochilish dealida ham price > 0).

Bu fayl uchala tuzatishni va takrorlanishga qarshi himoyani qotiradi.
"""
from __future__ import annotations

import csv as _csv
import os

import pytest

from apps.api.src.agents.trader import mt5_connector as mt5_mod
from apps.api.src.agents.trader.agent import TraderAgent
from apps.api.src.agents.trader.models.config import TradingConfig
from apps.api.src.agents.trader.state import persistence
from apps.api.src.agents.trader.utils import trade_analytics as ta

# Haqiqiy hodisa raqamlari (trades.csv 2026-07-08 14:46 qatori)
GHOST_SL_PIPS = 43.0
GHOST_LOT = 0.25
GHOST_RECORDED_PNL = -4.30     # eski buggy formula yozgani
GHOST_CORRECT_PNL = -107.50    # 43 × $10/pip/lot × 0.25


# ── Group A: state izolyatsiyasi — arvoh savdoning ILDIZ SABABI ──────────────

def test_get_state_dir_honors_env(tmp_path, monkeypatch):
    """`OPENCLAW_STATE_DIR` o'rnatilgan bo'lsa shu papka ishlatiladi."""
    target = tmp_path / "custom_state"
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(target))
    assert persistence.get_state_dir() == target
    assert target.is_dir()   # yo'q bo'lsa yaratiladi


def test_get_state_dir_env_read_at_call_time(tmp_path, monkeypatch):
    """Env import paytida emas, CHAQIRUV paytida o'qiladi (monkeypatch ishlasin)."""
    first, second = tmp_path / "a", tmp_path / "b"
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(first))
    assert persistence.get_state_dir() == first
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(second))
    assert persistence.get_state_dir() == second


def test_explicit_state_dir_beats_env(tmp_path, monkeypatch):
    """Aniq argument env'dan ustun (back-compat: mavjud chaqiruvlar buzilmasin)."""
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path / "env_dir"))
    explicit = tmp_path / "explicit"
    assert persistence.get_state_dir(explicit) == explicit


def test_save_trade_meta_never_touches_production(tmp_path, monkeypatch):
    """ILDIZ SABAB regressioni: env o'rnatilgan bo'lsa jonli fayl TEGILMAYDI.

    Aynan shu ishlamagani uchun `pytest` production `trade_meta.json` ga
    mock ticket=111 (entry=2000.0) ni yozib qo'ygan edi.
    """
    prod = persistence._DEFAULT_STATE_DIR / "trade_meta.json"
    prod_before = prod.read_bytes() if prod.exists() else None

    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path))
    ghost = {111: {"entry": 2000.0, "sl": 1995.7, "lot": 0.25, "label": "H1_OB"}}
    persistence.save_trade_meta(ghost)

    # Tmp papkaga yozildi va o'sha yerdan o'qiladi.
    assert (tmp_path / "trade_meta.json").exists()
    assert persistence.load_trade_meta()[111]["entry"] == 2000.0

    # Production fayl o'zgarmadi (yo'q bo'lsa — yaratilmadi ham).
    prod_after = prod.read_bytes() if prod.exists() else None
    assert prod_after == prod_before, (
        "test PRODUCTION trade_meta.json ga yozdi — A2 arvoh savdo bugi qaytdi"
    )


def test_autouse_isolation_is_active():
    """conftest `_isolate_production_files` har testda ishlayotganini tasdiqlash."""
    assert os.getenv("OPENCLAW_STATE_DIR"), "autouse izolyatsiya fixture'i ishlamadi"
    assert os.getenv("OPENCLAW_TRADES_CSV"), "trades.csv izolyatsiyasi yo'q"
    # Va u jonli papkaga ishora qilmasin.
    assert os.path.abspath(os.getenv("OPENCLAW_STATE_DIR")) != str(
        persistence._DEFAULT_STATE_DIR
    )


# ── Group B: chiqish narxi — ochilish deali chiqish deb yozilmasin ───────────

class _FakeDeal:
    """MT5 `history_deals_get()` elementi (kerakli maydonlar bilan)."""

    def __init__(self, position_id, entry, price, volume=0.0,
                 profit=0.0, swap=0.0, commission=0.0):
        self.position_id = position_id
        self.entry = entry
        self.price = price
        self.volume = volume
        self.profit = profit
        self.swap = swap
        self.commission = commission


_IN = getattr(mt5_mod.mt5, "DEAL_ENTRY_IN", 0)
_OUT = getattr(mt5_mod.mt5, "DEAL_ENTRY_OUT", 1)


def _connector_with_deals(monkeypatch, deals):
    conn = mt5_mod.MT5Connector()
    conn._sim_mode = False   # sim rejimda metod darhol None qaytaradi
    monkeypatch.setattr(mt5_mod.mt5, "history_deals_get", lambda *a, **k: deals)
    return conn


def test_close_price_uses_closing_deal_not_opening(monkeypatch):
    """2026-07-08 17:22 qatori regressioni: exit != entry bo'lishi shart.

    Sell 4069.99 da ochilgan, 4068.35 da yopilgan. Eski kod ikkala deal'ni
    ham qabul qilgani uchun natija deal tartibiga bog'liq edi.
    """
    deals = [
        _FakeDeal(555, _OUT, 4068.35, volume=0.03, profit=4.91),   # yopilish
        _FakeDeal(555, _IN, 4069.99, volume=0.03, commission=-0.1),  # ochilish
    ]
    res = _connector_with_deals(monkeypatch, deals).get_closed_position(555)
    assert res["price_close"] == 4068.35, (
        f"chiqish narxi {res['price_close']} — ochilish narxi (4069.99) yozilgan bo'lishi mumkin"
    )
    assert res["price_close"] != 4069.99


def test_close_price_volume_weighted_for_partial_closes(monkeypatch):
    """Qisman yopilishlarda hajm bo'yicha o'rtacha narx olinadi."""
    deals = [
        _FakeDeal(777, _IN, 4000.00, volume=0.10),
        _FakeDeal(777, _OUT, 4010.00, volume=0.04, profit=40.0),
        _FakeDeal(777, _OUT, 4020.00, volume=0.06, profit=120.0),
    ]
    res = _connector_with_deals(monkeypatch, deals).get_closed_position(777)
    expected = (4010.0 * 0.04 + 4020.0 * 0.06) / 0.10   # 4016.0
    assert res["price_close"] == pytest.approx(expected)
    assert res["profit"] == pytest.approx(160.0)


def test_close_price_zero_when_only_opening_deal(monkeypatch):
    """Yopuvchi deal yo'q → 0.0 ("noma'lum"), ochilish narxi EMAS.

    agent.py 0.0 ni ko'rib SL-taxmin yo'liga o'tadi; ochilish narxi yozilsa
    esa jimgina noto'g'ri "exit" CSV'ga tushib ketardi.
    """
    deals = [_FakeDeal(999, _IN, 4055.55, volume=0.02)]
    res = _connector_with_deals(monkeypatch, deals).get_closed_position(999)
    assert res["price_close"] == 0.0


def test_profit_includes_all_deals_of_position(monkeypatch):
    """PnL barcha deal'lardan yig'iladi (ochilish komissiyasi ham)."""
    deals = [
        _FakeDeal(321, _IN, 4000.0, volume=0.05, commission=-0.50),
        _FakeDeal(321, _OUT, 4010.0, volume=0.05, profit=50.0, swap=-0.25),
        _FakeDeal(888, _OUT, 9999.0, volume=1.0, profit=1000.0),   # boshqa pozitsiya
    ]
    res = _connector_with_deals(monkeypatch, deals).get_closed_position(321)
    assert res["profit"] == pytest.approx(49.25)   # boshqa pozitsiya aralashmadi


def test_unknown_ticket_returns_none(monkeypatch):
    deals = [_FakeDeal(1, _OUT, 4000.0, volume=0.01)]
    assert _connector_with_deals(monkeypatch, deals).get_closed_position(42) is None


# ── Group C: PnL taxmini hajmga mutanosib bo'lsin ────────────────────────────

def _agent(symbol_info: dict | None = None) -> TraderAgent:
    from unittest.mock import MagicMock
    agent = TraderAgent(config=TradingConfig(_env_file=None))
    mock = MagicMock(name="MT5Connector")
    mock.get_symbol_info.return_value = symbol_info
    agent.mt5 = mock
    return agent


def test_money_per_pips_scales_with_lot():
    """A2 asosiy tuzatish: qiymat lot'ga CHIZIQLI bog'liq."""
    agent = _agent({"point": 0.01, "trade_tick_value": 1.0})
    base = agent._money_per_pips(GHOST_SL_PIPS, 0.01)
    assert agent._money_per_pips(GHOST_SL_PIPS, 0.02) == pytest.approx(base * 2)
    assert agent._money_per_pips(GHOST_SL_PIPS, 0.25) == pytest.approx(base * 25)


def test_ghost_row_pnl_is_now_correct():
    """Haqiqiy hodisa raqamlari: 43 pip × 0.25 lot = $107.50, $4.30 EMAS."""
    agent = _agent({"point": 0.01, "trade_tick_value": 1.0})
    loss = agent._money_per_pips(GHOST_SL_PIPS, GHOST_LOT)
    assert loss == pytest.approx(abs(GHOST_CORRECT_PNL))
    # Eski formula (`-sl_pips * 0.1`) 25 barobar kam ko'rsatgan.
    assert loss == pytest.approx(abs(GHOST_RECORDED_PNL) * 25)


def test_money_per_pips_uses_broker_metrics():
    """Broker point/tick_value berilsa aynan shulardan hisoblanadi."""
    agent = _agent({"point": 0.001, "trade_tick_value": 1.0})
    # 10 pip = 1.0 narx; point 0.001 → 1000 point × $1 × 0.05 lot = $50
    assert agent._money_per_pips(10.0, 0.05) == pytest.approx(50.0)


def test_money_per_pips_falls_back_without_symbol_info():
    """Symbol info yo'q → XAUUSD konstantasi ($10/pip/lot)."""
    assert _agent(None)._money_per_pips(50.0, 0.04) == pytest.approx(20.0)


def test_money_per_pips_survives_broker_error():
    """get_symbol_info exception tashlasa ham taxmin ishlaydi (tick buzilmaydi)."""
    agent = _agent(None)
    agent.mt5.get_symbol_info.side_effect = RuntimeError("MT5 disconnected")
    assert agent._money_per_pips(50.0, 0.04) == pytest.approx(20.0)


@pytest.mark.parametrize("pips,lot", [(0, 0.10), (50, 0), (0, 0), (-50, 0.10)])
def test_money_per_pips_zero_guards(pips, lot):
    """Nol/manfiy kiritmalar 0.0 qaytaradi (manfiy pul yo'q)."""
    result = _agent({"point": 0.01, "trade_tick_value": 1.0})._money_per_pips(pips, lot)
    assert result >= 0.0
    if lot == 0 or pips == 0:
        assert result == 0.0


def test_real_journal_rows_are_reproducible():
    """Formula haqiqiy (ishonchli) jurnal qatorlarini qayta hosil qiladi.

    trades.csv 2026-07-08 17:31: 50 pip SL, 0.04 lot → -$20.00 (yozilgani ham
    shunday). Ya'ni yangi formula mavjud to'g'ri qatorlarga ZID emas.
    """
    agent = _agent({"point": 0.01, "trade_tick_value": 1.0})
    assert agent._money_per_pips(50.0, 0.04) == pytest.approx(20.00)   # 17:31 qatori
    assert agent._money_per_pips(47.3, 0.04) == pytest.approx(18.92)   # 16:55 qatori


# ── Group D: jurnal yo'li override qilinadi ──────────────────────────────────

def test_log_trade_csv_honors_env_path(tmp_path, monkeypatch):
    """`OPENCLAW_TRADES_CSV` jonli jurnal o'rniga boshqa faylga yozadi."""
    target = tmp_path / "isolated_trades.csv"
    monkeypatch.setenv("OPENCLAW_TRADES_CSV", str(target))
    assert ta.get_csv_path() == str(target)

    ta.log_trade_csv(
        direction="sell", symbol="XAUUSDm", entry=4070.0, exit_price=4068.0,
        sl=4074.0, tp1=4062.0, lot=0.04, pnl=-8.0, result="loss",
        label="M5_OB", tf="M5", mode="FLOW", session="newyork", regime="range",
        sl_pips=40.0, tp_pips=80.0,
    )
    with open(target, newline="", encoding="utf-8") as f:
        rows = list(_csv.DictReader(f))
    assert len(rows) == 1
    assert float(rows[0]["lot"]) == 0.04
    assert float(rows[0]["exit"]) == 4068.0


def test_csv_path_defaults_to_journal_when_env_unset(monkeypatch):
    """Env bo'lmasa jonli `brain/trades.csv` (production xulqi o'zgarmagan)."""
    monkeypatch.delenv("OPENCLAW_TRADES_CSV", raising=False)
    assert ta.get_csv_path().replace("\\", "/").endswith("brain/trades.csv")
