"""
F1-1 acceptance: 50-trade end-to-end self-learner approval-gate scenario.

Tasks.md F1-1 oxirgi qatorida ko'rsatilgan:
    "Test: 50 trade'dan keyin learned.json o'zgarmasligini, lekin
     pending_adjustments ga yozilishini tekshirish."

Bu test 50 trade'ni controlled distribution bilan SelfLearner ga yuboradi va
ikki rejimini taqqoslaydi:

  • AUTO_APPLY_LEARNING=false (default, prod gate ON):
      - learned.json['adapted/*'] CHANGE EMAS
      - pending_adjustments DB'da >=1 row
      - notify_pending_adjustment_sync chaqirilgan

  • AUTO_APPLY_LEARNING=true (legacy direct-mutate):
      - learned.json['adapted/*'] CHANGED
      - active_adjustments DB'da audit rows

Va bonus: gate rejimida admin_cli approve flow learned.json'ni yangilaydi.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apps.api.src.agents.trader.brain import self_learner as sl_mod
from apps.api.src.agents.trader.brain.self_learner import SelfLearner
from apps.api.src.agents.trader.state import db


# ── Trade distribution helper ─────────────────────────────────────────────────

# 50 ta trade — 4 ta setup turida, evolve()'da clear worst/best 30% chiqishi uchun:
#   OB:  12 trade, 4W/8L  →  33% WR  → worst 20% (n//5=0, max(1,0)=1) → PRUNE
#   FVG: 13 trade, 5W/8L  →  38% WR  → middle
#   BB:  13 trade, 8W/5L  →  61% WR  → top 30% → BOOST
#   RB:  12 trade, 9W/3L  →  75% WR  → top 30% → BOOST
#
# Hammasi (sl_pips, tp_pips) = (10, 20) — bir xil, sl_multiplier mutatsiyasini
# trigger qilmaslik uchun (avg_sl_loss == avg_sl_win → analyze SL skip).

def _trades_distribution() -> list[dict]:
    out: list[dict] = []
    for typ, wins, losses in [
        ("OB", 4, 8),
        ("FVG", 5, 8),
        ("BB", 8, 5),
        ("RB", 9, 3),
    ]:
        for _ in range(wins):
            out.append({"entry_type": typ, "result": "win", "pnl": 20.0})
        for _ in range(losses):
            out.append({"entry_type": typ, "result": "loss", "pnl": -10.0})
    assert len(out) == 50
    # Interleave deterministic — analyze() at 20/40 ham aralash sample ko'rsin
    rng_idx = sorted(range(50), key=lambda i: (i * 7) % 50)
    return [out[i] for i in rng_idx]


def _make_learner(tmp_path: Path) -> SelfLearner:
    """SelfLearner instance — learned.json + lessons.json tmp path'da."""
    learned = tmp_path / "learned.json"
    lessons = tmp_path / "lessons.json"  # mavjud emas — bo'sh disabled set
    return SelfLearner(path=str(learned), lessons_path=str(lessons))


