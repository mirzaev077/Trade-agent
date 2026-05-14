"""
SelfLearner — XAUUSD self-learning trade performance tracker.
Logs trades, analyzes every 20, evolves every 50.

lessons.json schema (backward compatible):
  {
    "lessons": [
      # Eski format (warning-only, hech qachon o'zgartirilmaydi):
      {"warning": "...", "conditions": {...}, "count": N, "created": "..."},

      # Yangi format (action-based, F0-5 fix uchun):
      # Qabul qilingan actions: "disable"
      # `setup` — `_extract_type()` natijasiga mos kelishi kerak ("OB", "FVG", "RB", "BB" …)
      {"setup": "RB", "action": "disable", "reason": "...", "created": "..."}
    ],
    ...
  }

Inson `lessons.json` ga `{"setup": "RB", "action": "disable"}` yozsa,
self-learner avtomatik weight evolution orqali bu setup'ni qayta yoqa olmaydi —
`get_setup_weight()` har doim 0.0 qaytaradi.

F1-1 — Admin approval gate:
  AUTO_APPLY_LEARNING env flag default `false`. False bo'lganda analyze() va
  evolve() learned.json'ning `adapted/*` qismini to'g'ridan-to'g'ri o'zgartirmaydi —
  o'rniga `pending_adjustments` jadvaliga propose yozadi va admin'ga Telegram
  xabar yuboradi. Admin `tools/admin_cli.py approve` orqali tasdiqlaganda
  learned.json yangilanadi. AUTO_APPLY_LEARNING=true bo'lsa eski xulq saqlanadi
  (legacy direct-mutate).
"""
import json
import os
from datetime import datetime, timezone
from loguru import logger

# F1-1 imports — DB approval gate va Telegram notify
try:
    from apps.api.src.agents.trader.state import db as _db
except Exception:  # noqa: BLE001 — paket yo'qligida self-learner sinmasin
    _db = None  # type: ignore
try:
    from apps.api.src.agents.trader.utils import telegram_bot as _tg
except Exception:  # noqa: BLE001
    _tg = None  # type: ignore


def _auto_apply_enabled() -> bool:
    """Har chaqiruvda env'ni o'qiydi (test'larda monkeypatch oson)."""
    return os.getenv("AUTO_APPLY_LEARNING", "false").strip().lower() in (
        "true", "1", "yes", "on"
    )


_DEFAULT_WEIGHTS = {
    "OB": 1.0, "FVG": 1.0, "BB": 1.0, "OTE": 1.0,
    "IFVG": 1.0, "BISI": 1.0, "SIBI": 1.0, "BPR": 1.0,
    "PDH": 1.0, "PDL": 1.0, "CRT": 1.0, "NWOG": 1.0,
    "AR_HI": 1.0, "AR_LO": 1.0, "Open": 1.0,
    "LiqVoid": 0.9, "BOS_S": 0.9, "CISD": 0.9,
}

_DEFAULT_DATA = {
    "trades": [],
    "adapted": {
        "setup_weights":    dict(_DEFAULT_WEIGHTS),
        "entry_pct":        0.30,  # OB entry offset (0.30 = 30% of OB range)
        "sl_multiplier":    1.0,   # SL width multiplier
        "disabled":         [],    # setup types temporarily disabled
        "session_weights":  {},    # "london_monday": 0.8 etc
    },
    "stats": {
        "total":              0,
        "last_analysis_at":   0,   # trade count at last analyze()
        "last_evolution_at":  0,
    },
    # Session + day-of-week win statistics
    "session_day_stats": {},       # "london_monday": {"wins": 0, "losses": 0}
}


