#!/usr/bin/env bash
set -uo pipefail
cd "C:/Users/Game PC 2026/Desktop/TRD/ai-trade-agent/ai-trading-agent"
OUT=reports/parity_2026-10-02
mkdir -p "$OUT"
PY="py -3.12"
COMMON="--start 2024-01-01 --end 2025-12-31 --symbol XAUUSD --data-path data/historical --primary-timeframe M15"

run () {
  local name="$1"; shift
  echo "===== RUN $name START $(date -u +%H:%M:%S) ====="
  $PY -m apps.api.src.agents.trader.engine $COMMON "$@" \
      --report-path "$OUT/$name" > "$OUT/$name.console.txt" 2>&1
  echo "===== RUN $name DONE rc=$? $(date -u +%H:%M:%S) ====="
}

run A_baseline
run B_chop        --chop-gate
run C_chop_rdays  --chop-gate --reduced-days 0,4

echo "ALL DONE $(date -u +%H:%M:%S)"
