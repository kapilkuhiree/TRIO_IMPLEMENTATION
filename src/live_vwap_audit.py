"""Audit every live options entry against the corrected ORB + true-VWAP rules.

Why this exists
---------------
The live ``orb`` / ``orb_retest`` sleeves still decide on the NIFTY **spot**
index, whose candles report ``Volume = 0``.  Their "VWAP" is therefore the
equal-weighted mean of ``(H+L+C)/3``, not a VWAP, and a breakout may be
entered repeatedly on the same leg.

The corrected rules (true VWAP, one entry per leg, 10:00-14:30 window) are
measured forward in ``src/shadow_orb_vwap.py``.  This module answers a
different, narrower question, after the fact:

    for each live entry, what would the corrected rules have said?

It is deliberately **read-only**.  It never imports the runtime, never opens a
broker connection, never writes to the trade ledger, and nothing in the live
path imports it.  Output goes to its own append-only JSONL file.

Classification
--------------
Each live entry is labelled with exactly one primary class:

``survives``
    The entry is itself the first corrected entry for that direction: two
    consecutive 5-minute closes beyond the opening range, on the correct side
    of the true VWAP, inside the 10:00-14:30 window.
``duplicate_entry``
    A corrected entry already existed earlier in the same direction and price
    never re-armed (no close back inside the opening range).  This is the
    repeat-entry defect.
``second_setup``
    Same as ``duplicate_entry``, but price did re-arm.  The corrected engine
    still skips it because it allows one trade per session, so this measures
    the cost of that cap.
``vwap_blocked``
    The opening range confirmed, but the close was on the wrong side of the
    true VWAP.
``basis_mismatch``
    The opening range did **not** confirm on futures, but it did on spot:
    the live signal came from a different instrument.
``no_confirmation``
    No two-close confirmation on either series.
``outside_window``
    Entry before 10:00 or after 14:30.
``data_issue``
    No futures session, or volume too sparse for a true VWAP.

Fields are recorded alongside the class (``orb_confirms``, ``vwap_ok``,
``spot_confirms``, ``repeat_of``, ``corrected_entry_time``) so a duplicate
can still be inspected for a hidden basis or VWAP problem.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.shadow_orb_vwap import (
    ENTRY_CUTOFF,
    ENTRY_FLOOR,
    IST,
    MIN_VOLUME_COVERAGE,
    add_indicators,
    compute_session_vwap,
    consecutive_closes,
    group_sessions,
    normalise_bars,
    opening_range,
    vwap_path,
)

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CONFIRM_CLOSES = 2

CLASSES = (
    "survives",
    "late_entry",
    "duplicate_entry",
    "second_setup",
    "vwap_blocked",
    "basis_mismatch",
    "no_confirmation",
    "outside_window",
    "data_issue",
)


# ---------------------------------------------------------------------------
# Live trade-log reader
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveEntry:
    day: str
    stamp: datetime
    strategy: str
    direction: str          # "long" | "short"
    position_id: str
    entry_premium: Optional[float] = None
    kind: str = ""
    strike: Optional[float] = None
    underlying: str = "NIFTY"


def _underlying_of(row: Mapping[str, Any]) -> str:
    """Best-effort underlying, from the explicit fields or the symbol text."""
    for key in ("option_underlying", "signal_underlying"):
        value = str(row.get(key) or "").strip().upper()
        if value:
            return value
    text = " ".join(
        str(row.get(key) or "")
        for key in ("trading_symbol", "symbol", "position_id")
    ).upper()
    for name in ("BANKNIFTY", "FINNIFTY", "NIFTY"):
        if name in text:
            return name
    return ""


def _as_ist(value: Any) -> Optional[datetime]:
    try:
        if isinstance(value, datetime):
            stamp = value
        else:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=IST)
    return stamp.astimezone(IST)


def _live_direction(row: Mapping[str, Any]) -> Optional[str]:
    raw = str(row.get("direction") or "").strip().lower()
    if raw in ("bullish", "long", "buy"):
        return "long"
    if raw in ("bearish", "short", "sell"):
        return "short"
    action = str(row.get("signal_action") or "").strip().upper()
    if action == "BUY":
        return "long"
    if action == "SELL":
        return "short"
    return None


def load_live_entries(path: Optional[Path] = None,
                      *,
                      underlying: Optional[str] = "NIFTY") -> List[LiveEntry]:
    """Return one ``LiveEntry`` per live options position, oldest first.

    Only entries for ``underlying`` are returned: a BANKNIFTY position audited
    against NIFTY futures levels is meaningless.  Rows whose underlying cannot
    be determined are kept so older logs still audit.

    The trade log writes one ``order`` event per position.  Restarts can repeat
    an event exactly, so rows are deduplicated on ``(position_id, timestamp)``.
    The key deliberately includes the timestamp: the live book can re-enter the
    same contract later in the session, and that second entry must survive.
    """
    path = Path(path or default_trade_log_path())
    if not path.exists():
        return []
    wanted = (underlying or "").strip().upper()
    seen: Dict[Tuple[str, str], LiveEntry] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(row.get("event")) != "order":
            continue
        if str(row.get("asset")) != "options":
            continue
        position_id = str(row.get("position_id") or "").strip()
        stamp = _as_ist(row.get("ts"))
        direction = _live_direction(row)
        if not position_id or stamp is None or direction is None:
            continue
        found = _underlying_of(row)
        if wanted and found and found != wanted:
            continue
        key = (position_id, stamp.isoformat())
        if key in seen:
            continue
        premium = row.get("entry_premium")
        strike = row.get("strike")
        seen[key] = LiveEntry(
            day=stamp.date().isoformat(),
            stamp=stamp,
            strategy=str(row.get("strategy") or row.get("setup") or "?"),
            direction=direction,
            position_id=position_id,
            entry_premium=float(premium) if premium is not None else None,
            kind=str(row.get("kind") or ""),
            strike=float(strike) if strike is not None else None,
            underlying=found or wanted or "",
        )
    return sorted(seen.values(), key=lambda item: item.stamp)


def load_live_pnl(entries: Sequence[LiveEntry],
                  path: Optional[Path] = None) -> Dict[str, float]:
    """Pair each entry with the next exit for the same position.

    Keyed by ``entry_ts``.  A contract can be entered more than once in a day,
    so the first exit is *not* necessarily this entry's exit.
    """
    path = Path(path or default_trade_log_path())
    exits: List[Tuple[datetime, str, float]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(row.get("event")) != "exit":
                continue
            if str(row.get("asset")) != "options":
                continue
            stamp = _as_ist(row.get("ts"))
            position_id = str(row.get("position_id") or "").strip()
            if stamp is None or not position_id:
                continue
            try:
                value = float(row.get("pnl") or 0.0)
            except (TypeError, ValueError):
                value = 0.0
            exits.append((stamp, position_id, value))
    exits.sort(key=lambda item: item[0])

    pnl: Dict[str, float] = {}
    used: set = set()
    for entry in sorted(entries, key=lambda item: item.stamp):
        for index, (stamp, position_id, value) in enumerate(exits):
            if index in used:
                continue
            if position_id != entry.position_id:
                continue
            if stamp < entry.stamp:
                continue
            used.add(index)
            pnl[entry.stamp.isoformat()] = value
            break
    return pnl


# ---------------------------------------------------------------------------
# Live runtime state reader
# ---------------------------------------------------------------------------
#
# The Zerodha live runtime journals closed legs in its own state file
# (``%LOCALAPPDATA%\TRIO\options_intraday\live_<date>.json``) and never writes
# them to ``output/trade_log.jsonl``.  Older state files carry no entry
# timestamp, so it is recovered from the runtime's ``ENTRY BUY`` log line.

_ENTRY_LINE = re.compile(
    r"^\[(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]"
    r".*?\bENTRY BUY\b.*?@\s*(?P<premium>\d+(?:\.\d+)?).*?"
    r"\|\s*(?P<position>[A-Za-z0-9_]+)\s*$"
)


def default_live_state_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "TRIO" / "options_intraday"


def default_live_state_path(day: str) -> Path:
    return default_live_state_dir() / f"live_{day}.json"


def find_runtime_logs(day: str, *,
                      root: Optional[Path] = None) -> List[Path]:
    """Runtime logs for ``day`` (the live runner redirects stdout/stderr)."""
    root = Path(root or ROOT)
    compact = day.replace("-", "")
    found: List[Path] = []
    for directory in (root, root / "output"):
        if not directory.is_dir():
            continue
        for pattern in (f"*{compact}*.log", f"*{day}*.log"):
            for path in sorted(directory.glob(pattern)):
                if path not in found:
                    found.append(path)
    return found


def _read_log_text(path: Path) -> str:
    """PowerShell redirection writes UTF-16LE; other runs write UTF-8."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return ""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")
    head = raw[:200]
    if head and head.count(b"\x00") > len(head) // 4:
        return raw.decode("utf-16-le", errors="replace")
    return raw.decode("utf-8", errors="replace")


