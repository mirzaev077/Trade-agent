"""
Unit tests for HistoricalDataManager (Component 2).

Sintetik DataFrame'lar bilan test qilinadi — haqiqiy data fayllari kerak emas.
tmp_path pytest fixture yordamida vaqtinchalik parquet/CSV fayllar yaratiladi.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from apps.api.src.agents.trader.core.data import TIMEFRAME_SECONDS, HistoricalDataManager


# ── Fixtures ────────────────────────────────────────────────────────────


def _make_ohlcv_df(
    start: datetime,
    periods: int,
    freq_seconds: int,
    base_price: float = 1.1000,
    with_spread: bool = False,
) -> pd.DataFrame:
    """
    Sintetik OHLCV DataFrame yaratadi — test uchun.
    Har bir candle uchun kichik tasodifiy narx o'zgarishi qo'shiladi.
    """
    rng = np.random.default_rng(seed=42)
    timestamps = [start + timedelta(seconds=freq_seconds * i) for i in range(periods)]
    data: dict[str, list] = {
        "timestamp": timestamps,
        "open": [],
        "high": [],
        "low": [],
        "close": [],
        "tick_volume": [],
    }
    if with_spread:
        data["spread"] = []

    price = base_price
    for _ in range(periods):
        change = rng.normal(0, 0.0005)
        o = price
        c = price + change
        h = max(o, c) + abs(rng.normal(0, 0.0003))
        l = min(o, c) - abs(rng.normal(0, 0.0003))  # noqa: E741
        data["open"].append(round(o, 5))
        data["high"].append(round(h, 5))
        data["low"].append(round(l, 5))
        data["close"].append(round(c, 5))
        data["tick_volume"].append(int(rng.integers(50, 500)))
        if with_spread:
            data["spread"].append(round(rng.uniform(0.00008, 0.00020), 5))
        price = c

    return pd.DataFrame(data)


def _save_parquet(df: pd.DataFrame, path: Path) -> None:
    """DataFrame'ni parquet formatda saqlaydi."""
    df.to_parquet(path, index=False)


def _save_csv(df: pd.DataFrame, path: Path) -> None:
    """DataFrame'ni CSV formatda saqlaydi."""
    df.to_csv(path, index=False)


@pytest.fixture
def sample_start() -> datetime:
    """Test uchun boshlang'ich vaqt."""
    return datetime(2023, 6, 1, 0, 0, 0)


@pytest.fixture
def m1_df(sample_start: datetime) -> pd.DataFrame:
    """M1 timeframe — 1000 ta candle (16+ soat ma'lumot)."""
    return _make_ohlcv_df(sample_start, periods=1000, freq_seconds=60)


@pytest.fixture
def h1_df(sample_start: datetime) -> pd.DataFrame:
    """H1 timeframe — 200 ta candle (8+ kun ma'lumot)."""
    return _make_ohlcv_df(sample_start, periods=200, freq_seconds=3600)


@pytest.fixture
def h1_df_with_spread(sample_start: datetime) -> pd.DataFrame:
    """H1 timeframe spread ustuni bilan."""
    return _make_ohlcv_df(
        sample_start, periods=200, freq_seconds=3600, with_spread=True
    )


@pytest.fixture
def data_dir_parquet(
    tmp_path: Path,
    m1_df: pd.DataFrame,
    h1_df: pd.DataFrame,
) -> Path:
    """Parquet fayllari bilan vaqtinchalik papka."""
    _save_parquet(m1_df, tmp_path / "EURUSD_M1.parquet")
    _save_parquet(h1_df, tmp_path / "EURUSD_H1.parquet")
    return tmp_path


@pytest.fixture
def data_dir_csv(
    tmp_path: Path,
    h1_df: pd.DataFrame,
) -> Path:
    """CSV fayli bilan vaqtinchalik papka."""
    csv_dir = tmp_path / "csv_data"
    csv_dir.mkdir()
    _save_csv(h1_df, csv_dir / "EURUSD_H1.csv")
    return csv_dir


@pytest.fixture
def data_dir_with_spread(
    tmp_path: Path,
    h1_df_with_spread: pd.DataFrame,
) -> Path:
    """Spread ustuni mavjud parquet fayl."""
    spread_dir = tmp_path / "spread_data"
    spread_dir.mkdir()
    _save_parquet(h1_df_with_spread, spread_dir / "EURUSD_H1.parquet")
    return spread_dir


