import numpy as np

rng = np.random.default_rng(42)

N_PATHS = 10_000
DAYS = 252
TRADES_PER_DAY = 6
N_TRADES = DAYS * TRADES_PER_DAY  # 1512

WIN_R = 2.0
LOSS_R = -1.0
P_WIN_NORMAL = 0.40

RUIN_50 = 0.50   # equity < 50% of start
RUIN_10 = 0.10   # equity < 10% of start

FS = [0.01, 0.02, 0.03, 0.05]


def simulate(p_win_seq, f, n_paths=N_PATHS, n_trades=N_TRADES, seed=42):
    """
    Vectorized MC. p_win_seq: array of length n_trades giving win prob per trade
    (allows a bad-regime quarter). f: fraction of equity staked per trade.
    Returns dict of stats.
    Each trade multiplies equity by (1 + f*WIN_R) if win else (1 + f*LOSS_R).
    """
    r = np.random.default_rng(seed)
    p = np.asarray(p_win_seq, dtype=float)
    assert p.shape[0] == n_trades

    win_mult = 1.0 + f * WIN_R
    loss_mult = 1.0 + f * LOSS_R

    equity = np.ones(n_paths)              # start = 1.0 (x start)
    running_max = np.ones(n_paths)
    max_dd = np.zeros(n_paths)             # max drawdown fraction (0..1)
    ruined_50 = np.zeros(n_paths, dtype=bool)
    ruined_10 = np.zeros(n_paths, dtype=bool)

    for t in range(n_trades):
        wins = r.random(n_paths) < p[t]
        equity = equity * np.where(wins, win_mult, loss_mult)

        running_max = np.maximum(running_max, equity)
        dd = 1.0 - equity / running_max
        max_dd = np.maximum(max_dd, dd)

        # ruin = ever drops below threshold of START (=1.0)
        ruined_50 |= (equity < RUIN_50)
        ruined_10 |= (equity < RUIN_10)

    return {
        "f": f,
        "p_ruin_50": ruined_50.mean(),
        "p_ruin_10": ruined_10.mean(),
        "median_final": np.median(equity),
        "p5_final": np.percentile(equity, 5),
        "p95_final": np.percentile(equity, 95),
        "median_maxdd": np.median(max_dd),
        "mean_final": equity.mean(),
    }


def fmt_x(v):
    if v >= 1000:
        return f"{v:,.0f}x"
    if v >= 1:
        return f"{v:.3f}x"
    return f"{v:.4f}x"


print("=" * 88)
print("BASELINE: all trades p_win = 0.40  (edge = +0.118R/trade)")
print("=" * 88)
print(f"{'f':>5} | {'P(ruin<50%)':>11} | {'P(ruin<10%)':>11} | {'median':>10} | "
      f"{'5th pct':>10} | {'95th pct':>12} | {'med maxDD':>9}")
print("-" * 88)

p_normal = np.full(N_TRADES, P_WIN_NORMAL)
baseline = {}
for f in FS:
    s = simulate(p_normal, f, seed=100 + int(f * 1000))
    baseline[f] = s
    print(f"{f*100:>4.0f}% | {s['p_ruin_50']*100:>10.2f}% | {s['p_ruin_10']*100:>10.2f}% | "
          f"{fmt_x(s['median_final']):>10} | {fmt_x(s['p5_final']):>10} | "
          f"{fmt_x(s['p95_final']):>12} | {s['median_maxdd']*100:>7.1f}%")

# ---- Regime stress: one bad quarter (63 days * 6 = 378 trades) inserted ----
BAD_DAYS = 63
BAD_TRADES = BAD_DAYS * TRADES_PER_DAY  # 378
P_WIN_BAD = 0.28  # -> edge = 2*0.28 - 0.72 = -0.16R (about -0.13R as stated)

# place the bad quarter in the middle (Q2-like). Start index after ~1 quarter.
bad_start = TRADES_PER_DAY * 63  # begins at day 64 ~ Q2
p_stress = np.full(N_TRADES, P_WIN_NORMAL)
p_stress[bad_start:bad_start + BAD_TRADES] = P_WIN_BAD

edge_bad = WIN_R * P_WIN_BAD + LOSS_R * (1 - P_WIN_BAD)
print()
print("=" * 88)
print(f"REGIME STRESS: one bad quarter ({BAD_TRADES} trades, p_win={P_WIN_BAD}, "
      f"edge={edge_bad:+.3f}R) inserted at day 64")
print(f"Rest of year p_win = {P_WIN_NORMAL} (edge +0.118R). Total trades = {N_TRADES}")
print("=" * 88)
print(f"{'f':>5} | {'P(ruin<50%)':>11} | {'P(ruin<10%)':>11} | {'median':>10} | "
      f"{'5th pct':>10} | {'95th pct':>12} | {'med maxDD':>9}")
print("-" * 88)

for f in [0.02, 0.03]:
    s = simulate(p_stress, f, seed=500 + int(f * 1000))
    print(f"{f*100:>4.0f}% | {s['p_ruin_50']*100:>10.2f}% | {s['p_ruin_10']*100:>10.2f}% | "
          f"{fmt_x(s['median_final']):>10} | {fmt_x(s['p5_final']):>10} | "
          f"{fmt_x(s['p95_final']):>12} | {s['median_maxdd']*100:>7.1f}%")

# ---- Reference: what 2%/day target implies ----
print()
print("=" * 88)
print("TARGET CHECK: 2% per DAY")
print("=" * 88)
target_year = (1.02) ** 252
print(f"(1.02)^252 = {target_year:,.0f}x  =  {(target_year-1)*100:,.0f}% per year")
# per-trade growth needed if 6 trades/day, geometric
per_trade_needed = (1.02) ** (1 / 6) - 1
print(f"Per-trade geometric growth needed: {per_trade_needed*100:.4f}%/trade")
# our actual per-trade expected log-growth at various f (baseline edge)
print()
print("Expected per-trade arithmetic return E[mult]-1 and E[log mult] (baseline 40% WR):")
for f in FS:
    wm = 1 + f * WIN_R
    lm = 1 + f * LOSS_R
    e_arith = P_WIN_NORMAL * (wm - 1) + (1 - P_WIN_NORMAL) * (lm - 1)
    e_log = P_WIN_NORMAL * np.log(wm) + (1 - P_WIN_NORMAL) * np.log(lm)
    daily_log = e_log * TRADES_PER_DAY
    print(f"  f={f*100:>3.0f}%: E[arith]={e_arith*100:+.4f}%/trade  "
          f"E[log]={e_log*100:+.4f}%/trade  -> daily E[log]={daily_log*100:+.4f}%/day "
          f"(={np.expm1(daily_log)*100:+.4f}%/day geometric)")