def _iter_log_records(text: str) -> Iterable[str]:
    """Join PowerShell-wrapped continuations back into one log record.

    Redirecting a Python logger through PowerShell wraps long lines at the
    console width.  The wrap keeps the trailing space, so concatenating the
    physical lines restores the record exactly.
    """
    buffer: Optional[str] = None
    for raw in text.splitlines():
        if re.match(r"^\s*\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", raw):
            if buffer:
                yield buffer
            buffer = raw
        elif buffer is not None and raw.strip():
            buffer = f"{buffer}{raw}"
    if buffer:
        yield buffer


def load_entry_times_from_logs(
        paths: Iterable[Path]) -> Dict[Tuple[str, float], datetime]:
    """Map ``(position_id, entry_premium)`` to the logged entry time.

    Only used for state files written before the runtime started recording
    ``entry_ts``.  The log line is authoritative: it is written at the fill.
    """
    stamps: Dict[Tuple[str, float], datetime] = {}
    for path in paths:
        text = _read_log_text(path)
        for line in _iter_log_records(text):
            match = _ENTRY_LINE.match(line)
            if not match:
                continue
            try:
                stamp = datetime.strptime(
                    match.group("stamp"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
                key = (match.group("position"),
                       round(float(match.group("premium")), 2))
            except (TypeError, ValueError):
                continue
            current = stamps.get(key)
            if current is None or stamp < current:
                stamps[key] = stamp
    return stamps


def _intent_entry_time(state: Mapping[str, Any],
                       position_id: str,
                       closed_at: Optional[datetime]) -> Optional[datetime]:
    """Entry time from the surviving intent, when it cannot be the wrong leg."""
    candidates: List[datetime] = []
    for intent in (state.get("intents") or {}).values():
        if not isinstance(intent, Mapping):
            continue
        if str(intent.get("position_id") or "") != position_id:
            continue
        stamp = _as_ist(intent.get("created_at"))
        if stamp is None:
            continue
        if closed_at is not None and stamp > closed_at:
            continue
        candidates.append(stamp)
    return min(candidates) if candidates else None


def load_state_entries(
    path: Optional[Path] = None,
    *,
    underlying: Optional[str] = "NIFTY",
    entry_stamps: Optional[Mapping[Tuple[str, float], datetime]] = None,
) -> Tuple[List[LiveEntry], Dict[str, float], List[Dict[str, Any]]]:
    """Read closed live positions from a runtime state file.

    Returns ``(entries, pnl_by_entry, unresolved)``.  ``unresolved`` lists the
    closed legs whose entry time could not be established; auditing them at
    their exit time would be a lie, so they are left out and reported.
    """
    path = Path(path or default_live_state_path(
        datetime.now(IST).date().isoformat()))
    if not path.exists():
        return [], {}, []
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], {}, []
    if not isinstance(state, Mapping):
        return [], {}, []

    wanted = (underlying or "").strip().upper()
    entry_stamps = entry_stamps or {}
    seen: Dict[Tuple[str, str], LiveEntry] = {}
    pnl: Dict[str, float] = {}
    unresolved: List[Dict[str, Any]] = []

    for row in state.get("closed") or []:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("asset") or "options") != "options":
            continue
        position_id = str(row.get("position_id") or "").strip()
        direction = _live_direction(row)
        found = _underlying_of(row)
        if not position_id or direction is None:
            continue
        if wanted and found and found != wanted:
            continue

        premium_raw = row.get("entry_premium")
        try:
            premium = (float(premium_raw)
                       if premium_raw is not None else None)
        except (TypeError, ValueError):
            premium = None
        closed_at = _as_ist(row.get("closed_at"))

        stamp = (_as_ist(row.get("entry_ts"))
                 or _as_ist(row.get("opened_at")))
        if stamp is None and premium is not None:
            stamp = entry_stamps.get((position_id, round(premium, 2)))
        if stamp is None:
            stamp = _intent_entry_time(state, position_id, closed_at)
        if stamp is None:
            unresolved.append({
                "position_id": position_id,
                "entry_premium": premium,
                "closed_at": None if closed_at is None else closed_at.isoformat(),
            })
            continue

        key = (position_id, stamp.isoformat())
        if key in seen:
            continue
        strike_raw = row.get("strike")
        try:
            strike = float(strike_raw) if strike_raw is not None else None
        except (TypeError, ValueError):
            strike = None
        seen[key] = LiveEntry(
            day=stamp.date().isoformat(),
            stamp=stamp,
            strategy=str(row.get("strategy") or row.get("setup") or "?"),
            direction=direction,
            position_id=position_id,
            entry_premium=premium,
            kind=str(row.get("kind") or ""),
            strike=strike,
            underlying=found or wanted or "",
        )
        if row.get("pnl") is not None:
            try:
                pnl[stamp.isoformat()] = float(row.get("pnl"))
            except (TypeError, ValueError):
                pass

    entries = sorted(seen.values(), key=lambda item: item.stamp)
    return entries, pnl, unresolved


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def _clock_minutes(stamp: datetime) -> int:
    clock = stamp.astimezone(IST)
    return clock.hour * 60 + clock.minute