@pytest.fixture
def manager(data_dir_parquet: Path) -> HistoricalDataManager:
    """Tayyor HistoricalDataManager — parquet ma'lumot bilan."""
    return HistoricalDataManager(data_dir_parquet)


# ── Test: Initialization ────────────────────────────────────────────────


class TestInit:
    """HistoricalDataManager initialization testlari."""

    def test_init_sets_data_path(self, tmp_path: Path) -> None:
        """data_path to'g'ri saqlanishi."""
        mgr = HistoricalDataManager(tmp_path)
        assert mgr.data_path == tmp_path

    def test_init_empty_caches(self, tmp_path: Path) -> None:
        """Boshlang'ich keshlar bo'sh bo'lishi."""
        mgr = HistoricalDataManager(tmp_path)
        assert mgr._cache == {}
        assert mgr._tick_cache == {}

    def test_init_accepts_string_path(self, tmp_path: Path) -> None:
        """String path ham qabul qilishi kerak."""
        mgr = HistoricalDataManager(str(tmp_path))
        assert isinstance(mgr.data_path, Path)


# ── Test: load() ────────────────────────────────────────────────────────


class TestLoad:
    """load() metodi testlari — OHLCV ma'lumot yuklash."""

    def test_load_parquet_returns_dataframe(
        self,
        manager: HistoricalDataManager,
        sample_start: datetime,
    ) -> None:
        """Parquet fayldan DataFrame qaytishi."""
        end = sample_start + timedelta(hours=5)
        df = manager.load("EURUSD", "H1", sample_start, end)
        assert isinstance(df, pd.DataFrame)
        assert not df.empty

    def test_load_csv_fallback(
        self,
        data_dir_csv: Path,
        sample_start: datetime,
    ) -> None:
        """Parquet yo'q bo'lsa CSV'dan yuklashi."""
        mgr = HistoricalDataManager(data_dir_csv)
        end = sample_start + timedelta(hours=5)
        df = mgr.load("EURUSD", "H1", sample_start, end)
        assert isinstance(df, pd.DataFrame)
        assert not df.empty

    def test_load_required_columns(
        self,
        manager: HistoricalDataManager,
        sample_start: datetime,
    ) -> None:
        """Zarur ustunlar mavjud bo'lishi: open, high, low, close, tick_volume."""
        end = sample_start + timedelta(hours=10)
        df = manager.load("EURUSD", "H1", sample_start, end)
        required = {"open", "high", "low", "close", "tick_volume"}
        assert required.issubset(set(df.columns))

    def test_load_with_spread_column(
        self,
        data_dir_with_spread: Path,
        sample_start: datetime,
    ) -> None:
        """spread ustuni ham qaytishi — agar mavjud bo'lsa."""
        mgr = HistoricalDataManager(data_dir_with_spread)
        end = sample_start + timedelta(hours=10)
        df = mgr.load("EURUSD", "H1", sample_start, end)
        assert "spread" in df.columns

    def test_load_filters_by_date_range(
        self,
        manager: HistoricalDataManager,
        sample_start: datetime,
    ) -> None:
        """Faqat start..end oralig'idagi ma'lumot qaytishi."""
        start = sample_start + timedelta(hours=5)
        end = sample_start + timedelta(hours=15)
        df = manager.load("EURUSD", "H1", start, end)
        assert df.index.min() >= pd.Timestamp(start)
        assert df.index.max() <= pd.Timestamp(end)

    def test_load_timestamp_index_sorted(
        self,
        manager: HistoricalDataManager,
        sample_start: datetime,
    ) -> None:
        """Index timestamp bo'yicha tartiblangan bo'lishi."""
        end = sample_start + timedelta(hours=50)
        df = manager.load("EURUSD", "H1", sample_start, end)
        assert df.index.is_monotonic_increasing

    def test_load_returns_copy(
        self,
        manager: HistoricalDataManager,
        sample_start: datetime,
    ) -> None:
        """Qaytarilgan DataFrame kesh nusxasi emas — mustaqil copy bo'lishi."""
        end = sample_start + timedelta(hours=5)
        df1 = manager.load("EURUSD", "H1", sample_start, end)
        df2 = manager.load("EURUSD", "H1", sample_start, end)
        # Ikkinchi chaqiruv birinchisini o'zgartirmasligi kerak
        assert df1 is not df2

    def test_load_caches_data(
        self,
        manager: HistoricalDataManager,
        sample_start: datetime,
    ) -> None:
        """Bir marta yuklangandan keyin keshda saqlanishi."""
        end = sample_start + timedelta(hours=5)
        manager.load("EURUSD", "H1", sample_start, end)
        assert ("EURUSD", "H1") in manager._cache

    def test_load_file_not_found(self, tmp_path: Path) -> None:
        """Fayl topilmasa FileNotFoundError chiqishi."""
        mgr = HistoricalDataManager(tmp_path)
        with pytest.raises(FileNotFoundError):
            mgr.load("GBPUSD", "D1", datetime(2023, 1, 1), datetime(2023, 12, 31))

    def test_load_empty_range_returns_empty_df(
        self,
        manager: HistoricalDataManager,
    ) -> None:
        """Ma'lumot oralig'idan tashqari so'rov bo'sh DataFrame qaytarishi."""
        # 2025 yil — sintetik ma'lumotda yo'q
        df = manager.load("EURUSD", "H1", datetime(2025, 1, 1), datetime(2025, 1, 2))
        assert df.empty


