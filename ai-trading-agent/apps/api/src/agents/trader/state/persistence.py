"""
F0-1: Restart safety — trade meta-data atomik persistence.

Bot qayta ishga tushganda _trade_meta dict xotirada yo'qolmasligi uchun
har o'zgarish diskka yoziladi. Atomik (temp file + os.replace) — yarim
yozilgan fayl xavfi yo'q.

Reconstruct: MT5'dan magic==20240101 pozitsiyalarni topib, meta'ni
asoslab tiklash (faqat asosiy field'lar: entry, sl, tp, lot, direction).
Partial close history va custom field'lar yo'qoladi (acceptable trade-off).
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from loguru import logger

# State dir — task spec ga ko'ra parents[5] (apps/) ichida
_DEFAULT_STATE_DIR = Path(__file__).resolve().parents[5] / "data" / "state"
_DEFAULT_META_FILE = "trade_meta.json"

OPENCLAW_MAGIC = 20240101


def get_state_dir(state_dir: Optional[Path] = None) -> Path:
    d = state_dir or _DEFAULT_STATE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_trade_meta(meta: dict, state_dir: Optional[Path] = None) -> None:
    """
    Atomik yozish: temp file ga yozib, keyin os.replace bilan asl faylga ko'chirish.
    Yarim yozilgan fayl xavfi yo'q.
    """
    d = get_state_dir(state_dir)
    target = d / _DEFAULT_META_FILE
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "magic": OPENCLAW_MAGIC,
        "meta": _serialize(meta),  # datetime → ISO string
    }
    try:
        # tempfile.mkstemp — keyin os.replace bilan atomik ko'chirish
        fd, tmp_path = tempfile.mkstemp(prefix=".trade_meta_", suffix=".json", dir=str(d))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
            os.replace(tmp_path, target)  # atomik
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as e:
        logger.error(f"save_trade_meta: failed to persist — {e}")


def load_trade_meta(state_dir: Optional[Path] = None) -> dict:
    """
    Diskdan o'qib qaytarish. Yo'q yoki buzilgan bo'lsa — bo'sh dict.
    Reconstruct (MT5'dan) bu metodda emas — agent.py o'zi qiladi.
    """
    d = get_state_dir(state_dir)
    target = d / _DEFAULT_META_FILE
    if not target.exists():
        logger.info(f"load_trade_meta: no state file at {target}")
        return {}
    try:
        with open(target, "r", encoding="utf-8") as f:
            payload = json.load(f)
        meta_raw = payload.get("meta", {})
        # JSON key'lar string — ticket'larni int ga aylantirish
        meta: dict = {}
        for k, v in meta_raw.items():
            try:
                ticket = int(k)
                meta[ticket] = _deserialize(v)
            except (ValueError, TypeError):
                logger.warning(f"load_trade_meta: invalid ticket key {k!r}, skip")
        logger.info(f"load_trade_meta: loaded {len(meta)} positions from {target}")
        return meta
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"load_trade_meta: failed — {e}")
        return {}


def recover_from_mt5(mt5_connector, magic: int = OPENCLAW_MAGIC) -> dict:
    """
    MT5'dan ochiq pozitsiyalarni o'qib, magic == OPENCLAW bo'lganlardan
    meta'ni asoslab tiklash. Faqat basic field'lar — partial close history
    va custom MT-meta yo'qoladi (acceptable, fallback variant).

    Note: mt5_connector.get_open_positions() allaqachon magic==OPENCLAW
    bilan filtrlangan list qaytaradi (mt5_connector.py:250-251).
    """
    try:
        positions = mt5_connector.get_open_positions() or []
    except Exception as e:
        logger.error(f"recover_from_mt5: get_open_positions failed — {e}")
        return {}
    recovered: dict = {}
    for pos in positions:
        # get_open_positions allaqachon magic==OPENCLAW filtri qiladi,
        # lekin paranoia uchun yana tekshiramiz
        if int(pos.get("magic", 0)) != magic:
            continue
        try:
            ticket = int(pos["ticket"])
        except (KeyError, ValueError, TypeError):
            continue
        # MT5 position type: 0 = BUY, 1 = SELL
        pos_type = int(pos.get("type", 0))
        recovered[ticket] = {
            "entry":     float(pos.get("price_open", 0)),
            "sl":        float(pos.get("sl", 0)),
            "tp":        float(pos.get("tp", 0)),
            "tp1":       float(pos.get("tp", 0)),  # asoslab — TP1 = position TP
            "tp2":       0.0,
            "tp3":       0.0,
            "lot":       float(pos.get("volume", 0)),
            "direction": "buy" if pos_type == 0 else "sell",
            "opened_at": pos.get("time", None),
            "tf":        "H1",        # noma'lum — default
            "mode":      "FLOW",     # noma'lum — default
            "label":     "RECOVERED",
            "set_id":    "",
            "tp_label":  "TP1",
            "sl_pips":   0.0,         # noma'lum
            "be_done":   False,
            "tp1_partial_done": False,
            "tp2_partial_done": False,
            "tp3_done":         False,
            "pnl":       0.0,
            "recovered": True,        # marker — boshqa flow'larda bilish uchun
        }
    logger.info(f"recover_from_mt5: reconstructed {len(recovered)} OpenClaw positions")
    return recovered


def _serialize(meta: dict) -> dict:
    """datetime ↔ ISO string. Boshqa typelar default str() bilan."""
    out = {}
    for k, v in meta.items():
        out[str(k)] = _serialize_value(v)
    return out


def _serialize_value(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _serialize_value(vv) for k, vv in v.items()}
    if isinstance(v, (list, tuple)):
        return [_serialize_value(x) for x in v]
    return v


def _deserialize(v: Any) -> Any:
    """ISO string → datetime tahmini. Buni avtomatik qilmang — meta strukturasi muhim.

    Hozircha — dict'ni shunchaki qaytarish. Datetime parse'ni agent.py o'zi qiladi
    (chunki qaysi field datetime ekanini agent biladi).
    """
    return v