def _minutes(clock: time) -> int:
    return clock.hour * 60 + clock.minute


def _bar_index(session: Sequence[Mapping[str, Any]],
               stamp: datetime) -> Optional[int]:
    """Index of the last bar that closed at or before ``stamp``."""
    target = stamp.astimezone(IST)
    found: Optional[int] = None
    for index, bar in enumerate(session):
        if bar["timestamp"] <= target:
            found = index
        else:
            break
    return found


def _first_qualifying(session: Sequence[Mapping[str, Any]],
                      path: Sequence[Optional[float]],
                      or_high: float,
                      or_low: float,
                      *,
                      confirm_closes: int,
                      entry_floor: time,
                      entry_cutoff: time) -> Dict[str, int]:
    """First corrected entry bar per direction inside the entry window."""
    floor = _minutes(entry_floor)
    cutoff = _minutes(entry_cutoff)
    first: Dict[str, int] = {}
    for index, bar in enumerate(session):
        clock = _clock_minutes(bar["timestamp"])
        if clock < floor:
            continue
        if clock > cutoff:
            break
        close = float(bar["close"])
        vwap = path[index]
        if vwap is None:
            continue
        for direction, above, level in (("long", True, or_high),
                                        ("short", False, or_low)):
            if direction in first:
                continue
            if consecutive_closes(session, index, level, above) < confirm_closes:
                continue
            if above and not close > vwap:
                continue
            if not above and not close < vwap:
                continue
            first[direction] = index
    return first