# ── Test: get_candles_at() — look-ahead bias protection ────────────────


class TestGetCandlesAt:
    """get_candles_at() metodi testlari — faqat YOPILGAN candle'lar."""

    def test_returns_only_closed_candles(
        self,
        manager: HistoricalDataManager,
        sample_start: datetime,
    ) -> None:
        """
        Joriy candle qaytarilmasligi kerak.
        Masalan: vaqt 10:30 bo'lsa — H1 uchun 10:00 candle hali yopilmagan,
        shuning uchun 09:00 oxirgi qaytariladigan candle.
        """
        # Avval ma'lumot yuklab olamiz
        manager.load("EURUSD", "H1", sample_start, sample_start + timedelta(days=8))

        # Soat 10:30 — H1 uchun 10:00 candle hali yopilmagan
        ts = sample_start + timedelta(hours=10, minutes=30)
        candles = manager.get_candles_at("EURUSD", "H1", ts, count=100)

        # Oxirgi candle 10:00 dan OLDIN bo'lishi kerak (09:00 yoki undan oldingi)
        assert candles.index[-1] < pd.Timestamp(sample_start + timedelta(hours=10))

    def test_exact_candle_boundary_excluded(
        self,
        manager: HistoricalDataManager,
        sample_start: datetime,
    ) -> None:
        """
        Agar timestamp aynan candle boshlanishi bo'lsa — bu candle ham qaytarilmaydi.
        Masalan: vaqt aynan 10:00 — H1 10:00 candle HALI yopilmagan.
        """
        manager.load("EURUSD", "H1", sample_start, sample_start + timedelta(days=8))

        # Aynan 10:00 — bu candle yopilmagan deb hisoblanadi
        ts = sample_start + timedelta(hours=10)
        candles = manager.get_candles_at("EURUSD", "H1", ts, count=100)

        # Oxirgi candle 09:00 yoki undan oldin bo'lishi kerak
        assert candles.index[-1] <= pd.Timestamp(sample_start + timedelta(hours=9))

    def test_count_parameter_limits_results(
        self,
        manager: HistoricalDataManager,
        sample_start: datetime,
    ) -> None:
        """count parametri qaytariladigan candle sonini cheklaydi."""
        manager.load("EURUSD", "H1", sample_start, sample_start + timedelta(days=8))

        ts = sample_start + timedelta(hours=50)
        candles = manager.get_candles_at("EURUSD", "H1", ts, count=5)
        assert len(candles) <= 5

    def test_get_candles_at_m1_timeframe(
        self,
        manager: HistoricalDataManager,
        sample_start: datetime,
    ) -> None:
        """M1 timeframe bilan ishlash."""
        manager.load("EURUSD", "M1", sample_start, sample_start + timedelta(hours=16))

        # 2 soat = 120 daqiqa ichidagi candle'lar
        ts = sample_start + timedelta(hours=2, minutes=30)
        candles = manager.get_candles_at("EURUSD", "M1", ts, count=50)

        assert len(candles) <= 50
        # Oxirgi candle 02:30 dan oldin bo'lishi kerak (02:29 candle yopilgan)
        assert candles.index[-1] < pd.Timestamp(ts)

    def test_get_candles_at_raises_if_not_loaded(
        self,
        tmp_path: Path,
    ) -> None:
        """Ma'lumot yuklanmagan bo'lsa KeyError chiqishi."""
        mgr = HistoricalDataManager(tmp_path)
        with pytest.raises(KeyError, match="Ma'lumot topilmadi"):
            mgr.get_candles_at("EURUSD", "H1", datetime(2023, 6, 1, 10, 0))

    def test_returns_copy_not_reference(
        self,
        manager: HistoricalDataManager,
        sample_start: datetime,
    ) -> None:
        """Qaytarilgan DataFrame kesh nusxasi emas — mustaqil copy."""
        manager.load("EURUSD", "H1", sample_start, sample_start + timedelta(days=8))
        ts = sample_start + timedelta(hours=20)
        c1 = manager.get_candles_at("EURUSD", "H1", ts, count=5)
        c2 = manager.get_candles_at("EURUSD", "H1", ts, count=5)
        assert c1 is not c2
        pd.testing.assert_frame_equal(c1, c2)


