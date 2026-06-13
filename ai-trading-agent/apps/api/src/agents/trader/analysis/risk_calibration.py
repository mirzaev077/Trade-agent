"""F4-4: Worst-case drawdown → risk-per-trade kalibratsiyasi (RiskConfig lever).

Monte-Carlo (`monte_carlo.py`) bizga *eng yomon holatdagi* drawdown'ni beradi.
Lekin backtest engine `StubRisk` (qat'iy 0.01 lot) ishlatadi — savdo PnL'lari
`risk_per_trade`'ga BOG'LIQ EMAS. Shu sababli xom $-DD'ni to'g'ridan-to'g'ri
`risk_per_trade`'ga ko'paytirib bo'lmaydi: u qat'iy lotga bog'langan.

To'g'ri ko'prik — **R-multiple** (har savdo natijasi o'z riskiga nisbatan):

    R_i = pnl_i / risk_dollars_i,   risk_dollars_i = |entry-sl| * lot * contract_size

Broker PnL = Δprice * lot * contract_size bo'lgani uchun `lot` va `contract_size`
qisqaradi → R sizing'dan MUSTAQIL (faqat narx harakati / SL masofasi). Shu R
ketma-ketligini Monte-Carlo'da 1% reference risk bilan aralashtirsak (1R ≈ 1%
balans), `worst_case_dd_pct` = "1% risk/trade'da eng yomon DD". Birinchi tartibli
(linear, non-compounding) yaqinlashish:

    worst_case_dd_pct(risk r%)  ≈  worst_dd_per_1pct * r

Bu fixed-fractional sizing'ning standart qoidasi. Sequencing/clustering riski
(qiyin qism) Monte-Carlo'da allaqachon hisobga olingan; r% ga chiziqli masshtab
faqat yaqinlashish. Shundan kelib chiqib, DD budjetiga (= RiskConfig.max_drawdown)
mos keladigan tavsiya etilgan risk:

    recommended_risk_pct  =  dd_budget_pct / worst_dd_per_1pct   (bounds'ga clamp)

Modul HECH NIMANI avto-o'zgartirmaydi — faqat tavsiya + ogohlantirish beradi
(F1-1 approval-gate falsafasiga mos). Wire-up `offline_validation.py`'da.

Ishlatish:
    from .risk_calibration import RiskCalibrationConfig, calibrate
    res = calibrate(closed_trades, RiskCalibrationConfig(
        dd_budget_pct=10.0,      # RiskConfig.max_drawdown
        current_risk_pct=1.0,    # RiskConfig.risk_per_trade
    ))
    print(res.recommended_risk_pct, res.verdict)
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from .monte_carlo import MonteCarloConfig, MonteCarloSimulator

# Reference risk darajasi: 1R = 1% balans deb modellaymiz. initial_balance=100
# → 1 unit = 1% (max_drawdown_pct natijasi to'g'ridan-to'g'ri foizda chiqadi).
_REF_RISK_PCT = 1.0
_REF_BALANCE = 100.0
_EPS = 1e-9


@dataclass(frozen=True)
class RiskCalibrationConfig:
    """Worst-case DD → risk_per_trade kalibratsiya parametrlari."""

    dd_budget_pct: float          # ruxsat etilgan eng yomon DD shifti (= RiskConfig.max_drawdown)
    current_risk_pct: float       # joriy jonli risk_per_trade (% per trade)
    contract_size: float = 100.0  # XAUUSD = 100 (R hisobida lot bilan qisqaradi, mos bo'lishi shart)
    risk_floor_pct: float = 0.1   # tavsiyaning pastki chegarasi
    risk_ceiling_pct: float = 5.0 # RiskConfig.risk_per_trade le=5.0 bound
    n_simulations: int = 10_000
    min_trades: int = 20          # bundan kam valid trade → INSUFFICIENT_DATA
    headroom_factor: float = 1.25 # rec >= current*1.25 → ROOM_TO_INCREASE
    seed: int = 42

    def __post_init__(self) -> None:
        if self.dd_budget_pct <= 0:
            raise ValueError("dd_budget_pct > 0 bo'lishi kerak")
        if self.current_risk_pct <= 0:
            raise ValueError("current_risk_pct > 0 bo'lishi kerak")
        if self.contract_size <= 0:
            raise ValueError("contract_size > 0 bo'lishi kerak")
        if not (0 < self.risk_floor_pct <= self.risk_ceiling_pct):
            raise ValueError("0 < risk_floor_pct <= risk_ceiling_pct bo'lishi kerak")
        if self.headroom_factor < 1.0:
            raise ValueError("headroom_factor >= 1.0 bo'lishi kerak")


@dataclass(frozen=True)
class RiskCalibrationResult:
    n_trades_total: int
    n_trades_valid: int           # R hisoblanadigan (SL bor) savdolar
    n_trades_dropped: int         # SL yo'q / risk<=0 → tashlab yuborilgan
    avg_r_multiple: float

    # 1% reference risk'dagi Monte-Carlo DD (sizing'dan mustaqil)
    worst_dd_per_1pct: float
    p95_dd_per_1pct: float
    expected_max_losing_streak_p95: int

    # Joriy risk'dagi proyeksiya (linear yaqinlashish)
    projected_worst_dd_pct: float
    projected_p95_dd_pct: float
    within_budget: bool

    # Tavsiya
    dd_budget_pct: float
    current_risk_pct: float
    recommended_risk_pct: float

    verdict: str   # OK | REDUCE_RISK | ROOM_TO_INCREASE | INSUFFICIENT_DATA
    notes: str

    def to_dict(self) -> dict:
        return {
            "n_trades_total": self.n_trades_total,
            "n_trades_valid": self.n_trades_valid,
            "n_trades_dropped": self.n_trades_dropped,
            "avg_r_multiple": self.avg_r_multiple,
            "worst_dd_per_1pct": self.worst_dd_per_1pct,
            "p95_dd_per_1pct": self.p95_dd_per_1pct,
            "expected_max_losing_streak_p95": self.expected_max_losing_streak_p95,
            "projected_worst_dd_pct": self.projected_worst_dd_pct,
            "projected_p95_dd_pct": self.projected_p95_dd_pct,
            "within_budget": self.within_budget,
            "dd_budget_pct": self.dd_budget_pct,
            "current_risk_pct": self.current_risk_pct,
            "recommended_risk_pct": self.recommended_risk_pct,
            "verdict": self.verdict,
            "notes": self.notes,
        }


# ── R-multiple ─────────────────────────────────────────────────────────────────


def _trade_r_multiple(trade: dict, contract_size: float) -> float | None:
    """Bitta yopilgan savdo → R-multiple, yoki hisoblab bo'lmasa None.

    R = pnl / (|entry-sl| * lot * contract_size). pnl allaqachon ishorali
    (yo'nalishni o'zi hisobga oladi). SL yo'q / lot<=0 / risk<=0 → None.
    """
    entry = trade.get("entry_price")
    sl = trade.get("sl")
    lot = trade.get("lot")
    pnl = trade.get("pnl")
    if entry is None or sl is None or lot is None or pnl is None:
        return None
    try:
        entry_f = float(entry)
        sl_f = float(sl)
        lot_f = float(lot)
        pnl_f = float(pnl)
    except (TypeError, ValueError):
        return None
    risk_dollars = abs(entry_f - sl_f) * lot_f * contract_size
    if not math.isfinite(risk_dollars) or risk_dollars <= _EPS:
        return None
    r = pnl_f / risk_dollars
    if not math.isfinite(r):
        return None
    return r


def r_multiples(closed_trades: Iterable[dict], contract_size: float = 100.0) -> tuple[list[float], int]:
    """Yopilgan savdolardan R-multiple ro'yxati + tashlab yuborilganlar soni.

    Returns: (R ro'yxati, dropped_count). dropped = SL yo'q yoki risk<=0 savdolar.
    """
    rs: list[float] = []
    dropped = 0
    for t in closed_trades:
        r = _trade_r_multiple(t, contract_size)
        if r is None:
            dropped += 1
        else:
            rs.append(r)
    return rs, dropped


# ── Kalibratsiya ─────────────────────────────────────────────────────────────


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(x, hi))


def calibrate(
    closed_trades: Iterable[dict],
    config: RiskCalibrationConfig,
) -> RiskCalibrationResult:
    """Worst-case DD'ni risk_per_trade tavsiyasiga aylantiradi.

    Qadamlar:
      1. closed_trades → R-multiple (sizing'dan mustaqil).
      2. Monte-Carlo 1% reference risk'da → worst_dd_per_1pct.
      3. Linear: projected_dd(current) = worst_dd_per_1pct * current_risk.
      4. recommended = dd_budget / worst_dd_per_1pct, bounds'ga clamp.
    """
    cfg = config
    trades = list(closed_trades)
    n_total = len(trades)
    rs, dropped = r_multiples(trades, cfg.contract_size)
    n_valid = len(rs)
    avg_r = (sum(rs) / n_valid) if n_valid else 0.0

    # Yetarli ma'lumot yo'q → o'zgartirmaymiz, ogohlantiramiz.
    if n_valid < cfg.min_trades:
        return RiskCalibrationResult(
            n_trades_total=n_total,
            n_trades_valid=n_valid,
            n_trades_dropped=dropped,
            avg_r_multiple=avg_r,
            worst_dd_per_1pct=0.0,
            p95_dd_per_1pct=0.0,
            expected_max_losing_streak_p95=0,
            projected_worst_dd_pct=0.0,
            projected_p95_dd_pct=0.0,
            within_budget=True,
            dd_budget_pct=cfg.dd_budget_pct,
            current_risk_pct=cfg.current_risk_pct,
            recommended_risk_pct=cfg.current_risk_pct,
            verdict="INSUFFICIENT_DATA",
            notes=(
                f"Faqat {n_valid} valid trade (min {cfg.min_trades}). "
                f"{dropped} ta SL/risk yo'qligi sabab tashlandi. "
                "Kalibratsiya ishonchsiz - risk_per_trade o'zgartirilmadi."
            ),
        )

    # 1% reference risk: pnls = R_i (1R = 1 unit = 1% of 100-balans).
    mc_cfg = MonteCarloConfig(
        n_simulations=cfg.n_simulations,
        initial_balance=_REF_BALANCE,
        seed=cfg.seed,
    )
    mc = MonteCarloSimulator(mc_cfg).run([r * _REF_RISK_PCT for r in rs])
    worst_per_1pct = mc.worst_case_dd_pct
    p95_per_1pct = mc.max_dd_p95
    streak = mc.expected_max_losing_streak_p95

    # Hech qanday DD kuzatilmadi (masalan hammasi yutuq) → har qanday risk xavfsiz.
    if worst_per_1pct <= _EPS:
        return RiskCalibrationResult(
            n_trades_total=n_total,
            n_trades_valid=n_valid,
            n_trades_dropped=dropped,
            avg_r_multiple=avg_r,
            worst_dd_per_1pct=worst_per_1pct,
            p95_dd_per_1pct=p95_per_1pct,
            expected_max_losing_streak_p95=streak,
            projected_worst_dd_pct=0.0,
            projected_p95_dd_pct=0.0,
            within_budget=True,
            dd_budget_pct=cfg.dd_budget_pct,
            current_risk_pct=cfg.current_risk_pct,
            recommended_risk_pct=cfg.risk_ceiling_pct,
            verdict="OK",
            notes=(
                "Monte-Carlo'da drawdown kuzatilmadi (worst-case DD ~ 0). "
                "Risk DD budjeti bilan cheklanmaydi."
            ),
        )

    projected_worst = worst_per_1pct * cfg.current_risk_pct
    projected_p95 = p95_per_1pct * cfg.current_risk_pct
    within_budget = projected_worst <= cfg.dd_budget_pct + _EPS

    # DD budjetiga mos eng katta risk (linear back-out), pol/shift bilan.
    # worst_per_1pct > _EPS va finite (R-multiplelar `_trade_r_multiple`'da
    # finite-filtrlangan) → raw_rec finite. Mudofaa: kelajakdagi refaktor
    # offline job'ni math.floor(NaN/inf) bilan buzmasligi uchun guard.
    raw_rec = cfg.dd_budget_pct / worst_per_1pct
    if not math.isfinite(raw_rec):
        raw_rec = 0.0   # konservativ: pastki chegaraga tushadi (max-risk EMAS)
    # Konservativ: 2 kasrgacha PASTGA yaxlitlaymiz, so'ng bounds.
    rec = math.floor(raw_rec * 100.0) / 100.0
    rec = _clamp(rec, cfg.risk_floor_pct, cfg.risk_ceiling_pct)

    if not within_budget:
        verdict = "REDUCE_RISK"
        notes = (
            f"Joriy {cfg.current_risk_pct:.2f}% risk -> eng yomon DD "
            f"~{projected_worst:.1f}% > budjet {cfg.dd_budget_pct:.1f}%. "
            f"Tavsiya: risk_per_trade'ni ~{rec:.2f}% ga tushiring."
        )
    elif rec >= cfg.current_risk_pct * cfg.headroom_factor and cfg.current_risk_pct < cfg.risk_ceiling_pct:
        verdict = "ROOM_TO_INCREASE"
        notes = (
            f"Joriy {cfg.current_risk_pct:.2f}% risk -> eng yomon DD "
            f"~{projected_worst:.1f}% < budjet {cfg.dd_budget_pct:.1f}%. "
            f"Budjet ~{rec:.2f}% gacha riskni ko'tarishga imkon beradi "
            "(ixtiyoriy, approval bilan)."
        )
    else:
        verdict = "OK"
        notes = (
            f"Joriy {cfg.current_risk_pct:.2f}% risk -> eng yomon DD "
            f"~{projected_worst:.1f}%, budjet {cfg.dd_budget_pct:.1f}% ichida. "
            "O'zgartirish shart emas."
        )

    return RiskCalibrationResult(
        n_trades_total=n_total,
        n_trades_valid=n_valid,
        n_trades_dropped=dropped,
        avg_r_multiple=avg_r,
        worst_dd_per_1pct=worst_per_1pct,
        p95_dd_per_1pct=p95_per_1pct,
        expected_max_losing_streak_p95=streak,
        projected_worst_dd_pct=projected_worst,
        projected_p95_dd_pct=projected_p95,
        within_budget=within_budget,
        dd_budget_pct=cfg.dd_budget_pct,
        current_risk_pct=cfg.current_risk_pct,
        recommended_risk_pct=rec,
        verdict=verdict,
        notes=notes,
    )