def _rearmed(session: Sequence[Mapping[str, Any]],
             start: int,
             end: int,
             or_high: float,
             or_low: float) -> bool:
    """True when price closed back inside the opening range between two bars."""
    for index in range(start + 1, end):
        close = float(session[index]["close"])
        if or_low < close < or_high:
            return True
    return False


def _spot_confirms(spot_sessions: Mapping[str, List[Dict[str, Any]]],
                   day: str,
                   stamp: datetime,
                   direction: str,
                   *,
                   confirm_closes: int) -> Optional[bool]:
    session = spot_sessions.get(day)
    if not session:
        return None
    opening = opening_range(session)
    if opening is None:
        return None
    or_high, or_low = opening
    index = _bar_index(session, stamp)
    if index is None:
        return None
    if direction == "long":
        return consecutive_closes(session, index, or_high, True) >= confirm_closes
    return consecutive_closes(session, index, or_low, False) >= confirm_closes


def audit_session(
    entries: Sequence[LiveEntry],
    futures_bars: Sequence[Mapping[str, Any]],
    *,
    spot_bars: Optional[Sequence[Mapping[str, Any]]] = None,
    confirm_closes: int = DEFAULT_CONFIRM_CLOSES,
    entry_floor: time = ENTRY_FLOOR,
    entry_cutoff: time = ENTRY_CUTOFF,
    pnl_by_entry: Optional[Mapping[str, float]] = None,
) -> List[Dict[str, Any]]:
    """Classify every live entry for one session.

    ``futures_bars`` and ``spot_bars`` may span many sessions; only the day of
    each entry is used.
    """
    if not entries:
        return []
    days = {entry.day for entry in entries}
    if len(days) != 1:
        raise ValueError(f"audit_session expects one session, got {sorted(days)}")
    day = entries[0].day
    sessions = group_sessions(normalise_bars(futures_bars))
    spot_sessions = (group_sessions(normalise_bars(spot_bars))
                     if spot_bars else {})
    session = add_indicators(sessions.get(day, []))
    floor = _minutes(entry_floor)
    cutoff = _minutes(entry_cutoff)

    opening = opening_range(session) if session else None
    path, path_reason = vwap_path(session) if session else ([], "no_session_bars")
    first = (_first_qualifying(
        session, path,
        opening[0], opening[1],
        confirm_closes=confirm_closes,
        entry_floor=entry_floor,
        entry_cutoff=entry_cutoff,
    ) if opening is not None else {})

    # Per-direction leg state.  A leg starts at the first corrected entry and
    # restarts when price closes back inside the opening range and breaks out
    # again.
    leg_start_index: Dict[str, int] = dict(first)
    leg_has_live: Dict[str, bool] = {direction: False for direction in first}
    leg_first_live: Dict[str, str] = {}

    records: List[Dict[str, Any]] = []
    for entry in entries:
        record: Dict[str, Any] = {
            "kind": "live_vwap_audit",
            "day": day,
            "position_id": entry.position_id,
            "strategy": entry.strategy,
            "direction": entry.direction,
            "kind_opt": entry.kind,
            "underlying": entry.underlying,
            "strike": entry.strike,
            "entry_premium": entry.entry_premium,
            "entry_time": entry.stamp.strftime("%H:%M"),
            "entry_ts": entry.stamp.isoformat(),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "classification": "data_issue",
            "detail": "",
        }
        if pnl_by_entry is not None and entry.stamp.isoformat() in pnl_by_entry:
            record["pnl_inr"] = round(
                float(pnl_by_entry[entry.stamp.isoformat()]), 2
            )

        if not session or opening is None:
            record["detail"] = "no_session_bars" if not session else "incomplete_opening_range"
            records.append(record)
            continue

        or_high, or_low = opening
        index = _bar_index(session, entry.stamp)
        if index is None:
            record["detail"] = "no_bar_at_entry"
            records.append(record)
            continue

        bar = session[index]
        clock = _clock_minutes(bar["timestamp"])
        vwap = path[index] if index < len(path) else None
        close = float(bar["close"])
        volume = compute_session_vwap(session[: index + 1])
        record.update({
            "or_high": round(or_high, 2),
            "or_low": round(or_low, 2),
            "bar_time": bar["timestamp"].strftime("%H:%M"),
            "futures_close": round(close, 2),
            "vwap_at_entry": None if vwap is None else round(float(vwap), 2),
            "volume_coverage": round(volume.coverage, 3),
        })

        if vwap is None:
            record["classification"] = "data_issue"
            record["detail"] = path_reason or volume.reason or "vwap_unavailable"
            records.append(record)
            continue

        in_window = floor <= clock <= cutoff
        orb_confirms = (
            consecutive_closes(session, index, or_high, True) >= confirm_closes
            if entry.direction == "long"
            else consecutive_closes(session, index, or_low, False) >= confirm_closes
        )
        vwap_ok = close > vwap if entry.direction == "long" else close < vwap
        spot_ok = _spot_confirms(
            spot_sessions, day, entry.stamp, entry.direction,
            confirm_closes=confirm_closes,
        )

        anchor = first.get(entry.direction)
        anchor_time = (session[anchor]["timestamp"].strftime("%H:%M")
                       if anchor is not None else None)
        record.update({
            "in_window": in_window,
            "orb_confirms": bool(orb_confirms),
            "vwap_ok": bool(vwap_ok),
            "spot_confirms": spot_ok,
            "corrected_entry_time": anchor_time,
        })
        if anchor is not None and anchor < index:
            record["delay_minutes"] = (
                clock - _clock_minutes(session[anchor]["timestamp"])
            )

        leg_start = leg_start_index.get(entry.direction)
        rearmed = (leg_start is not None and leg_start < index and
                   _rearmed(session, leg_start, index, or_high, or_low))

        if not in_window:
            classification = "outside_window"
        elif anchor is not None and anchor <= index:
            if rearmed and orb_confirms:
                classification = "second_setup"
                leg_start_index[entry.direction] = index
                leg_first_live[entry.direction] = record["entry_time"]
            elif not leg_has_live.get(entry.direction):
                classification = ("survives" if anchor == index
                                  else "late_entry")
                leg_has_live[entry.direction] = True
                leg_first_live[entry.direction] = record["entry_time"]
            else:
                classification = "duplicate_entry"
                record["repeat_of"] = (
                    f"live@{leg_first_live[entry.direction]}"
                )
        elif orb_confirms and not vwap_ok:
            classification = "vwap_blocked"
        elif not orb_confirms and spot_ok:
            classification = "basis_mismatch"
        else:
            classification = "no_confirmation"

        record["classification"] = classification
        record["detail"] = _detail(classification, record)
        records.append(record)
    return records