# ── Test: get_tick() ────────────────────────────────────────────────────


class TestGetTick:
    """get_tick() metodi testlari — tick data yoki M1 fallback."""

    def test_get_tick_m1_fallback(
        self,
        manager: HistoricalDataManager,
        sample_start: datetime,
    ) -> None:
        """Tick data yo'q bo'lsa M1 candle'dan tahmin qilishi."""
        # M1 ma'lumotni yuklaymiz
        manager.load("EURUSD", "M1", sample_start, sample_start + timedelta(hours=16))

        # Tick so'rov — M1 fallback ishlatiladi
        ts = sample_start + timedelta(hours=1, minutes=30)
        tick = manager.get_tick("EURUSD", ts)

        assert isinstance(tick, dict)
        assert "time" in tick
        assert "bid" in tick
        assert "ask" in tick
        assert "last" in tick
        assert "volume" in tick
        assert tick["time"] == ts

    def test_get_tick_bid_ask_spread(
        self,
        manager: HistoricalDataManager,
        sample_start: datetime,
    ) -> None:
        """ask >= bid bo'lishi kerak (spread >= 0)."""
        manager.load("EURUSD", "M1", sample_start, sample_start + timedelta(hours=16))

        ts = sample_start + timedelta(hours=1, minutes=30)
        tick = manager.get_tick("EURUSD", ts)

        assert tick["ask"] >= tick["bid"]

    def test_get_tick_with_tick_cache(
        self,
        manager: HistoricalDataManager,
        sample_start: datetime,
    ) -> None:
        """Tick cache mavjud bo'lsa undan foydalanishi."""
        # Sintetik tick data
        tick_times = [
            sample_start + timedelta(seconds=i * 5) for i in range(100)
        ]
        tick_df = pd.DataFrame(
            {
                "bid": [1.1000 + i * 0.00001 for i in range(100)],
                "ask": [1.1002 + i * 0.00001 for i in range(100)],
                "last": [1.1001 + i * 0.00001 for i in range(100)],
                "volume": [10] * 100,
            },
            index=pd.DatetimeIndex(tick_times, name="timestamp"),
        )
        manager._tick_cache["EURUSD"] = tick_df

        ts = sample_start + timedelta(seconds=25)
        tick = manager.get_tick("EURUSD", ts)

        # Tick cache'dan qaytarilishi kerak
        assert "bid" in tick
        assert "ask" in tick


# ── Test: detect_gaps() ────────────────────────────────────────────────


