"""Download MT5 historical bars (XAUUSD M15+H1+H4) for backtest.

Usage:
    python scripts/download_mt5_history.py
    python scripts/download_mt5_history.py --start 2024-01-01 --end 2025-12-31
    python scripts/download_mt5_history.py --symbol EURUSD --timeframes M15,H1
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

try:
    import MetaTrader5 as mt5
except ImportError:
    print("ERROR: MetaTrader5 package not installed. Run: pip install MetaTrader5", file=sys.stderr)
    sys.exit(1)


TF_MAP = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}


def init_mt5() -> None:
    load_dotenv()
    login = os.getenv("MT5_LOGIN")
    password = os.getenv("MT5_PASSWORD")
    server = os.getenv("MT5_SERVER")

    if not (login and password and server):
        raise RuntimeError("MT5_LOGIN / MT5_PASSWORD / MT5_SERVER missing in .env")

    if not mt5.initialize(login=int(login), password=password, server=server):
        err = mt5.last_error()
        raise RuntimeError(f"mt5.initialize failed: {err}")

    info = mt5.account_info()
    if info is None:
        raise RuntimeError(f"mt5.account_info() returned None: {mt5.last_error()}")
    print(f"[mt5] connected: {info.login}@{info.server} balance={info.balance} {info.currency}")


def fetch_range(symbol: str, tf_name: str, start: datetime, end: datetime) -> pd.DataFrame:
    tf = TF_MAP[tf_name]
    rates = mt5.copy_rates_range(symbol, tf, start, end)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"copy_rates_range({symbol}, {tf_name}) returned 0 bars: {mt5.last_error()}")

    df = pd.DataFrame(rates)
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    out = df[["timestamp", "open", "high", "low", "close", "tick_volume", "spread"]].copy()
    out["spread"] = out["spread"].astype(float) / 100.0
    return out


def save(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".parquet":
        df.to_parquet(path, index=False)
    else:
        df.to_csv(path, index=False)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="XAUUSD", help="Filename label (e.g. XAUUSD)")
    p.add_argument("--mt5-symbol", default=None, help="MT5 symbol name (e.g. XAUUSDm on Exness)")
    p.add_argument("--start", default="2024-01-01")
    p.add_argument("--end", default="2025-12-31")
    p.add_argument("--timeframes", default="M15,H1,H4")
    p.add_argument("--out", default="data/historical")
    p.add_argument("--format", choices=["parquet", "csv"], default="parquet")
    args = p.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    tfs = [t.strip().upper() for t in args.timeframes.split(",") if t.strip()]

    for tf in tfs:
        if tf not in TF_MAP:
            print(f"ERROR: unknown timeframe {tf!r}. Valid: {list(TF_MAP)}", file=sys.stderr)
            return 2

    out_dir = Path(args.out)
    mt5_sym = args.mt5_symbol or args.symbol
    print(f"[fetch] symbol={args.symbol} (mt5={mt5_sym}) range={args.start}..{args.end} tfs={tfs} out={out_dir}")

    init_mt5()
    try:
        if not mt5.symbol_select(mt5_sym, True):
            raise RuntimeError(f"symbol_select({mt5_sym}) failed: {mt5.last_error()}")

        for tf in tfs:
            df = fetch_range(mt5_sym, tf, start, end)
            path = out_dir / f"{args.symbol}_{tf}.{args.format}"
            save(df, path)
            print(
                f"[ok] {tf:>3}: {len(df):>6} bars  "
                f"{df['timestamp'].iloc[0]} .. {df['timestamp'].iloc[-1]}  -> {path}"
            )
    finally:
        mt5.shutdown()

    print("[done]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