def _detail(classification: str,
            record: Mapping[str, Any]) -> str:
    if classification == "survives":
        return "live entry is the corrected first entry"
    if classification == "late_entry":
        delay = record.get("delay_minutes")
        return (f"first live entry on this leg, {delay} min after the corrected "
                "first entry" if delay else
                "first live entry on this leg")
    if classification == "duplicate_entry":
        return f"repeat of the {record.get('repeat_of')} entry"
    if classification == "second_setup":
        return (f"re-armed leg after {record.get('corrected_entry_time')}; "
                "one-trade-per-session rule would skip it")
    if classification == "vwap_blocked":
        return "opening range confirmed, close on the wrong side of true VWAP"
    if classification == "basis_mismatch":
        return "futures range not confirmed; spot range confirmed"
    if classification == "no_confirmation":
        return "no two-close confirmation on futures or spot"
    if classification == "outside_window":
        return "outside the 10:00-14:30 corrected entry window"
    return str(record.get("detail") or "data issue")


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def default_trade_log_path() -> Path:
    return ROOT / "output" / "trade_log.jsonl"


def default_ledger_path() -> Path:
    return ROOT / "output" / "live_vwap_audit.jsonl"


def _key(record: Mapping[str, Any]) -> Tuple[str, str, str]:
    return (
        str(record.get("day")),
        str(record.get("position_id")),
        str(record.get("entry_ts") or record.get("entry_time")),
    )