class TestDetectGaps:
    """detect_gaps() metodi testlari — weekend, bayram, broker downtime gaplari."""

    def test_no_gaps_in_continuous_data(
        self,
        manager: HistoricalDataManager,
        sample_start: datetime,
    ) -> None:
        """Uzluksiz ma'lumotda gap bo'lmasligi kerak."""
        manager.load("EURUSD", "H1", sample_start, sample_start + timedelta(days=8))
        gaps = manager.detect_gaps("EURUSD", "H1")
        assert gaps == []

    def test_detects_gap_greater_than_1_5x(
        self,
        tmp_path: Path,
    ) -> None:
        """actual_delta > expected_delta * 1.5 bo'lsa gap sifatida aniqlanishi."""
        start = datetime(2023, 6, 1, 0, 0)
        # H1 candle'lar — lekin 5-dan keyin 10 soatlik gap
        timestamps = []
        for i in range(5):
            timestamps.append(start + timedelta(hours=i))
        # 10 soatlik gap (>> 1.5 * 1 soat)
        gap_start = timestamps[-1]
        gap_end = gap_start + timedelta(hours=10)
        for i in range(5):
            timestamps.append(gap_end + timedelta(hours=i))

        df = pd.DataFrame(
            {
                "timestamp": timestamps,
                "open": [1.1] * 10,
                "high": [1.11] * 10,
                "low": [1.09] * 10,
                "close": [1.1] * 10,
                "tick_volume": [100] * 10,
            }
        )
        _save_parquet(df, tmp_path / "EURUSD_H1.parquet")

        mgr = HistoricalDataManager(tmp_path)
        mgr.load("EURUSD", "H1", start, gap_end + timedelta(hours=5))
        gaps = mgr.detect_gaps("EURUSD", "H1")

        assert len(gaps) == 1
        assert gaps[0]["from"] == pd.Timestamp(gap_start)
        assert gaps[0]["to"] == pd.Timestamp(gap_end)

    def test_gap_duration_correct(
        self,
        tmp_path: Path,
    ) -> None:
        """Gap davomiyligi to'g'ri hisoblanishi."""
        start = datetime(2023, 6, 1, 0, 0)
        timestamps = [start, start + timedelta(hours=1), start + timedelta(hours=8)]
        df = pd.DataFrame(
            {
                "timestamp": timestamps,
                "open": [1.1] * 3,
                "high": [1.11] * 3,
                "low": [1.09] * 3,
                "close": [1.1] * 3,
                "tick_volume": [100] * 3,
            }
        )
        _save_parquet(df, tmp_path / "EURUSD_H1.parquet")

        mgr = HistoricalDataManager(tmp_path)
        mgr.load("EURUSD", "H1", start, start + timedelta(hours=10))
        gaps = mgr.detect_gaps("EURUSD", "H1")

        assert len(gaps) == 1
        assert gaps[0]["duration"] == pd.Timedelta(hours=7)

    def test_weekend_gap_detected(
        self,
        tmp_path: Path,
    ) -> None:
        """Weekend gap'lari is_weekend=True bo'lishi kerak."""
        # Juma 22:00 → Dushanba 00:00 (weekend gap)
        friday = datetime(2023, 6, 2, 22, 0)  # Juma
        monday = datetime(2023, 6, 5, 0, 0)  # Dushanba

        timestamps = [
            friday - timedelta(hours=2),
            friday - timedelta(hours=1),
            friday,
            monday,
            monday + timedelta(hours=1),
        ]

        df = pd.DataFrame(
            {
                "timestamp": timestamps,
                "open": [1.1] * 5,
                "high": [1.11] * 5,
                "low": [1.09] * 5,
                "close": [1.1] * 5,
                "tick_volume": [100] * 5,
            }
        )
        _save_parquet(df, tmp_path / "EURUSD_H1.parquet")

        mgr = HistoricalDataManager(tmp_path)
        mgr.load(
            "EURUSD",
            "H1",
            friday - timedelta(hours=3),
            monday + timedelta(hours=2),
        )
        gaps = mgr.detect_gaps("EURUSD", "H1")

        assert len(gaps) == 1
        assert gaps[0]["is_weekend"] is True

    def test_no_gap_at_exactly_1_5x(
        self,
        tmp_path: Path,
    ) -> None:
        """Agar actual_delta == expected_delta * 1.5 — bu GAP emas (> 1.5 kerak)."""
        start = datetime(2023, 6, 1, 0, 0)
        # 1.5x H1 = 90 daqiqa
        timestamps = [start, start + timedelta(minutes=90)]
        df = pd.DataFrame(
            {
                "timestamp": timestamps,
                "open": [1.1] * 2,
                "high": [1.11] * 2,
                "low": [1.09] * 2,
                "close": [1.1] * 2,
                "tick_volume": [100] * 2,
            }
        )
        _save_parquet(df, tmp_path / "EURUSD_H1.parquet")

        mgr = HistoricalDataManager(tmp_path)
        mgr.load("EURUSD", "H1", start, start + timedelta(hours=3))
        gaps = mgr.detect_gaps("EURUSD", "H1")

        assert gaps == []

    def test_detect_gaps_raises_if_not_loaded(
        self,
        tmp_path: Path,
    ) -> None:
        """Ma'lumot yuklanmagan bo'lsa KeyError chiqishi."""
        mgr = HistoricalDataManager(tmp_path)
        with pytest.raises(KeyError):
            mgr.detect_gaps("EURUSD", "H1")


