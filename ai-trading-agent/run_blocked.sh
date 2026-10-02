#!/usr/bin/env bash
set -uo pipefail
cd "C:/Users/Game PC 2026/Desktop/TRD/ai-trade-agent/ai-trading-agent"
OUT=reports/parity_2026-10-02
PY="py -3.12"
COMMON="--start 2024-01-01 --end 2025-12-31 --symbol XAUUSD --data-path data/historical --primary-timeframe M15"

BLOCKS="--block-setup H1_DR_Eq --block-setup M15_RB --block-setup M15_SSL_sweep \
--block-setup M15_BULL --block-setup M15_BEAR --block-setup H1_BSL_sweep \
--block-setup M15_BSL_sweep --block-setup H1_SSL_sweep --block-setup H1_BULL \
--block-setup H4_DR_Eq --block-setup M15_LiqVoid --block-setup H4_BOS_R \
--block-setup H1_LiqVoid"

echo "===== RUN D_blocked START $(date -u +%H:%M:%S) ====="
$PY -m apps.api.src.agents.trader.engine $COMMON $BLOCKS \
    --report-path "$OUT/D_blocked" > "$OUT/D_blocked.console.txt" 2>&1
echo "===== RUN D_blocked DONE rc=$? $(date -u +%H:%M:%S) ====="
echo "ALL DONE $(date -u +%H:%M:%S)"