def read_records(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    path = Path(path or default_ledger_path())
    if not path.exists():
        return []
    records: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def append_record(record: Mapping[str, Any],
                  path: Optional[Path] = None) -> bool:
    """Append unless this exact entry is already in the ledger.

    The key is ``(day, position_id, entry_ts)`` — a contract re-entered later
    in the day is a different entry, not a duplicate record.
    """
    path = Path(path or default_ledger_path())
    existing = {_key(item) for item in read_records(path)}
    if _key(record) in existing:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(record), default=str) + "\n")
    return True


def summarise(records: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    rows = list(records)
    counts = {name: 0 for name in CLASSES}
    for row in rows:
        name = str(row.get("classification") or "data_issue")
        counts[name] = counts.get(name, 0) + 1
    days = sorted({str(row.get("day")) for row in rows})
    pnl = sum(float(row.get("pnl_inr") or 0.0) for row in rows)
    basis_flags = sum(
        1 for row in rows
        if row.get("spot_confirms") is True and row.get("orb_confirms") is False
    )
    return {
        "days": len(days),
        "entries": len(rows),
        "counts": counts,
        "live_net_inr": round(pnl, 2),
        "days_list": days,
        "basis_flags": basis_flags,
    }


# ---------------------------------------------------------------------------
# Data adapters
# ---------------------------------------------------------------------------


def load_spot_bars(underlying: str = "NIFTY",
                   *,
                   timeframe: str = "5m",
                   period: str = "30d") -> List[Dict[str, Any]]:
    """Fetch the spot index series the live scanner actually decides on.

    Returns an empty list on any failure.  Spot volume is zero by design; it is
    only used for the opening range, never for the VWAP.
    """
    symbol = "^NSEI" if underlying.upper() == "NIFTY" else underlying
    try:
        from src.data_fetcher import fetch_market_data

        frame = fetch_market_data(symbol, timeframe, period).ohlcv
        rows = [
            {
                "date": index,
                "open": row["Open"],
                "high": row["High"],
                "low": row["Low"],
                "close": row["Close"],
                "volume": row.get("Volume", 0),
            }
            for index, row in frame.iterrows()
        ]
    except Exception:
        return []
    return normalise_bars(rows)


__all__ = [
    "CLASSES",
    "DEFAULT_CONFIRM_CLOSES",
    "LiveEntry",
    "append_record",
    "audit_session",
    "default_ledger_path",
    "default_live_state_dir",
    "default_live_state_path",
    "default_trade_log_path",
    "find_runtime_logs",
    "load_entry_times_from_logs",
    "load_live_entries",
    "load_live_pnl",
    "load_spot_bars",
    "load_state_entries",
    "read_records",
    "summarise",
]
