"""F0-1 test: state/persistence.py — atomik save/load + recover_from_mt5"""
import sys
import os
import json
import tempfile
import shutil
from datetime import datetime, timezone
from pathlib import Path

# Loyiha rootini sys.path'ga qo'shish — `ai-trading-agent/` papka
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Module'ni import qilamiz (state_dir parametri orqali izolatsiya qilamiz)
from apps.api.src.agents.trader.state.persistence import (
    save_trade_meta,
    load_trade_meta,
    recover_from_mt5,
    OPENCLAW_MAGIC,
)

PASSED = 0
FAILED = 0


def test(name):
    def decorator(fn):
        global PASSED, FAILED
        try:
            fn()
            PASSED += 1
            print(f"  [PASS] {name}")
        except AssertionError as e:
            FAILED += 1
            print(f"  [FAIL] {name}: {e}")
        except Exception as e:
            FAILED += 1
            print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
        return fn
    return decorator


def _tmp_state_dir():
    """Vaqtinchalik state dir yaratish — har test izolatsiyalangan"""
    return Path(tempfile.mkdtemp(prefix="f0_persistence_test_"))


class MockMT5Connector:
    """recover_from_mt5 uchun minimal mock"""
    def __init__(self, positions=None, raise_exc=False):
        self._positions = positions or []
        self._raise = raise_exc

    def get_open_positions(self):
        if self._raise:
            raise RuntimeError("simulated MT5 failure")
        return self._positions


print("\n=== F0-1: persistence tests ===")


