"""
F5-2: kunlik zarar chegarasi 5.0% → 4.0% (2026-08-01).

MUAMMO: F5-1 xavfni 3% → 2% ga tushirdi, lekin `DAILY_MAX_RISK` 5.0 da qoldi.
2%/savdo da 5.0% = 2.5 ta to'liq zarar — ya'ni breaker "yarim savdo" joyida
turadi: 2 zarardan keyin (4.0%) bot HALI HAM yangi order qo'yadi, 3-chisi esa
chegaradan oshib ketadi. Aniq bo'lmagan chegara.

QAROR (Tasks.md F5-2): 4.0% — ANIQ 2 ta to'liq zarardan keyin "faqat
boshqarish" rejimi.

Bu fayl uchta darajani qotiradi:
  1. Jonli `.env` qiymatlari         — 4.0 / 2.0 (drift'ga qarshi)
  2. Cross-field invariantlar        — daily >= risk, dd >= daily
  3. HAQIQIY xulq                    — `RiskManagement.check_trade_allowed`
                                        1-zarardan keyin ruxsat, 2-dan keyin YO'Q

Va F5-1 sinfidagi bugga qarshi source-guard: o'sha bug'da `agent.py` xavfni
`.env`'dan emas, QATTIQ YOZILGAN konstantadan o'qigan va `.env` jim
e'tiborga olinmagan. Shu sabab kunlik darvoza ham `self.config.daily_max_risk`
ni o'qishi test bilan qotiriladi.

Determinizm: `frozen_now` (2026-05-14 12:00 UTC = payshanba) + `set_clock(None)`
— `check_trade_allowed` ichidagi weekend-guard va today-filter global soatga
tayanadi, leaked VirtualClock testni nondeterministik qilib qo'yardi.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from dotenv import dotenv_values
from pydantic import ValidationError

from apps.api.src.agents.trader.core.clock import get_clock, set_clock
from apps.api.src.agents.trader.models.config import RiskConfig, TradingConfig
from apps.api.src.agents.trader.risk.manager import RiskManagement

_ROOT = Path(__file__).resolve().parents[2]
_ENV_PATH = _ROOT / ".env"
_AGENT_PATH = _ROOT / "apps" / "api" / "src" / "agents" / "trader" / "agent.py"

# F5-2 kelishilgan qiymatlar — o'zgartirilsa bu testlar ataylab yiqiladi.
EXPECTED_DAILY_MAX_RISK = 4.0
EXPECTED_RISK_PER_TRADE = 2.0
OLD_DAILY_MAX_RISK = 5.0  # F5-2 dan oldingi qiymat (regression kontrasti)


def _live_env_values() -> dict[str, str]:
    """Jonli `.env` → UPPER-key dict. Fayl yo'q bo'lsa (fresh clone) skip."""
    if not _ENV_PATH.exists():
        pytest.skip(".env topilmadi — jonli config testlari o'tkazib yuborildi")
    return {
        k.upper(): v
        for k, v in dotenv_values(str(_ENV_PATH)).items()
        if v is not None
    }


def _live_config() -> TradingConfig:
    """Jonli `.env` qiymatlaridan TradingConfig — strict validation bilan.

    `_env_file=None` + aniq kwargs: pydantic env_file yo'lini emas, aynan
    shu fayldan o'qilgan qiymatlarni tekshirayotganimizga kafolat.
    """
    values = _live_env_values()
    fields = TradingConfig.model_fields
    kwargs = {k.lower(): v for k, v in values.items() if k.lower() in fields}
    return TradingConfig(_env_file=None, **kwargs)


# ── 1. Jonli `.env` qiymatlari ───────────────────────────────────────────────

def test_live_env_daily_max_risk_is_4pct() -> None:
    """`.env DAILY_MAX_RISK` = 4.0 (F5-2 dan keyin 5.0 EMAS)."""
    values = _live_env_values()
    assert "DAILY_MAX_RISK" in values, ".env'da DAILY_MAX_RISK yo'q"
    actual = float(values["DAILY_MAX_RISK"])
    assert actual == EXPECTED_DAILY_MAX_RISK, (
        f"DAILY_MAX_RISK={actual} (F5-2 kutgani {EXPECTED_DAILY_MAX_RISK}). "
        f"Eski qiymat {OLD_DAILY_MAX_RISK} = 2.5 to'liq zarar — noaniq chegara."
    )


def test_live_env_risk_per_trade_still_2pct() -> None:
    """F5-1 qiymati saqlanib qolgan — 4.0% hisobi 2.0% ga bog'liq."""
    values = _live_env_values()
    actual = float(values["RISK_PER_TRADE"])
    assert actual == EXPECTED_RISK_PER_TRADE, (
        f"RISK_PER_TRADE={actual} (F5-1 kutgani {EXPECTED_RISK_PER_TRADE}). "
        f"O'zgargan bo'lsa DAILY_MAX_RISK ni ham qayta hisoblash kerak."
    )


