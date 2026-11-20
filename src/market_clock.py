"""
TRIO — Market Clock (Phase 2: single authoritative clock)
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

All session logic (what can enter, what must square off, what is closed)
reads ONE source: this module, which in turn reads config/market.yaml
(merged by load_config() under cfg["market"]), with an explicit
forward_test fallback. Nothing else should parse HH:MM strings directly.

Semantics (NSE, per spec):
  Before 09:20 : no new entries.
  09:20–15:00  : new entries allowed (entry_cutoff exclusive).
  15:00–15:10  : exits only (management-only, no new entries).
  >= 15:10     : force hard-flat square-off.
  15:30        : exchange closed.
  Invalid config : management-only mode; no new entries.

DISCLAIMER: Educational purposes only. Not financial advice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Set

from src.utils import get_logger, load_config

logger = get_logger("market_clock")

IST_OFFSET = timedelta(hours=5, minutes=30)
IST = timezone(IST_OFFSET)


def _mins(hhmm: str) -> int:
    h_s, m_s = str(hhmm).strip().split(":")
    h, m = int(h_s), int(m_s)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"out of range: {hhmm!r}")
    return h * 60 + m


def _resolve_dt(now=None) -> datetime:
    """Accept None/datetime/time -> IST-aware datetime today."""
    if now is None:
        return datetime.now(IST)
    if isinstance(now, datetime):
        return now.astimezone(IST) if now.tzinfo is not None else now.replace(tzinfo=IST)
    # datetime.time -> combine with today's date in IST
    today = datetime.now(IST).date()
    return datetime.combine(today, now)  # type: ignore


def _installed_market_raw() -> Dict[str, Any]:
    """Raw `market` block from load_config(), or {} if absent/invalid."""
    try:
        cfg = load_config()
        m = cfg.get("market", {})
        return dict(m) if isinstance(m, dict) else {}
    except Exception:
        return {}


@dataclass
class MarketClock:
    """Immutable per-session clock. `valid=False` means the config was bad
    and entries must be blocked (management-only). Exits still run."""
    timezone_name: str = "Asia/Kolkata"
    open: str = "09:15"
    entry_start: str = "09:20"
    entry_cutoff: str = "15:00"
    hard_flat: str = "15:10"
    close: str = "15:30"
    holidays: Set[str] = field(default_factory=set)
    # Per-date session overrides for exchange-declared special sessions (for
    # example a weekend/holiday trading day).  A key here makes that date a
    # trading day even at a weekend; the date's own
    # ``open``/``entry_start``/... replace the base window.
    special_sessions: Dict[str, Dict[str, str]] = field(default_factory=dict)
    valid: bool = True
    error: str = ""

    def _mins_of(self, attr: str) -> Optional[int]:
        try:
            return _mins(getattr(self, attr))
        except Exception:
            return None

    def _times_for(self, dt: datetime) -> Dict[str, str]:
        """Effective session times for ``dt``, honouring a special session."""
        times = {
            "open": self.open,
            "entry_start": self.entry_start,
            "entry_cutoff": self.entry_cutoff,
            "hard_flat": self.hard_flat,
            "close": self.close,
        }
        override = self.special_sessions.get(dt.strftime("%Y-%m-%d"))
        if isinstance(override, dict):
            for key in times:
                value = override.get(key)
                if value not in (None, ""):
                    times[key] = str(value)
        return times

    @staticmethod
    def _mins_for(times: Dict[str, str], key: str) -> Optional[int]:
        try:
            return _mins(times[key])
        except Exception:
            return None

    def is_trading_day(self, now=None) -> bool:
        if not self.valid:
            return False
        if isinstance(now, dtime):
            # Time-only callers (session tests, scan_once) are just asking
            # "is this minute inside the NSE window?" — ignore the calendar.
            return True
        dt = _resolve_dt(now)
        day = dt.strftime("%Y-%m-%d")
        # An explicit holiday always wins, even over a special session.
        if day in self.holidays:
            return False
        if day in self.special_sessions:
            return True
        if dt.weekday() >= 5:  # Sat/Sun
            return False
        return True

    def can_enter(self, now=None) -> bool:
        if not self.valid:
            return False
        if isinstance(now, dtime):
            m = now.hour * 60 + now.minute
            lo, hi = self._mins_of("entry_start"), self._mins_of("entry_cutoff")
            if lo is None or hi is None:
                return False
            return lo <= m < hi
        if not self.is_trading_day(now):
            return False
        dt = _resolve_dt(now)
        times = self._times_for(dt)
        lo = self._mins_for(times, "entry_start")
        hi = self._mins_for(times, "entry_cutoff")
        if lo is None or hi is None:
            return False
        return lo <= dt.hour * 60 + dt.minute < hi

    def is_exits_only(self, now=None) -> bool:
        if not self.valid:
            return False
        if isinstance(now, dtime):
            m = now.hour * 60 + now.minute
            lo, hi = self._mins_of("entry_cutoff"), self._mins_of("hard_flat")
            if lo is None or hi is None:
                return False
            return lo <= m < hi
        if not self.is_trading_day(now):
            return False
        dt = _resolve_dt(now)
        times = self._times_for(dt)
        lo = self._mins_for(times, "entry_cutoff")
        hi = self._mins_for(times, "hard_flat")
        if lo is None or hi is None:
            return False
        return lo <= dt.hour * 60 + dt.minute < hi

    def is_hard_flat(self, now=None) -> bool:
        if not self.valid:
            # invalid config => management-only, not forced flat
            return False
        if isinstance(now, dtime):
            hf = self._mins_of("hard_flat")
            if hf is None:
                return False
            return now.hour * 60 + now.minute >= hf
        if not self.is_trading_day(now):
            return False
        dt = _resolve_dt(now)
        hf = self._mins_for(self._times_for(dt), "hard_flat")
        if hf is None:
            return False
        return dt.hour * 60 + dt.minute >= hf

    def is_closed(self, now=None) -> bool:
        if not self.valid:
            return True
        if isinstance(now, dtime):
            cl = self._mins_of("close")
            if cl is None:
                return True
            return now.hour * 60 + now.minute >= cl
        if not self.is_trading_day(now):
            return True
        dt = _resolve_dt(now)
        cl = self._mins_for(self._times_for(dt), "close")
        if cl is None:
            return True
        return dt.hour * 60 + dt.minute >= cl

    def session_state(self, now=None) -> str:
        if not self.valid:
            return "invalid"
        if isinstance(now, dtime):
            m = now.hour * 60 + now.minute
            times = {key: getattr(self, key) for key in
                     ("entry_start", "entry_cutoff", "hard_flat", "close")}
        else:
            if not self.is_trading_day(now):
                return "closed"
            dt = _resolve_dt(now)
            m = dt.hour * 60 + dt.minute
            times = self._times_for(dt)
        es, ec, hf, cl = (self._mins_for(times, a) for a in
                          ("entry_start", "entry_cutoff", "hard_flat", "close"))
        if any(v is None for v in (es, ec, hf, cl)):
            return "invalid"
        if m < es:
            return "pre"
        if m < ec:
            return "open"
        if m < hf:
            return "exits-only"
        if m < cl:
            return "hard-flat"
        return "closed"


def market_clock_from_config(cfg: Optional[Dict[str, Any]] = None) -> MarketClock:
    """Build a MarketClock from a merged config dict.

    Reads ``cfg["market"]`` when that file is present; otherwise falls back
    to ``cfg["forward_test"]`` (legacy) — so the test suite can keep patching
    ``load_config() -> {"forward_test": {...}}`` and the clock still follows.

    Any parse error produces valid=False (management-only).
    """
    if cfg is None:
        try:
            from src.utils import load_config as _lc
            cfg = _lc()
        except Exception as exc:
            return MarketClock(valid=False, error=str(exc))

    mkt = cfg.get("market") if isinstance(cfg.get("market"), dict) else None
    if mkt and mkt.get("entry_start"):
        raw = dict(mkt)
        source = "market"
    else:
        raw = dict(cfg.get("forward_test") or {})
        source = "forward_test"
        raw = {
            "timezone": raw.get("timezone", "Asia/Kolkata"),
            "open": raw.get("open", "09:15"),
            "entry_start": raw.get("session_start", "09:20"),
            "entry_cutoff": raw.get("session_end", "15:00"),
            # graceful degradation: legacy only had one end; use it for both
            # cutoff and flat.
            "hard_flat": raw.get("hard_flat", raw.get("session_end", "15:10")),
            "close": raw.get("close", "15:30"),
            "holidays": raw.get("holidays", []),
            "_source": source,
        }

    def _as_str(k: str, default: str) -> str:
        v = raw.get(k, default)
        return str(v).strip() or default

    hols = raw.get("holidays", [])
    hol_set: Set[str] = set(str(x).strip() for x in (hols or []) if str(x).strip())

    raw_special = raw.get("special_sessions", {}) or {}
    special: Dict[str, Dict[str, str]] = {}
    if isinstance(raw_special, dict):
        for day, values in raw_special.items():
            key = str(day).strip()
            if not key or not isinstance(values, dict):
                continue
            special[key] = {
                str(field): str(value) for field, value in values.items()
                if value not in (None, "")
            }

    clk = MarketClock(
        timezone_name=_as_str("timezone", "Asia/Kolkata"),
        open=_as_str("open", "09:15"),
        entry_start=_as_str("entry_start", "09:20"),
        entry_cutoff=_as_str("entry_cutoff", "15:00"),
        hard_flat=_as_str("hard_flat", "15:10"),
        close=_as_str("close", "15:30"),
        holidays=hol_set,
        special_sessions=special,
    )
    # 00:00 sentinel (used by tests to mean "always hard-flat") — don't
    # reject on ordering; the live installed market.yaml never uses it.
    sentinel_00 = (clk.entry_cutoff == "00:00" or clk.hard_flat == "00:00")
    # validate
    if clk.timezone_name != "Asia/Kolkata":
        # Wrong TZ (IST is the only valid one) -> management-only.
        clk.valid = False
        clk.error = f"wrong timezone: {clk.timezone_name!r} (want Asia/Kolkata)"
        return clk
    try:
        for attr in ("open", "entry_start", "entry_cutoff", "hard_flat", "close"):
            _mins(getattr(clk, attr))
        if not sentinel_00 and not (_mins(clk.open) <= _mins(clk.entry_start) < _mins(clk.entry_cutoff)
                <= _mins(clk.hard_flat) <= _mins(clk.close)):
            raise ValueError("not ordered: open <= entry_start < entry_cutoff <= hard_flat <= close")
    except Exception as exc:
        clk.valid = False
        clk.error = str(exc)

    if clk.valid:
        for day, values in special.items():
            try:
                datetime.strptime(day, "%Y-%m-%d")
                merged = {
                    "open": clk.open,
                    "entry_start": clk.entry_start,
                    "entry_cutoff": clk.entry_cutoff,
                    "hard_flat": clk.hard_flat,
                    "close": clk.close,
                }
                merged.update(values)
                ordered = (_mins(merged["open"]) <= _mins(merged["entry_start"]) <
                           _mins(merged["entry_cutoff"]) <= _mins(merged["hard_flat"]) <=
                           _mins(merged["close"]))
                if not ordered:
                    raise ValueError(
                        "not ordered: open <= entry_start < entry_cutoff "
                        "<= hard_flat <= close")
            except Exception as exc:
                clk.valid = False
                clk.error = f"special session {day!r}: {exc}"
                break
    return clk


def get_market_clock() -> MarketClock:
    """Return the clock for the installed config (cached per call via load_config's cache)."""
    return market_clock_from_config()
