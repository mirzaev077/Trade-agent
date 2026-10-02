#!/usr/bin/env bash
set -uo pipefail
cd "C:/Users/Game PC 2026/Desktop/TRD/ai-trade-agent/ai-trading-agent"
OUT=reports/parity_2026-10-02/wf
mkdir -p "$OUT"
PY="py -3.12"
ENG="apps.api.src.agents.trader.engine"
DATA="--symbol XAUUSD --data-path data/historical --primary-timeframe M15"

run () {  # $1=name  $2=start $3=end  rest=extra args
  local name="$1" start="$2" end="$3"; shift 3
  echo "===== $name START $(date -u +%H:%M:%S) ====="
  $PY -m $ENG --start "$start" --end "$end" $DATA "$@" \
      --report-path "$OUT/$name" > "$OUT/$name.console.txt" 2>&1
  echo "===== $name DONE rc=$? $(date -u +%H:%M:%S) ====="
}

# 1) In-sample: 2024 only, no blocks
run IS2024 2024-01-01 2024-12-31

# 2) Derive block list from 2024 breakdown: PF < 0.70 AND count >= 20
BLOCKS=$($PY - "$OUT/IS2024" <<'PY'
import json, glob, sys
f = glob.glob(sys.argv[1] + "/*.json")[0]
d = json.load(open(f, encoding="utf-8"))
bd = d["performance"]["setup_breakdown"]
sel = [s for s,v in bd.items()
       if v.get("count",0) >= 20 and v.get("profit_factor",0) < 0.70]
print(" ".join(f"--block-setup {s}" for s in sorted(sel)))
open(sys.argv[1] + ".blocklist.txt","w").write("\n".join(sorted(sel)))
PY
)
echo "DERIVED BLOCKS (from 2024): $BLOCKS"

# 3) Out-of-sample: 2025 only, baseline (no blocks)
run OOS2025_baseline 2025-01-01 2025-12-31

# 4) Out-of-sample: 2025 only, with 2024-derived blocks
run OOS2025_blocked  2025-01-01 2025-12-31 $BLOCKS

echo "ALL DONE $(date -u +%H:%M:%S)"
