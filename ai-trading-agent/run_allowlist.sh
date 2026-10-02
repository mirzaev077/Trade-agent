#!/usr/bin/env bash
set -uo pipefail
cd "C:/Users/Game PC 2026/Desktop/TRD/ai-trade-agent/ai-trading-agent"
OUT=reports/parity_2026-10-02/wf
PY="py -3.12"
ENG="apps.api.src.agents.trader.engine"
DATA="--symbol XAUUSD --data-path data/historical --primary-timeframe M15"
BLOCK=$(cat "$OUT/allowlist_block_args.txt")

run () {
  local name="$1" start="$2" end="$3"; shift 3
  echo "===== $name START $(date -u +%H:%M:%S) ====="
  $PY -m $ENG --start "$start" --end "$end" $DATA "$@" \
      --report-path "$OUT/$name" > "$OUT/$name.console.txt" 2>&1
  echo "===== $name DONE rc=$? $(date -u +%H:%M:%S) ====="
}

# In-sample check: allowlist on 2024 (the year it was fit on)
run IS2024_allow 2024-01-01 2024-12-31 $BLOCK
# Out-of-sample: same allowlist on 2025 (unseen) — the real test
run OOS2025_allow 2025-01-01 2025-12-31 $BLOCK

echo "ALL DONE $(date -u +%H:%M:%S)"