# ── Test: _timeframe_to_seconds() ──────────────────────────────────────


class TestTimeframeToSeconds:
    """_timeframe_to_seconds() helper testi."""

    @pytest.mark.parametrize(
        "tf, expected",
        [
            ("M1", 60),
            ("M5", 300),
            ("M15", 900),
            ("M30", 1800),
            ("H1", 3600),
            ("H4", 14400),
            ("D1", 86400),
        ],
    )
    def test_all_supported_timeframes(self, tf: str, expected: int) -> None:
        """Barcha qo'llab-quvvatlanadigan timeframe'lar to'g'ri qiymat qaytarishi."""
        assert HistoricalDataManager._timeframe_to_seconds(tf) == expected

    def test_invalid_timeframe_raises(self) -> None:
        """Noto'g'ri timeframe ValueError chiqarishi."""
        with pytest.raises(ValueError, match="Noto'g'ri timeframe"):
            HistoricalDataManager._timeframe_to_seconds("W1")


# ── Test: _floor_time() ────────────────────────────────────────────────


class TestFloorTime:
    """_floor_time() helper testi."""

    def test_floor_m15(self) -> None:
        """M15 (900s) uchun floor: 10:37 -> 10:30."""
        dt = datetime(2023, 6, 1, 10, 37, 0)
        result = HistoricalDataManager._floor_time(dt, 900)
        assert result == datetime(2023, 6, 1, 10, 30, 0)

    def test_floor_h1(self) -> None:
        """H1 (3600s) uchun floor: 14:45 -> 14:00."""
        dt = datetime(2023, 6, 1, 14, 45, 0)
        result = HistoricalDataManager._floor_time(dt, 3600)
        assert result == datetime(2023, 6, 1, 14, 0, 0)

    def test_floor_exact_boundary(self) -> None:
        """Aynan chegarada bo'lsa — o'sha vaqtni qaytarishi."""
        dt = datetime(2023, 6, 1, 10, 0, 0)
        result = HistoricalDataManager._floor_time(dt, 3600)
        assert result == dt

    def test_floor_m1(self) -> None:
        """M1 (60s) uchun floor: 10:37:45 -> 10:37:00."""
        dt = datetime(2023, 6, 1, 10, 37, 45)
        result = HistoricalDataManager._floor_time(dt, 60)
        assert result == datetime(2023, 6, 1, 10, 37, 0)


# ── Test: _estimate_spread() ───────────────────────────────────────────


class TestEstimateSpread:
    """_estimate_spread() helper testi."""

    def test_eurusd_has_tight_spread(self) -> None:
        """EURUSD — eng tor spread."""
        spread = HistoricalDataManager._estimate_spread(
            "EURUSD", datetime(2023, 6, 1, 15, 0)
        )
        assert 0 < spread < 0.001

    def test_unknown_symbol_has_default_spread(self) -> None:
        """Noma'lum juftlik uchun default spread."""
        spread = HistoricalDataManager._estimate_spread(
            "XYZABC", datetime(2023, 6, 1, 12, 0)
        )
        assert spread > 0

    def test_asian_session_wider_spread(self) -> None:
        """Asian session (0-8 UTC) — kengaytirilgan spread."""
        asian = HistoricalDataManager._estimate_spread(
            "EURUSD", datetime(2023, 6, 1, 3, 0)
        )
        overlap = HistoricalDataManager._estimate_spread(
            "EURUSD", datetime(2023, 6, 1, 15, 0)
        )
        assert asian > overlap


# ── Test: _is_weekend_gap() ─────────────────────────────────────────────


class TestIsWeekendGap:
    """_is_weekend_gap() helper testi."""

    def test_friday_to_monday_is_weekend(self) -> None:
        """Juma -> Dushanba orasida weekend bor."""
        friday = datetime(2023, 6, 2, 22, 0)
        monday = datetime(2023, 6, 5, 0, 0)
        assert HistoricalDataManager._is_weekend_gap(friday, monday) is True

    def test_weekday_gap_is_not_weekend(self) -> None:
        """Hafta ichi gap — weekend emas."""
        tuesday = datetime(2023, 6, 6, 10, 0)  # Seshanba
        wednesday = datetime(2023, 6, 7, 10, 0)  # Chorshanba
        assert HistoricalDataManager._is_weekend_gap(tuesday, wednesday) is False