def test_daily_cap_is_exactly_two_full_risk_losses() -> None:
    """F5-2 mohiyati: chegara ANIQ 2 ta to'liq zararga teng (kasr emas)."""
    cfg = _live_config()
    ratio = cfg.daily_max_risk / cfg.risk_per_trade
    assert ratio == 2.0, (
        f"daily_max_risk/risk_per_trade = {ratio} (kutilgan 2.0). "
        f"Butun son bo'lmasa breaker savdo o'rtasida turadi."
    )


# ── 2. Cross-field invariantlar ──────────────────────────────────────────────

def test_live_config_invariants_hold() -> None:
    """Jonli `.env` strict validation'dan o'tadi va invariantlar buzilmagan."""
    cfg = _live_config()
    assert cfg.daily_max_risk >= cfg.risk_per_trade
    assert cfg.max_drawdown >= cfg.daily_max_risk, (
        f"max_drawdown={cfg.max_drawdown} < daily_max_risk={cfg.daily_max_risk} — "
        f"DD breaker kunlik breaker'dan oldin ishlab ketadi"
    )

    # get_risk_config() ham xuddi shu qiymatlarni ko'chiradi (RiskConfig
    # konstruktori invariantlarni qayta validate qiladi).
    rc = cfg.get_risk_config()
    assert rc.daily_max_risk == cfg.daily_max_risk == EXPECTED_DAILY_MAX_RISK
    assert rc.risk_per_trade == cfg.risk_per_trade == EXPECTED_RISK_PER_TRADE


def test_config_rejects_daily_below_risk_at_new_value() -> None:
    """4.0% chegara risk_per_trade'dan past bo'lsa startup RAD etadi."""
    with pytest.raises(ValidationError) as exc:
        TradingConfig(
            _env_file=None,
            daily_max_risk=EXPECTED_DAILY_MAX_RISK,
            risk_per_trade=5.0,   # > 4.0 → invariant buziladi
        )
    assert "daily_max_risk" in str(exc.value).lower()


def test_config_rejects_drawdown_below_new_daily_cap() -> None:
    """max_drawdown 4.0% dan past bo'lsa ham RAD etiladi."""
    with pytest.raises(ValidationError) as exc:
        TradingConfig(
            _env_file=None,
            daily_max_risk=EXPECTED_DAILY_MAX_RISK,
            risk_per_trade=EXPECTED_RISK_PER_TRADE,
            max_drawdown=3.0,     # < 4.0 → invariant buziladi
        )
    assert "max_drawdown" in str(exc.value).lower()


# ── 3. Haqiqiy xulq — breaker qachon yopiladi ────────────────────────────────

def _full_risk_loss(balance: float, risk_pct: float) -> float:
    """Bitta to'liq SL zarari, $ (manfiy)."""
    return -balance * risk_pct / 100.0


def test_breaker_allows_first_full_loss_blocks_second(frozen_now) -> None:
    """2%/savdo, 4% kunlik: 1-zarardan keyin savdo OCHIQ, 2-dan keyin YOPIQ.

    Bu F5-2 ning butun maqsadi — chegara aynan 2-zararda ishlashi.
    """
    set_clock(None)   # RealClock majburlansin (leaked VirtualClock'dan himoya)
    try:
        cfg = _live_config()
        rm = RiskManagement(cfg.get_risk_config())

        balance = 10_000.0
        account = {"balance": balance, "equity": balance}
        loss = _full_risk_loss(balance, cfg.risk_per_trade)   # -$200.0
        assert loss == -200.0

        now = get_clock().now()

        # Zararsiz holat — chegara ochiq.
        assert rm.check_trade_allowed(account, [], None).checks["daily_limit"] is True

        # 1-to'liq zarar → 2.0% ishlatildi, 4.0% dan past → HALI OCHIQ.
        rm.add_closed_trade({"pnl": loss, "close_time": now})
        perm1 = rm.check_trade_allowed(account, [], None)
        assert perm1.checks["daily_limit"] is True, (
            "1 ta zarardan keyin bot to'xtab qoldi — chegara juda tor"
        )

        # 2-to'liq zarar → 4.0% == chegara → YOPILADI (manager `<` ishlatadi).
        rm.add_closed_trade({"pnl": loss, "close_time": now})
        perm2 = rm.check_trade_allowed(account, [], None)
        assert perm2.checks["daily_limit"] is False, (
            "2 ta to'liq zarardan keyin ham yangi savdoga ruxsat berildi — "
            "F5-2 chegarasi ishlamayapti"
        )
        # Bitta check yiqilsa umumiy ruxsat ham yiqiladi.
        assert perm2.allowed is False
    finally:
        set_clock(None)


