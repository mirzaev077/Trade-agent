"""Jonli kuzatuv — brain/trades.csv ni o'qib, hozirgi natijani qisqa ko'rsatadi.

Ishlatish:
    python scripts/live_monitor.py            # to'liq xulosa + oxirgi 8 savdo
    python scripts/live_monitor.py --since 2026-06-18   # faqat shu sanadan keyin

Faqat HAQIQIY savdolar hisoblanadi (pnl != 0 yoki exit qo'yilgan).
Replay/breakeven (pnl=0, exit=0) qatorlar alohida ko'rsatiladi.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

CSV = Path(__file__).resolve().parent.parent / "apps/api/src/agents/trader/brain/trades.csv"


def load(since: str | None):
    real, replay = [], []
    if not CSV.exists():
        return real, replay
    with CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if since and row.get("timestamp", "") < since:
                continue
            try:
                pnl = float(row.get("pnl") or 0)
            except ValueError:
                pnl = 0.0
            row["_pnl"] = pnl
            exit_ = row.get("exit") or "0"
            if pnl != 0 or (exit_ not in ("0", "0.0", "")):
                real.append(row)
            else:
                replay.append(row)
    return real, replay


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="YYYY-MM-DD dan keyingilar")
    ap.add_argument("--last", type=int, default=8, help="oxirgi nechta savdo")
    args = ap.parse_args()

    real, replay = load(args.since)
    wins = [r for r in real if r["_pnl"] > 0]
    losses = [r for r in real if r["_pnl"] < 0]
    gross_w = sum(r["_pnl"] for r in wins)
    gross_l = sum(r["_pnl"] for r in losses)
    net = gross_w + gross_l
    n = len(real)
    wr = len(wins) / n * 100 if n else 0.0
    pf = (gross_w / abs(gross_l)) if gross_l else float("inf")

    print("=" * 56)
    print(f"  JONLI KUZATUV - {CSV.name}" + (f"  (since {args.since})" if args.since else ""))
    print("=" * 56)
    print(f"  Haqiqiy savdolar : {n}   (replay/be: {len(replay)})")
    print(f"  Yutuq / Zarar    : {len(wins)} / {len(losses)}   (WR {wr:.1f}%)")
    print(f"  Jami yutuq       : +${gross_w:.2f}")
    print(f"  Jami zarar       : ${gross_l:.2f}")
    print(f"  SOF NATIJA       : {'+' if net >= 0 else ''}${net:.2f}   (PF {pf:.2f})")
    if n < 100:
        print(f"  [!] Namuna kichik ({n}/100) - bitta savdo natijani buzishi mumkin.")
    print("-" * 56)
    print(f"  Oxirgi {min(args.last, n)} savdo:")
    for r in real[-args.last:]:
        tag = "WIN " if r["_pnl"] > 0 else "LOSS"
        print(f"   {r['timestamp']}  {tag}  {r.get('label',''):12} {r['_pnl']:+8.2f}")
    print("=" * 56)


if __name__ == "__main__":
    main()