@test("save_trade_meta atomik yozadi va fayl mavjud")
def _():
    d = _tmp_state_dir()
    try:
        meta = {101: {"entry": 2050.0, "sl": 2045.0, "tp": 2060.0, "direction": "buy"}}
        save_trade_meta(meta, state_dir=d)
        target = d / "trade_meta.json"
        assert target.exists(), f"trade_meta.json yaratilmadi: {target}"
        # JSON valid bo'lishi kerak
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert "meta" in data
        assert "saved_at" in data
        assert data["magic"] == OPENCLAW_MAGIC
        # Temp fayllari qolmagan bo'lishi kerak
        leftovers = [p for p in d.iterdir() if p.name.startswith(".trade_meta_")]
        assert not leftovers, f"Vaqtinchalik fayl qoldi: {leftovers}"
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test("save -> load round-trip aniq (int key saqlanadi)")
def _():
    d = _tmp_state_dir()
    try:
        original = {
            123: {"entry": 2050.5, "sl": 2045.0, "tp": 2060.0, "direction": "buy", "lot": 0.01},
            456: {"entry": 2100.0, "sl": 2110.0, "tp": 2090.0, "direction": "sell", "lot": 0.02},
        }
        save_trade_meta(original, state_dir=d)
        loaded = load_trade_meta(state_dir=d)
        assert len(loaded) == 2, f"Expected 2 entries, got {len(loaded)}"
        # Key int bo'lishi kerak
        for k in loaded.keys():
            assert isinstance(k, int), f"Key {k!r} int emas (type={type(k).__name__})"
        # Qiymatlar mos kelishi kerak
        assert loaded[123]["entry"] == 2050.5
        assert loaded[123]["direction"] == "buy"
        assert loaded[456]["direction"] == "sell"
        assert loaded[456]["lot"] == 0.02
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test("load_trade_meta yo'q faylda bo'sh dict qaytaradi")
def _():
    d = _tmp_state_dir()
    try:
        result = load_trade_meta(state_dir=d)
        assert result == {}, f"Bo'sh dict kutilgan, lekin {result!r}"
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test("load_trade_meta buzilgan JSON da bo'sh dict")
def _():
    d = _tmp_state_dir()
    try:
        target = d / "trade_meta.json"
        with open(target, "w", encoding="utf-8") as f:
            f.write("{not: valid: json")
        result = load_trade_meta(state_dir=d)
        assert result == {}, f"Buzilgan JSON da bo'sh dict kutilgan, lekin {result!r}"
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test("load_trade_meta invalid ticket key'larni skip qiladi")
def _():
    d = _tmp_state_dir()
    try:
        target = d / "trade_meta.json"
        payload = {
            "saved_at": "2025-01-01T00:00:00+00:00",
            "magic": OPENCLAW_MAGIC,
            "meta": {
                "123": {"entry": 2050.0},
                "notanumber": {"entry": 2100.0},
                "456": {"entry": 2200.0},
            },
        }
        with open(target, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        result = load_trade_meta(state_dir=d)
        assert 123 in result, f"123 yo'q: {result}"
        assert 456 in result, f"456 yo'q: {result}"
        assert "notanumber" not in result
        assert len(result) == 2
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test("recover_from_mt5 magic filter ishlaydi")
def _():
    positions = [
        {"ticket": 111, "magic": OPENCLAW_MAGIC, "price_open": 2050.0, "sl": 2045.0, "tp": 2060.0, "volume": 0.01, "type": 0, "time": 1234567890},
        {"ticket": 222, "magic": 99999, "price_open": 2050.0, "sl": 0, "tp": 0, "volume": 0.01, "type": 0, "time": 0},
        {"ticket": 333, "magic": OPENCLAW_MAGIC, "price_open": 2100.0, "sl": 2110.0, "tp": 2090.0, "volume": 0.02, "type": 1, "time": 1234567899},
    ]
    mock = MockMT5Connector(positions=positions)
    result = recover_from_mt5(mock)
    assert len(result) == 2, f"2 ta OpenClaw position kutilgan, lekin {len(result)}: {list(result.keys())}"
    assert 111 in result
    assert 333 in result
    assert 222 not in result  # boshqa magic
    assert result[111]["direction"] == "buy"
    assert result[333]["direction"] == "sell"
    assert result[111]["entry"] == 2050.0
    assert result[333]["lot"] == 0.02
    assert result[111]["recovered"] is True


@test("recover_from_mt5 — get_open_positions exception'da bo'sh dict")
def _():
    mock = MockMT5Connector(raise_exc=True)
    result = recover_from_mt5(mock)
    assert result == {}, f"Exception'da bo'sh dict kutilgan, lekin {result!r}"


@test("recover_from_mt5 — bo'sh ro'yxat -> bo'sh dict")
def _():
    mock = MockMT5Connector(positions=[])
    result = recover_from_mt5(mock)
    assert result == {}, f"Bo'sh kutilgan, lekin {result!r}"


@test("save_trade_meta datetime field'lari ISO string'ga aylantiriladi")
def _():
    d = _tmp_state_dir()
    try:
        dt = datetime(2025, 1, 15, 12, 30, 0, tzinfo=timezone.utc)
        meta = {789: {"opened_at": dt, "entry": 2050.0}}
        save_trade_meta(meta, state_dir=d)
        # Raw JSON ni o'qib datetime ISO bo'lganini tekshiramiz
        target = d / "trade_meta.json"
        with open(target, "r", encoding="utf-8") as f:
            raw = json.load(f)
        opened_at_raw = raw["meta"]["789"]["opened_at"]
        assert isinstance(opened_at_raw, str), f"datetime ISO string emas: {opened_at_raw!r}"
        assert "2025-01-15" in opened_at_raw
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test("save -> load -> save (idempotent qayta yozish)")
def _():
    d = _tmp_state_dir()
    try:
        meta1 = {100: {"entry": 2050.0, "direction": "buy"}}
        save_trade_meta(meta1, state_dir=d)
        loaded1 = load_trade_meta(state_dir=d)
        # Yangi field qo'shib qayta yozamiz
        loaded1[100]["sl"] = 2045.0
        loaded1[200] = {"entry": 2100.0, "direction": "sell"}
        save_trade_meta(loaded1, state_dir=d)
        loaded2 = load_trade_meta(state_dir=d)
        assert 100 in loaded2 and 200 in loaded2
        assert loaded2[100]["sl"] == 2045.0
        assert loaded2[200]["direction"] == "sell"
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test("JSON key normalize: int ticket -> string -> int round-trip")
def _():
    d = _tmp_state_dir()
    try:
        # Int key bilan saqlaymiz
        meta = {999888: {"entry": 2050.0}}
        save_trade_meta(meta, state_dir=d)
        # Raw faylda key string bo'lishi kerak
        target = d / "trade_meta.json"
        with open(target, "r", encoding="utf-8") as f:
            raw = json.load(f)
        assert "999888" in raw["meta"], f"String key kutilgan, raw keys: {list(raw['meta'].keys())}"
        # load orqali yana int bo'lib qaytishi kerak
        loaded = load_trade_meta(state_dir=d)
        assert 999888 in loaded, f"Int key kutilgan: {list(loaded.keys())}"
        assert isinstance(list(loaded.keys())[0], int)
    finally:
        shutil.rmtree(d, ignore_errors=True)


print(f"\nResult: {PASSED} passed, {FAILED} failed")
sys.exit(0 if FAILED == 0 else 1)