def test_old_5pct_threshold_would_not_have_blocked(frozen_now) -> None:
    """Regression kontrasti: eski 5.0% da 2 zarardan keyin bot savdoni davom
    ettirardi — F5-2 o'zgarishi haqiqatan xulqni o'zgartirgani shu bilan
    isbotlanadi (aks holda test tavtologiya bo'lardi)."""
    set_clock(None)
    try:
        rm_old = RiskManagement(RiskConfig(
            risk_per_trade=EXPECTED_RISK_PER_TRADE,
            daily_max_risk=OLD_DAILY_MAX_RISK,
        ))
        balance = 10_000.0
        account = {"balance": balance, "equity": balance}
        loss = _full_risk_loss(balance, EXPECTED_RISK_PER_TRADE)
        now = get_clock().now()

        rm_old.add_closed_trade({"pnl": loss, "close_time": now})
        rm_old.add_closed_trade({"pnl": loss, "close_time": now})

        # 4.0% ishlatildi, eski chegara 5.0% → hali OCHIQ (aynan shu muammo edi).
        assert rm_old.check_trade_allowed(account, [], None).checks["daily_limit"] is True

        # Eski sozlamada faqat 3-zarardan keyin (6.0%) yopilardi.
        rm_old.add_closed_trade({"pnl": loss, "close_time": now})
        assert rm_old.check_trade_allowed(account, [], None).checks["daily_limit"] is False
    finally:
        set_clock(None)


def test_partial_losses_below_cap_keep_trading(frozen_now) -> None:
    """To'liq bo'lmagan zararlar (SL'gacha yetmagan) chegarani erta yopmaydi."""
    set_clock(None)
    try:
        cfg = _live_config()
        rm = RiskManagement(cfg.get_risk_config())
        balance = 10_000.0
        account = {"balance": balance, "equity": balance}
        now = get_clock().now()

        # 3 ta kichik zarar: -$50 × 3 = -$150 = 1.5% < 4.0%
        for _ in range(3):
            rm.add_closed_trade({"pnl": -50.0, "close_time": now})
        assert rm.check_trade_allowed(account, [], None).checks["daily_limit"] is True

        # Yutuqlar chegarani NETLAMAYDI — faqat manfiy pnl hisoblanadi.
        rm.add_closed_trade({"pnl": +500.0, "close_time": now})
        assert rm.check_trade_allowed(account, [], None).checks["daily_limit"] is True
    finally:
        set_clock(None)


# ── 4. F5-1 sinfidagi bugga qarshi source-guard ──────────────────────────────

def test_agent_daily_gate_reads_config_not_hardcode() -> None:
    """`agent.py` kunlik darvozasi `.env` qiymatini o'qishi SHART.

    F5-1 da aynan shu sinf bug bo'lgan: `RISK_SNIPER=0.03` qattiq yozilgan
    edi va `.env RISK_PER_TRADE` jim e'tiborga olinmagan. Kunlik darvoza
    ham shunday "muzlab" qolmasligi uchun guard.
    """
    src = _AGENT_PATH.read_text(encoding="utf-8")

    assert "self.config.daily_max_risk" in src, (
        "agent.py kunlik darvozasi endi config'dan o'qimayapti — "
        ".env DAILY_MAX_RISK e'tiborga olinmay qolgan bo'lishi mumkin (F5-1 sinfi)"
    )

    # `daily_loss_pct >= 5` ko'rinishidagi qattiq yozilgan chegara bo'lmasin.
    hardcoded = re.search(r"daily_loss_pct\s*>=\s*\d", src)
    assert hardcoded is None, (
        f"agent.py'da qattiq yozilgan kunlik chegara topildi: {hardcoded.group(0)!r}"
    )
