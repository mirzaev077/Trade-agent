"""
SessionGuard — Kill Zone and session-aware trading filter.

RULE: Only trade during high-probability session windows.
Dead hours = no new orders placed.

Windows (UTC):
  Asia KZ:       00:00 – 03:00  (silver bullet 02:00–04:00)
  London Pre:    06:00 – 08:00  (prep only — HTF setups allowed)
  London KZ:     08:00 – 11:00  ★ BEST
  Silver Bullet: 10:00 – 11:00  ★★ HIGHEST
  NY KZ:         13:00 – 16:00  ★ BEST
  Silver Bullet: 14:00 – 15:00  ★★ HIGHEST
  Dead zone:     22:00 – 00:00  (market close — NO ORDERS)
  NY tail:       16:00 – 22:00  (allowed with reduced quality)
"""
from dataclasses import dataclass
from datetime import datetime  # noqa: F401  # type hint uchun saqlanadi

from ..core.clock import get_clock


@dataclass
class SessionGuardResult:
    allowed: bool
    session: str              # current session name
    in_kz: bool
    in_silver_bullet: bool
    quality_mult: float       # 1.0 = normal, 1.3 = SB, 0.7 = off-hours
    min_confluence: int       # minimum confluence score required THIS session
    reason: str


class SessionGuard:
    """
    Enforces session-based trading rules.

    Quality multipliers affect confluence minimum requirement:
      Silver Bullet: min_confluence -= 1 (easier to approve)
      London/NY KZ:  min_confluence = default
      Asia KZ:       min_confluence += 1 (harder — more volatile/thin)
      London Pre:    min_confluence += 2 (only top setups)
      Dead zone:     blocked entirely
      NY tail:       min_confluence += 1
    """

    # (start_hour, start_min, end_hour, end_min)
    _SESSIONS = {
        "asia_kz":       (0,  0,  3,  0),
        "silver_bullet_asia": (2, 0, 4, 0),
        "london_pre":    (6,  0,  8,  0),
        "london_kz":     (8,  0, 11,  0),
        "silver_bullet_london": (10, 0, 11, 0),
        "overlap_kz":    (11, 0, 13,  0),
        "ny_kz":         (13, 0, 16,  0),
        "silver_bullet_ny": (14, 0, 15, 0),
        "ny_tail":       (16, 0, 22,  0),
        "dead_zone":     (22, 0, 24,  0),
    }

    def check(self, now: datetime = None, base_min_confluence: int = 10) -> SessionGuardResult:
        if now is None:
            now = get_clock().now()

        h, m = now.hour, now.minute
        t = now.hour * 60 + now.minute  # minutes since midnight

        # ── Dead zone: 22:00–24:00 ──────────────────────────────────
        if t >= 22 * 60:
            return SessionGuardResult(
                allowed=False, session="dead_zone",
                in_kz=False, in_silver_bullet=False,
                quality_mult=0.0, min_confluence=99,
                reason="Dead zone (22:00–00:00) — market closing",
            )

        # ── Silver Bullet windows (highest quality) ─────────────────
        in_sb = (
            (10 * 60 <= t < 11 * 60) or   # London SB
            (14 * 60 <= t < 15 * 60) or   # NY SB
            (2  * 60 <= t <  4 * 60)       # Asia SB
        )

        # ── Kill Zone windows ────────────────────────────────────────
        in_london_kz = (8 * 60 <= t < 11 * 60)
        in_ny_kz     = (13 * 60 <= t < 16 * 60)
        in_asia_kz   = (0 * 60 <= t < 3 * 60)
        in_kz        = in_london_kz or in_ny_kz or in_asia_kz

        # ── Session name ────────────────────────────────────────────
        if in_silver_bullet_london := (10 * 60 <= t < 11 * 60):
            session = "silver_bullet_london"
        elif in_silver_bullet_ny := (14 * 60 <= t < 15 * 60):
            session = "silver_bullet_ny"
        elif in_silver_bullet_asia := (2 * 60 <= t < 4 * 60):
            session = "silver_bullet_asia"
        elif in_london_kz:
            session = "london_kz"
        elif in_ny_kz:
            session = "ny_kz"
        elif in_asia_kz:
            session = "asia_kz"
        elif 6 * 60 <= t < 8 * 60:
            session = "london_pre"
        elif 11 * 60 <= t < 13 * 60:
            session = "overlap_kz"
        elif 16 * 60 <= t < 22 * 60:
            session = "ny_tail"
        else:
            session = "other"

        # ── Quality multiplier + min confluence ──────────────────────
        if in_sb:
            quality_mult   = 1.3
            min_conf_delta = -1    # Silver Bullet = easier approval
        elif in_london_kz or in_ny_kz:
            quality_mult   = 1.0
            min_conf_delta = 0
        elif in_asia_kz:
            quality_mult   = 0.9
            min_conf_delta = +1    # Asia = harder
        elif 6 * 60 <= t < 8 * 60:   # London Pre
            quality_mult   = 0.8
            min_conf_delta = +2    # only top setups
        elif 11 * 60 <= t < 13 * 60:  # Overlap pre-NY
            quality_mult   = 1.0
            min_conf_delta = 0
        elif 16 * 60 <= t < 22 * 60:  # NY tail
            quality_mult   = 0.85
            min_conf_delta = +1
        else:
            quality_mult   = 0.7
            min_conf_delta = +2

        min_conf = max(6, base_min_confluence + min_conf_delta)
        allowed  = quality_mult > 0.0

        reason = (
            f"[{session}] "
            f"KZ={'✓' if in_kz else '✗'} "
            f"SB={'✓' if in_sb else '✗'} "
            f"qmult={quality_mult:.1f} min_conf={min_conf}"
        )

        return SessionGuardResult(
            allowed=allowed,
            session=session,
            in_kz=in_kz,
            in_silver_bullet=in_sb,
            quality_mult=quality_mult,
            min_confluence=min_conf,
            reason=reason,
        )

    def is_silver_bullet(self, now: datetime = None) -> bool:
        if now is None:
            now = get_clock().now()
        t = now.hour * 60 + now.minute
        return (10 * 60 <= t < 11 * 60) or (14 * 60 <= t < 15 * 60) or (2 * 60 <= t < 4 * 60)

    def is_kill_zone(self, now: datetime = None) -> bool:
        if now is None:
            now = get_clock().now()
        t = now.hour * 60 + now.minute
        return (0 <= t < 3 * 60) or (8 * 60 <= t < 11 * 60) or (13 * 60 <= t < 16 * 60)