# ── Test: Caching mechanism ─────────────────────────────────────────────


class TestCaching:
    """Kesh mexanizmi testlari."""

    def test_second_load_uses_cache(
        self,
        manager: HistoricalDataManager,
        sample_start: datetime,
    ) -> None:
        """Ikkinchi load() chaqiruvida keshdan olinishi."""
        end = sample_start + timedelta(hours=5)
        manager.load("EURUSD", "H1", sample_start, end)

        # Keshda borligini tekshiramiz
        assert ("EURUSD", "H1") in manager._cache

        # Ikkinchi chaqiruv — keshdan
        df = manager.load("EURUSD", "H1", sample_start, end)
        assert not df.empty

    def test_cache_info(
        self,
        manager: HistoricalDataManager,
        sample_start: datetime,
    ) -> None:
        """cache_info() to'g'ri ma'lumot qaytarishi."""
        end = sample_start + timedelta(hours=5)
        manager.load("EURUSD", "H1", sample_start, end)

        info = manager.cache_info()
        assert info["cached_dataframes"] == 1
        assert info["total_rows"] > 0

    def test_multiple_symbols_cached_separately(
        self,
        tmp_path: Path,
        sample_start: datetime,
    ) -> None:
        """Turli symbol/timeframe juftliklari alohida keshlanishi."""
        # Ikkita sintetik parquet yaratamiz
        df1 = _make_ohlcv_df(sample_start, 50, 3600, base_price=1.1)
        df2 = _make_ohlcv_df(sample_start, 50, 3600, base_price=0.85)
        _save_parquet(df1, tmp_path / "EURUSD_H1.parquet")
        _save_parquet(df2, tmp_path / "GBPUSD_H1.parquet")

        mgr = HistoricalDataManager(tmp_path)
        end = sample_start + timedelta(hours=10)
        mgr.load("EURUSD", "H1", sample_start, end)
        mgr.load("GBPUSD", "H1", sample_start, end)

        assert ("EURUSD", "H1") in mgr._cache
        assert ("GBPUSD", "H1") in mgr._cache
        assert mgr.cache_info()["cached_dataframes"] == 2


# ── Test: Multi-timeframe support ───────────────────────────────────────


class TestMultiTimeframe:
    """Multi-timeframe qo'llab-quvvatlash testlari."""

    def test_m1_and_h1_loaded_together(
        self,
        manager: HistoricalDataManager,
        sample_start: datetime,
    ) -> None:
        """M1 va H1 bir vaqtda keshda bo'lishi mumkin."""
        end_h1 = sample_start + timedelta(days=5)
        end_m1 = sample_start + timedelta(hours=10)

        manager.load("EURUSD", "H1", sample_start, end_h1)
        manager.load("EURUSD", "M1", sample_start, end_m1)

        assert ("EURUSD", "H1") in manager._cache
        assert ("EURUSD", "M1") in manager._cache

    def test_timeframe_constants(self) -> None:
        """TIMEFRAME_SECONDS konstantasi to'liq bo'lishi."""
        expected_tfs = {"M1", "M5", "M15", "M30", "H1", "H4", "D1"}
        assert set(TIMEFRAME_SECONDS.keys()) == expected_tfs


# ── Test: preload() ─────────────────────────────────────────────────────


class TestPreload:
    """preload() metodi testlari."""

    def test_preload_loads_available_timeframes(
        self,
        manager: HistoricalDataManager,
    ) -> None:
        """Mavjud timeframe'larni oldindan yuklash."""
        manager.preload("EURUSD", ["M1", "H1"])
        assert ("EURUSD", "M1") in manager._cache
        assert ("EURUSD", "H1") in manager._cache

    def test_preload_skips_missing_files(
        self,
        manager: HistoricalDataManager,
    ) -> None:
        """Mavjud bo'lmagan fayllarni xatosiz o'tkazib yuborishi."""
        # D1 fayli yo'q — lekin xato chiqmasligi kerak
        manager.preload("EURUSD", ["H1", "D1"])
        assert ("EURUSD", "H1") in manager._cache
        assert ("EURUSD", "D1") not in manager._cache