class SelfLearner:
    ANALYZE_EVERY = 20
    EVOLVE_EVERY  = 50
    MIN_WEIGHT    = 0.1
    MAX_WEIGHT    = 2.0

    def __init__(self, path: str = None, lessons_path: str = None):
        if path is None:
            base = os.path.dirname(__file__)
            path = os.path.join(base, "learned.json")
        self.path = path

        # lessons.json — inson tomonidan qo'lda boshqariladigan hard-override fayli
        if lessons_path is None:
            base = os.path.dirname(__file__)
            lessons_path = os.path.join(base, "lessons.json")
        self.lessons_path = lessons_path
        self._lessons_mtime: float = 0.0
        self._disabled_setups: set = set()
        self._refresh_disabled_setups()  # initial load

        self.data = self._load()

        # F1-1 — DB init (idempotent). Crash bermaslik shart.
        if _db is not None:
            try:
                _db.init_db()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"SelfLearner: db.init_db() xato — {e}")

    # ── Persistence ───────────────────────────────────────────────

    def _load_disabled_setups(self) -> set:
        """
        lessons.json'dan barcha `action == "disable"` setup'larni o'qib qaytaradi.
        Malformed JSON yoki yo'q fayl → bo'sh set (crash emas).
        """
        disabled: set = set()
        try:
            if not os.path.exists(self.lessons_path):
                return disabled
            with open(self.lessons_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if not isinstance(payload, dict):
                return disabled
            lessons = payload.get("lessons", [])
            if not isinstance(lessons, list):
                return disabled
            for entry in lessons:
                if not isinstance(entry, dict):
                    continue
                action = entry.get("action")
                setup = entry.get("setup")
                if action == "disable" and isinstance(setup, str) and setup:
                    disabled.add(setup.upper())
        except (json.JSONDecodeError, OSError, ValueError) as e:
            logger.warning(f"SelfLearner lessons.json parse error: {e}")
            return set()
        except Exception as e:
            logger.warning(f"SelfLearner lessons.json unexpected error: {e}")
            return set()
        return disabled

    def _refresh_disabled_setups(self) -> None:
        """
        lessons.json mtime o'zgargan bo'lsa qayta yuklaydi.
        Har get_setup_weight() chaqiruvida fayl o'qish kerak emas — faqat stat().
        """
        try:
            if not os.path.exists(self.lessons_path):
                # Fayl yo'q bo'lsa cache'ni tozalash
                if self._lessons_mtime != 0.0 or self._disabled_setups:
                    self._lessons_mtime = 0.0
                    self._disabled_setups = set()
                return
            mtime = os.path.getmtime(self.lessons_path)
            if mtime != self._lessons_mtime:
                self._disabled_setups = self._load_disabled_setups()
                self._lessons_mtime = mtime
                if self._disabled_setups:
                    logger.info(
                        f"[SelfLearner] lessons.json reload: disabled setups = "
                        f"{sorted(self._disabled_setups)}"
                    )
        except OSError as e:
            logger.warning(f"SelfLearner lessons.json stat error: {e}")

    def _load(self) -> dict:
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    # Merge missing keys from default
                    for k, v in _DEFAULT_DATA.items():
                        if k not in loaded:
                            loaded[k] = v
                    for k, v in _DEFAULT_DATA["adapted"].items():
                        if k not in loaded.get("adapted", {}):
                            loaded.setdefault("adapted", {})[k] = v
                    return loaded
        except Exception as e:
            logger.warning(f"SelfLearner load error: {e}")
        import copy
        return copy.deepcopy(_DEFAULT_DATA)

    def _save(self):
        """
        F1-1: AUTO_APPLY_LEARNING=false bo'lganda `adapted/*` qismini admin_cli
        boshqaradi. Bu metod faqat `trades`/`stats`/`session_day_stats` yozadi,
        `adapted/*` diskdagi holatdan o'qib merge qilinadi — bot self_learner
        eski cache bilan admin'ning approve qilgan o'zgarishini ezib yubormaydi.
        """
        try:
            if not _auto_apply_enabled() and os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        on_disk = json.load(f)
                    if isinstance(on_disk, dict) and "adapted" in on_disk:
                        # In-memory cache'ni ham yangilaymiz — keyingi get_setup_weight()
                        # admin yangiliklarini ko'radi
                        self.data["adapted"] = on_disk["adapted"]
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"SelfLearner _save: adapted reload skip — {e}")

            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"SelfLearner save error: {e}")

    # ── F1-1: Approval gate helpers ───────────────────────────────

    def _propose_or_apply(self, param: str, old_value, new_value, reason: str) -> None:
        """
        AUTO_APPLY_LEARNING=true → bevosita qo'llaydi (eski xulq) + audit.
        Aks holda → pending_adjustments'ga yozadi + Telegram notify.
        """
        # No-op tekshiruvi — bir xil qiymat propose qilinmasin
        try:
            if old_value is not None and new_value is not None:
                if abs(float(old_value) - float(new_value)) < 1e-9:
                    return
        except (TypeError, ValueError):
            pass

        if _auto_apply_enabled():
            self._apply_locally(param, new_value)
            if _db is not None:
                try:
                    adj_id = _db.propose_adjustment(
                        param, old_value, new_value, f"auto-apply: {reason}"
                    )
                    _db.approve_adjustment(adj_id, applied_by="auto-apply")
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"SelfLearner audit insert xato — {e}")
            logger.info(
                f"[SelfLearner] AUTO-APPLY {param}: {old_value}→{new_value} ({reason})"
            )
            return

        if _db is None:
            # DB yo'q — gate ishlamaydi, lekin in-memory ham mutatsiya qilinmaydi.
            logger.warning(
                f"[SelfLearner] propose skipped (no DB): {param} {old_value}→{new_value}"
            )
            return

        try:
            adj_id = _db.propose_adjustment(param, old_value, new_value, reason)
        except Exception as e:  # noqa: BLE001
            logger.error(f"SelfLearner: propose_adjustment xato — {e}")
            return

        logger.info(
            f"[SelfLearner] PROPOSED {param}: {old_value}→{new_value} "
            f"(id={adj_id[:8]}) — {reason}"
        )

        # Best-effort Telegram alert
        if _tg is not None:
            try:
                _tg.notify_pending_adjustment_sync(
                    param, old_value, new_value, reason, adj_id
                )
            except Exception as e:  # noqa: BLE001
                logger.debug(f"SelfLearner: telegram notify xato — {e}")

    def _apply_locally(self, param: str, value) -> None:
        """AUTO_APPLY rejimida self.data['adapted']'ga to'g'ridan-to'g'ri yozish."""
        a = self.data.setdefault("adapted", {})
        if param.startswith("setup_weights."):
            key = param[len("setup_weights."):]
            a.setdefault("setup_weights", {})[key] = float(value) if value is not None else 0.0
        elif param.startswith("disabled."):
            key = param[len("disabled."):]
            disabled = a.setdefault("disabled", [])
            if value is None:
                if key not in disabled:
                    disabled.append(key)
            else:
                if key in disabled:
                    disabled.remove(key)
                a.setdefault("setup_weights", {})[key] = float(value)
        elif param == "entry_pct":
            a["entry_pct"] = float(value)
        elif param == "sl_multiplier":
            a["sl_multiplier"] = float(value)
        elif param.startswith("session_weights."):
            key = param[len("session_weights."):]
            sw = a.setdefault("session_weights", {})
            if value is None:
                sw.pop(key, None)
            else:
                sw[key] = float(value)
        else:
            logger.warning(f"SelfLearner _apply_locally: noma'lum param {param!r}")

    # ── Public API ────────────────────────────────────────────────

    def log_trade(
        self,
        entry_type: str,
        timeframe:  str,
        session:    str,
        direction:  str,
        sl_pips:    float,
        tp_pips:    float,
        result:     str,       # "win" | "loss" | "be"
        pnl:        float,
        mode:       str = "FLOW",
        regime:     str = "range",
    ):
        """Log a closed trade. Triggers analyze/evolve when thresholds hit."""
        now = datetime.now(timezone.utc)
        day_name = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"][now.weekday()]
        session_day_key = f"{session}_{day_name}"

        record = {
            "ts":              now.isoformat(),
            "entry_type":      self._extract_type(entry_type),
            "tf":              timeframe,
            "session":         session,
            "day":             day_name,
            "session_day_key": session_day_key,
            "direction":       direction,
            "sl_pips":         round(sl_pips, 1),
            "tp_pips":         round(tp_pips, 1),
            "result":          result,
            "pnl":             round(pnl, 2),
            "mode":            mode,
            "regime":          regime,
        }
        self.data["trades"].append(record)
        self.data["stats"]["total"] += 1
        total = self.data["stats"]["total"]

        # Session/day stats yangilash
        sd = self.data.setdefault("session_day_stats", {})
        bucket = sd.setdefault(session_day_key, {"wins": 0, "losses": 0})
        if result == "win":
            bucket["wins"] += 1
        elif result == "loss":
            bucket["losses"] += 1

        if total - self.data["stats"]["last_analysis_at"] >= self.ANALYZE_EVERY:
            self.analyze()
        if total - self.data["stats"]["last_evolution_at"] >= self.EVOLVE_EVERY:
            self.evolve()

        self._save()

    def get_session_day_weight(self, session: str, day_name: str) -> float:
        """
        Session + kun bo'yicha win rate asosida weight qaytaradi.
        Kam ma'lumot bo'lsa (< 5 trade) → 1.0 (neytral).
        """
        key = f"{session}_{day_name}"
        bucket = self.data.get("session_day_stats", {}).get(key, {})
        wins   = bucket.get("wins", 0)
        losses = bucket.get("losses", 0)
        total  = wins + losses
        if total < 5:
            return 1.0
        wr = wins / total
        if wr >= 0.65:
            return 1.3
        if wr >= 0.50:
            return 1.0
        if wr >= 0.35:
            return 0.75
        return 0.5   # bu session/kunda trade qilmaslik yaxshi

    def get_stats_summary(self, max_trades: int = 50) -> dict:
        """
        AI prompti uchun: oxirgi N tradedagi setup type win rate'lari.
        Returns: {"OB": {"win_rate": 0.72, "total": 25}, ...}
        """
        recent = self.data["trades"][-max_trades:]
        by_type: dict = {}
        for t in recent:
            typ = t.get("entry_type", "OB")
            by_type.setdefault(typ, {"wins": 0, "losses": 0})
            if t["result"] == "win":
                by_type[typ]["wins"] += 1
            elif t["result"] == "loss":
                by_type[typ]["losses"] += 1
        summary = {}
        for typ, c in by_type.items():
            total = c["wins"] + c["losses"]
            if total < 3:
                continue
            summary[typ] = {
                "win_rate": round(c["wins"] / total, 2),
                "total":    total,
            }
        return summary

    def get_setup_stats(self) -> dict:
        """Alias for get_stats_summary — called from agent.py AI brain section."""
        return self.get_stats_summary()

    def get_setup_weight(self, zone_label: str) -> float:
        """
        Return weight multiplier for a zone label (e.g. 'H4_OB').

        Priority:
          1) lessons.json hard-override (`action: disable`) → 0.0 (avto-mutation orqali qayta yoqilmaydi)
          2) learned.json `adapted.disabled` (avto-evolution natijasi) → 0.0
          3) learned.json `adapted.setup_weights[typ]` → float
        """
        typ = self._extract_type(zone_label)

        # 1) Inson tomonidan qo'lda disable qilingan setup'lar (lessons.json) — eng yuqori ustuvorlik
        self._refresh_disabled_setups()
        if typ.upper() in self._disabled_setups:
            return 0.0

        # 2) Self-learner avto-disable qilgan setup'lar
        if typ in self.data["adapted"]["disabled"]:
            return 0.0

        # 3) Odatdagi weight
        w = self.data["adapted"]["setup_weights"]
        return float(w.get(typ, w.get("OB", 1.0)))

    def get_entry_pct(self) -> float:
        return float(self.data["adapted"]["entry_pct"])

    def get_sl_multiplier(self) -> float:
        return float(self.data["adapted"]["sl_multiplier"])

    def get_regime(self, ict_h4, ict_h1) -> str:
        """Detect market regime: trend / range / chop."""
        h4_trend = ict_h4.structure.get("trend", "sideways")
        h4_bos   = int(ict_h4.structure.get("bos", 0))
        h4_choch = ict_h4.structure.get("choch", False)
        h1_trend = ict_h1.structure.get("trend", "sideways")

        if h4_trend != "sideways" and h4_bos >= 2:
            return "trend"
        if h4_trend == "sideways" and h1_trend == "sideways" and not h4_choch:
            return "chop"
        return "range"

    # ── Analysis (every 20 trades) ────────────────────────────────

    def analyze(self):
        recent = self.data["trades"][-self.ANALYZE_EVERY:]
        if len(recent) < 10:
            return

        self.data["stats"]["last_analysis_at"] = self.data["stats"]["total"]

        # Winrate per setup type
        by_type: dict = {}
        for t in recent:
            typ = t.get("entry_type", "OB")
            by_type.setdefault(typ, {"w": 0, "l": 0})
            if t["result"] == "win":
                by_type[typ]["w"] += 1
            elif t["result"] == "loss":
                by_type[typ]["l"] += 1

        weights = self.data["adapted"]["setup_weights"]
        disabled = self.data["adapted"]["disabled"]
        changes: list[str] = []

        for typ, counts in by_type.items():
            total = counts["w"] + counts["l"]
            if total < 3:
                continue
            wr = counts["w"] / total

            old_w = weights.get(typ, 1.0)
            if wr >= 0.60:
                new_w = min(old_w + 0.20, self.MAX_WEIGHT)
            elif wr >= 0.45:
                new_w = old_w   # neutral
            elif wr >= 0.30:
                new_w = max(old_w - 0.15, self.MIN_WEIGHT)
            else:
                new_w = max(old_w - 0.25, self.MIN_WEIGHT)

            new_w = round(new_w, 2)

            # Weight o'zgarishi (faqat sezilarli farq bo'lsa)
            if abs(new_w - old_w) >= 0.01:
                self._propose_or_apply(
                    f"setup_weights.{typ}", old_w, new_w,
                    f"analyze: WR={wr*100:.0f}% over {total} {typ} trades"
                )
                changes.append(f"{typ}: WR={wr*100:.0f}% w={old_w}→{new_w:.2f}")

            # Disable propose (juda yomon WR + min weight)
            if wr < 0.30 and new_w <= self.MIN_WEIGHT and typ not in disabled:
                self._propose_or_apply(
                    f"disabled.{typ}", None, None,  # ikkalasi ham null = disable
                    f"analyze: WR={wr*100:.0f}% triggered disable"
                )
                changes.append(f"DISABLE {typ} (WR={wr*100:.0f}%)")

            # Re-enable propose
            if typ in disabled and wr >= 0.50:
                self._propose_or_apply(
                    f"disabled.{typ}", None, new_w,  # new=non-null = re-enable
                    f"analyze: WR={wr*100:.0f}% suggests re-enable"
                )
                changes.append(f"RE-ENABLE {typ} (WR={wr*100:.0f}%)")

        # Entry precision adaptation
        win_trades = [t for t in recent if t["result"] == "win"]
        loss_trades = [t for t in recent if t["result"] == "loss"]
        overall_wr = len(win_trades) / len(recent) if recent else 0.5

        cur_pct = self.data["adapted"]["entry_pct"]
        new_pct = cur_pct
        if overall_wr >= 0.60 and cur_pct < 0.50:
            new_pct = round(min(cur_pct + 0.05, 0.70), 2)
        elif overall_wr < 0.40 and cur_pct > 0.30:
            new_pct = round(max(cur_pct - 0.05, 0.30), 2)

        if abs(new_pct - cur_pct) >= 0.01:
            self._propose_or_apply(
                "entry_pct", cur_pct, new_pct,
                f"analyze: overall_wr={overall_wr*100:.0f}%"
            )
            changes.append(f"entry_pct: {cur_pct:.2f}→{new_pct:.2f}")

        # SL adaptation
        if loss_trades:
            avg_sl_loss = sum(t["sl_pips"] for t in loss_trades) / len(loss_trades)
            avg_sl_win  = sum(t["sl_pips"] for t in win_trades) / len(win_trades) if win_trades else avg_sl_loss
            cur_sl = self.data["adapted"]["sl_multiplier"]
            if avg_sl_loss > avg_sl_win * 1.3:
                new_sl = round(max(cur_sl - 0.1, 0.7), 2)
                if abs(new_sl - cur_sl) >= 0.01:
                    self._propose_or_apply(
                        "sl_multiplier", cur_sl, new_sl,
                        "analyze: loss_sl > win_sl * 1.3"
                    )
                    changes.append(f"sl_mult: {cur_sl:.1f}→{new_sl:.1f}")

        logger.info(
            f"[SelfLearner] Analysis #{self.data['stats']['total']} "
            f"WR={overall_wr*100:.0f}% | " + " | ".join(changes[:5])
        )

    # ── Evolution (every 50 trades) ───────────────────────────────

    def evolve(self):
        trades = self.data["trades"][-self.EVOLVE_EVERY:]
        if len(trades) < 20:
            return

        self.data["stats"]["last_evolution_at"] = self.data["stats"]["total"]

        # Winrate per setup
        by_type: dict = {}
        for t in trades:
            typ = t.get("entry_type", "OB")
            by_type.setdefault(typ, {"w": 0, "l": 0})
            if t["result"] == "win":
                by_type[typ]["w"] += 1
            elif t["result"] == "loss":
                by_type[typ]["l"] += 1

        ranked = sorted(
            [(typ, c["w"] / (c["w"] + c["l"]))
             for typ, c in by_type.items() if (c["w"] + c["l"]) >= 5],
            key=lambda x: x[1],
        )
        if not ranked:
            return

        n = len(ranked)
        worst_20pct = ranked[:max(1, n // 5)]
        best_30pct  = ranked[max(0, int(n * 0.7)):]

        weights = self.data["adapted"]["setup_weights"]
        disabled = self.data["adapted"]["disabled"]
        msgs: list[str] = []

        for typ, wr in worst_20pct:
            if typ not in disabled:
                self._propose_or_apply(
                    f"disabled.{typ}", None, None,
                    f"evolve: WR={wr*100:.0f}% in worst 20% over {self.EVOLVE_EVERY} trades"
                )
                msgs.append(f"PRUNE {typ} WR={wr*100:.0f}%")

        for typ, wr in best_30pct:
            old_w = weights.get(typ, 1.0)
            new_w = round(min(old_w * 1.5, self.MAX_WEIGHT), 2)
            if abs(new_w - old_w) >= 0.01:
                self._propose_or_apply(
                    f"setup_weights.{typ}", old_w, new_w,
                    f"evolve: WR={wr*100:.0f}% in best 30% over {self.EVOLVE_EVERY} trades"
                )
                msgs.append(f"BOOST {typ} WR={wr*100:.0f}% w→{new_w:.2f}")
            if typ in disabled:
                self._propose_or_apply(
                    f"disabled.{typ}", None, new_w,
                    f"evolve: WR={wr*100:.0f}% promoted to best 30%, re-enable"
                )
                msgs.append(f"RE-ENABLE {typ} WR={wr*100:.0f}%")

        logger.info(f"[SelfLearner] Evolution #{self.data['stats']['total']} | " + " | ".join(msgs))

    # ── Helpers ───────────────────────────────────────────────────

    @staticmethod
    def _extract_type(label: str) -> str:
        """'H4_OB' → 'OB', 'M15_FVG' → 'FVG', 'H1_BSL_sweep' → 'BSL'"""
        parts = label.split("_")
        for p in parts[1:]:
            p = p.upper()
            if p in _DEFAULT_WEIGHTS or p in (
                "BSL", "SSL", "EQH", "EQL", "PDH", "PDL",
                "MB", "RB", "BPR", "IDM",
            ):
                return p
        return parts[-1].upper() if len(parts) > 1 else label.upper()
