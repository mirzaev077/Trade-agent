"""
HistoricalDataManager — Tarixiy ma'lumotlarni boshqaradi.

Kirish formatlari:
- MT5 CSV export (timeframe per file)
- Parquet files (recommended — 10x kichik, 5x tez)
- Tick data (CSV yoki Arctic / TimescaleDB)

Multi-timeframe alignment:
- M1 -> M5 -> M15 -> M30 -> H1 -> H4 -> D1
- Har bir timeframe alohida saqlanadi, lekin sinxronlashtirilgan
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd


# ── Supported timeframes ────────────────────────────────────────────────
TIMEFRAME_SECONDS: dict[str, int] = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}


class HistoricalDataManager:
    """
    Tarixiy ma'lumotlarni boshqaradi.

    Kirish formatlari:
    - MT5 CSV export (timeframe per file)
    - Parquet files (recommended — 10x kichik, 5x tez)
    - Tick data (CSV yoki Arctic / TimescaleDB)

    Multi-timeframe alignment:
    - M1 -> M5 -> M15 -> M30 -> H1 -> H4 -> D1
    - Har bir timeframe alohida saqlanadi, lekin sinxronlashtirilgan
    """

    def __init__(self, data_path: Path) -> None:
        self.data_path = Path(data_path)
        self._cache: dict[tuple[str, str], pd.DataFrame] = {}  # (symbol, tf) -> DataFrame
        self._tick_cache: dict[str, pd.DataFrame] = {}  # tick data — agar mavjud bo'lsa

    # ── Public API ──────────────────────────────────────────────────────

    def load(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        """
        OHLCV ma'lumot qaytaradi.

        Columns: timestamp, open, high, low, close, tick_volume,
                 spread (agar mavjud bo'lsa)

        Parquet va CSV formatlarini qo'llab-quvvatlaydi.
        Yuklangan ma'lumotlar keshlanadi — qayta o'qish kerak emas.
        """
        key = (symbol, timeframe)
        if key not in self._cache:
            self._load_to_cache(symbol, timeframe)

        df = self._cache[key]
        return df.loc[start:end].copy()

    def get_candles_at(
        self,
        symbol: str,
        timeframe: str,
        timestamp: datetime,
        count: int = 500,
    ) -> pd.DataFrame:
        """
        Berilgan vaqtda mavjud bo'lgan oxirgi `count` ta candle.

        MUHIM: Faqat YOPILGAN candle'lar qaytariladi.
        Joriy (hali yopilmagan) candle ko'rinmaydi — look-ahead bias'ning oldini olish.
        """
        tf_seconds = self._timeframe_to_seconds(timeframe)

        # Joriy candle hali yopilmagan — uni chiqarib tashlaymiz.
        # _floor_time(timestamp) — joriy (hali yopilmagan) candle boshlanishi.
        # Biz undan BIR PERIOD oldingi candle boshlanishini olishimiz kerak.
        current_candle_open = self._floor_time(timestamp, tf_seconds)
        last_closed = current_candle_open - timedelta(seconds=tf_seconds)

        key = (symbol, timeframe)
        if key not in self._cache:
            raise KeyError(
                f"Ma'lumot topilmadi: ({symbol}, {timeframe}). "
                f"Avval load() chaqiring."
            )

        df = self._cache[key]
        return df.loc[:last_closed].tail(count).copy()

    def get_tick(self, symbol: str, timestamp: datetime) -> dict:
        """
        Tick data — agar mavjud bo'lsa.
        Yo'q bo'lsa — eng yaqin M1 candle'dan tahmin.
        """
        if symbol in self._tick_cache:
            ticks = self._tick_cache[symbol]
            idx = ticks.index.searchsorted(timestamp, side="right") - 1
            if idx >= 0:
                return ticks.iloc[idx].to_dict()

        # Fallback: M1 candle close + tahminiy spread
        m1 = self.get_candles_at(symbol, "M1", timestamp, 1)
        if m1.empty:
            return {
                "time": timestamp,
                "bid": 0.0,
                "ask": 0.0,
                "last": 0.0,
                "volume": 0,
            }

        close_price = m1.iloc[-1]["close"]
        return {
            "time": timestamp,
            "bid": close_price,
            "ask": close_price + self._estimate_spread(symbol, timestamp),
            "last": close_price,
            "volume": m1.iloc[-1]["tick_volume"],
        }

    def detect_gaps(self, symbol: str, timeframe: str) -> list[dict]:
        """
        Ma'lumotlardagi gap'larni topish — weekend, bayram, broker downtime.
        Gap > 1 candle period bo'lsa — backtest natijalariga ta'sir qilishi mumkin.

        Gap aniqlash qoidasi: agar actual_delta > expected_delta * 1.5 bo'lsa — gap.
        """
        key = (symbol, timeframe)
        if key not in self._cache:
            raise KeyError(
                f"Ma'lumot topilmadi: ({symbol}, {timeframe}). "
                f"Avval load() chaqiring."
            )

        df = self._cache[key]
        expected_delta = pd.Timedelta(seconds=self._timeframe_to_seconds(timeframe))
        gaps: list[dict] = []

        for i in range(1, len(df)):
            actual_delta = df.index[i] - df.index[i - 1]
            if actual_delta > expected_delta * 1.5:
                gaps.append(
                    {
                        "from": df.index[i - 1],
                        "to": df.index[i],
                        "duration": actual_delta,
                        "is_weekend": self._is_weekend_gap(
                            df.index[i - 1], df.index[i]
                        ),
                    }
                )
        return gaps

    # ── Helper / private methods ────────────────────────────────────────

    @staticmethod
    def _timeframe_to_seconds(timeframe: str) -> int:
        """Timeframe nomini sekundlarga aylantiradi. M1=60, ..., D1=86400."""
        if timeframe not in TIMEFRAME_SECONDS:
            raise ValueError(
                f"Noto'g'ri timeframe: {timeframe!r}. "
                f"Qo'llab-quvvatlanadigan: {list(TIMEFRAME_SECONDS.keys())}"
            )
        return TIMEFRAME_SECONDS[timeframe]

    @staticmethod
    def _floor_time(dt: datetime, period_seconds: int) -> datetime:
        """
        Vaqtni pastga yaxlitlaydi (floor) berilgan period bo'yicha.
        Masalan: 10:37 — M15 uchun -> 10:30.
        """
        ts = int(dt.timestamp())
        floored_ts = ts - (ts % period_seconds)
        return datetime.fromtimestamp(floored_ts, tz=dt.tzinfo)

    @staticmethod
    def _estimate_spread(symbol: str, timestamp: datetime) -> float:
        """
        Tahminiy spread qaytaradi (pips da).
        Haqiqiy spread ma'lumoti mavjud bo'lmasa — standart qiymatlar.
        """
        # Standart spread qiymatlari (major juftliklar uchun)
        default_spreads: dict[str, float] = {
            "EURUSD": 0.00012,
            "GBPUSD": 0.00015,
            "USDJPY": 0.012,
            "USDCHF": 0.00015,
            "AUDUSD": 0.00015,
            "NZDUSD": 0.00018,
            "USDCAD": 0.00018,
            "XAUUSD": 0.20,  # ~0.20 USD spread for gold (broker-dependent, demo Exness ~0.2-0.4)
        }

        # Session-based spread multiplier (Asian session — kengaytirilgan spread)
        hour = timestamp.hour if hasattr(timestamp, "hour") else 12
        if 0 <= hour < 8:
            # Asian session — odatda spread kengayadi
            multiplier = 1.5
        elif 13 <= hour < 17:
            # London/NY overlap — eng tor spread
            multiplier = 0.8
        else:
            multiplier = 1.0

        base_spread = default_spreads.get(symbol, 0.00020)
        return base_spread * multiplier

    @staticmethod
    def _is_weekend_gap(start: datetime, end: datetime) -> bool:
        """
        Gap weekend (shanba-yakshanba) sababli ekanligini tekshiradi.
        Forex bozori juma 22:00 UTC dan yakshanba 22:00 UTC gacha yopiq.
        """
        # Agar gap orasida shanba yoki yakshanba bo'lsa — weekend gap
        current = start
        while current <= end:
            if current.weekday() in (5, 6):  # Saturday=5, Sunday=6
                return True
            current += timedelta(days=1)
        return False

    def _load_to_cache(self, symbol: str, timeframe: str) -> None:
        """
        Ma'lumotni diskdan o'qib keshga yuklaydi.
        Avval parquet, keyin CSV qidiradi.
        """
        parquet_path = self.data_path / f"{symbol}_{timeframe}.parquet"
        csv_path = self.data_path / f"{symbol}_{timeframe}.csv"

        if parquet_path.exists():
            df = pd.read_parquet(parquet_path)
        elif csv_path.exists():
            df = pd.read_csv(csv_path)
        else:
            raise FileNotFoundError(
                f"Ma'lumot fayli topilmadi: {parquet_path} yoki {csv_path}"
            )

        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.set_index("timestamp").sort_index()

        self._cache[(symbol, timeframe)] = df

    def preload(
        self,
        symbol: str,
        timeframes: list[str] | None = None,
    ) -> None:
        """
        Bir nechta timeframe'larni oldindan keshga yuklash.
        Warmup data uchun foydali — BacktestEngine boshlashda chaqiradi.
        """
        if timeframes is None:
            timeframes = list(TIMEFRAME_SECONDS.keys())
        for tf in timeframes:
            try:
                self._load_to_cache(symbol, tf)
            except FileNotFoundError:
                pass  # Mavjud bo'lmagan timeframe'larni o'tkazib yuboramiz

    def cache_info(self) -> dict[str, int]:
        """Kesh holati haqida ma'lumot — debug uchun."""
        return {
            "cached_dataframes": len(self._cache),
            "cached_tick_symbols": len(self._tick_cache),
            "total_rows": sum(len(df) for df in self._cache.values()),
        }