def _run_50_trades(learner: SelfLearner) -> None:
    for t in _trades_distribution():
        learner.log_trade(
            entry_type=t["entry_type"],
            timeframe="H1",
            session="london",
            direction="buy",
            sl_pips=10.0,
            tp_pips=20.0,
            result=t["result"],
            pnl=t["pnl"],
        )


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_gate_mode_does_not_mutate_learned_json(
    tmp_path, tmp_state_dir, monkeypatch
):
    """AUTO_APPLY_LEARNING=false → learned.json['adapted/*'] o'zgarmaydi."""
    monkeypatch.delenv("AUTO_APPLY_LEARNING", raising=False)  # gate ON

    notify_calls: list[tuple] = []
    monkeypatch.setattr(
        sl_mod._tg,
        "notify_pending_adjustment_sync",
        lambda *a, **kw: notify_calls.append((a, kw)),
    )

    learner = _make_learner(tmp_path)

    # Baseline: defaults snapshot
    default_weights = dict(learner.data["adapted"]["setup_weights"])
    default_disabled = list(learner.data["adapted"]["disabled"])
    default_entry_pct = learner.data["adapted"]["entry_pct"]
    default_sl_mult = learner.data["adapted"]["sl_multiplier"]

    _run_50_trades(learner)

    # 1) Diskdagi learned.json adapted/* — defaults bilan bir xil
    with open(learner.path, "r", encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk["adapted"]["setup_weights"] == default_weights, (
        "Gate ON bo'lganda setup_weights diskka yozilmasligi kerak"
    )
    assert on_disk["adapted"]["disabled"] == default_disabled
    assert on_disk["adapted"]["entry_pct"] == default_entry_pct
    assert on_disk["adapted"]["sl_multiplier"] == default_sl_mult

    # 2) trades + stats diskka yozildi (50)
    assert len(on_disk["trades"]) == 50
    assert on_disk["stats"]["total"] == 50

    # 3) pending_adjustments DB'da rows bor — propose ishladi
    pendings = db.list_pending(include_expired=True)
    assert len(pendings) >= 1, (
        f"Kamida 1 ta pending bo'lishi kerak. Olindi: {pendings}"
    )

    # 4) Eng kamida bitta `disabled.OB` (PRUNE) yoki setup_weights.RB (BOOST) ko'rinishi kerak
    params = {p["param"] for p in pendings}
    assert any("OB" in p or "RB" in p or "BB" in p for p in params), (
        f"Kutilgan propose params yo'q. Topildi: {params}"
    )

    # 5) Telegram notify chaqirildi (pending row sonidan kam emas)
    assert len(notify_calls) == len(pendings), (
        f"Notify chaqiruvlar soni pending'lar soni bilan mos kelmadi: "
        f"notify={len(notify_calls)}, pendings={len(pendings)}"
    )

    # 6) active_adjustments BO'SH — hech qaysi propose auto-approve bo'lmagan
    active = db.list_active()
    assert active == [], f"Gate ON bo'lganda active bo'sh bo'lishi kerak: {active}"


def test_auto_apply_mode_mutates_learned_json(
    tmp_path, tmp_state_dir, monkeypatch
):
    """AUTO_APPLY_LEARNING=true → learned.json['adapted/*'] o'zgaradi va audit yoziladi."""
    monkeypatch.setenv("AUTO_APPLY_LEARNING", "true")

    # Bu rejimda notify chaqirilmaydi
    notify_calls: list[tuple] = []
    monkeypatch.setattr(
        sl_mod._tg,
        "notify_pending_adjustment_sync",
        lambda *a, **kw: notify_calls.append((a, kw)),
    )

    learner = _make_learner(tmp_path)
    default_ob_weight = learner.data["adapted"]["setup_weights"]["OB"]

    _run_50_trades(learner)

    # 1) Diskdagi learned.json adapted — MODIFIED
    with open(learner.path, "r", encoding="utf-8") as f:
        on_disk = json.load(f)

    # OB worst → disabled list'ga qo'shilgan bo'lishi kerak (evolve worst 20% prune)
    assert "OB" in on_disk["adapted"]["disabled"], (
        f"AUTO_APPLY rejimida OB disabled bo'lishi kerak edi. "
        f"disabled: {on_disk['adapted']['disabled']}"
    )

    # BB yoki RB weight — oshirilgan bo'lishi kerak (evolve best 30% boost)
    bb_w = on_disk["adapted"]["setup_weights"].get("BB", 1.0)
    rb_w = on_disk["adapted"]["setup_weights"].get("RB", 1.0)
    assert bb_w > 1.0 or rb_w > 1.0, (
        f"BB yoki RB weight oshirilishi kerak edi. BB={bb_w}, RB={rb_w}"
    )

    # 2) AUTO_APPLY rejimida har propose darhol approve qilinadi —
    # pending_adjustments status='approved' bo'ladi, list_pending bo'sh
    # bo'lishi mumkin. active_adjustments'da audit ko'rinishi kerak.
    active = db.list_active()
    assert len(active) >= 1, (
        f"AUTO_APPLY rejimida active rows bo'lishi kerak: {active}"
    )

    # OB DISABLE + BB/RB BOOST kamida 3 ta audit row hosil qiladi
    active_params = {a["param"] for a in active}
    assert any("OB" in p for p in active_params), (
        f"OB uchun audit row kerak. Topildi: {active_params}"
    )

    # 3) Telegram notify CHAQIRILMAGAN — AUTO_APPLY rejimida pending alert yo'q
    assert notify_calls == [], (
        f"AUTO_APPLY rejimida notify chaqirilmasligi kerak. "
        f"Olindi: {notify_calls}"
    )


def test_admin_approve_applies_to_learned_json(
    tmp_path, tmp_state_dir, monkeypatch
):
    """Gate rejimida propose → admin_cli approve → learned.json yangilanadi."""
    monkeypatch.delenv("AUTO_APPLY_LEARNING", raising=False)
    monkeypatch.setattr(
        sl_mod._tg, "notify_pending_adjustment_sync", lambda *a, **kw: None
    )

    learner = _make_learner(tmp_path)
    _run_50_trades(learner)

    # admin_cli `_LEARNED_JSON_PATH` import-vaqtida o'rnatiladi — monkeypatch bilan
    # to'g'ridan-to'g'ri uchqun yo'lini almashtirib qo'yamiz
    from apps.api.src.tools import admin_cli
    monkeypatch.setattr(admin_cli, "_LEARNED_JSON_PATH", Path(learner.path))

    pendings = db.list_pending(include_expired=True)
    assert pendings, "Pending row bo'lishi kerak"

    # setup_weights.* turidagi propose'ni tanlaymiz (OB disable emas, balki BB/RB boost)
    target = next(
        (p for p in pendings if p["param"].startswith("setup_weights.")),
        None,
    )
    if target is None:
        pytest.skip("setup_weights propose topilmadi — test trade distribution ni qayta ko'ring")

    # admin_cli approve subcommand'ini chaqirish (subprocess emas, import bilan)
    exit_code = admin_cli.main(["approve", target["id"], "--by", "test-suite"])
    assert exit_code == 0, f"admin_cli approve fail bo'ldi (exit={exit_code})"

    # 1) Pending status = approved
    updated = db.get_pending(target["id"])
    assert updated is not None
    assert updated["status"] == "approved"

    # 2) active_adjustments'da yangi row
    active = db.list_active(param=target["param"])
    assert len(active) == 1
    assert active[0]["value"] == target["new_value"]

    # 3) learned.json adapted YANGILANGAN — setup_weights.<TYP>
    typ = target["param"].split(".", 1)[1]
    with open(learner.path, "r", encoding="utf-8") as f:
        on_disk = json.load(f)
    new_w_on_disk = on_disk["adapted"]["setup_weights"].get(typ)
    assert new_w_on_disk == target["new_value"], (
        f"learned.json[adapted.setup_weights.{typ}] approve'dan keyin "
        f"yangilanishi kerak edi. Olindi: {new_w_on_disk}, "
        f"kutilgan: {target['new_value']}"
    )
