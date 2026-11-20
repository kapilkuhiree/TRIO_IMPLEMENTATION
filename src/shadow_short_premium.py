"""Shadow-only defined-risk short-premium sleeve (bull put spread).

Why this exists
---------------
The long-premium ORB book already wins ~69% of real trades, but its
break-even win rate is ~68%: the profit-lock ladder cuts winners at +10-20%
of premium while a stop costs -20% plus slippage.  Win rate and payoff move
in opposite directions, so the product stays near zero (see
HIGH_WINRATE_RESEARCH.md and the live ledger).

A short-premium spread is the only structure whose win rate is high
*structurally*: most spreads expire worthless.  The question is whether the
credit pays for the occasional full-width loss.  This module records what the
sleeve WOULD have done, every session, from archived option premiums.

Hard rules
----------
* **Shadow only.**  Nothing here places, sizes, blocks or alters an order, and
  nothing in the live runtime imports it.
* **Defined risk.**  One short put, one long put, same expiry, fixed width.
* **Measurable.**  One record per session per config, appended to a JSONL
  ledger so the green-day rate can be judged after 20-30 sessions.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

ROOT = Path(__file__).resolve().parent.parent
LOT_SIZE = 65
ENTRY_MINUTE = "09:45"
EXIT_MINUTE = "15:05"

DEFAULT_OTM = 0.015
DEFAULT_WIDTH = 100.0
DEFAULT_COST_POINTS = 2.0


def default_archive_path() -> Path:
    configured = os.environ.get("TRIO_OPTIONS_ARCHIVE_PATH")
    if configured:
        return Path(configured).expanduser()
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    base = Path(local) if local else Path.home() / ".trio"
    return base / "TRIO" / "options" / "options_archive.sqlite3"


def default_ledger_path() -> Path:
    return ROOT / "output" / "shadow_short_premium.jsonl"


# ---------------------------------------------------------------------------
# Pure planning / P&L
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpreadPlan:
    underlying: str
    day: str
    expiry: str
    spot: float
    short_strike: float
    long_strike: float
    width: float
    credit: float
    cost_points: float

    @property
    def max_loss_points(self) -> float:
        return self.width - self.credit


def plan_bull_put_spread(
    day: str,
    expiry: str,
    spot: float,
    quotes: Mapping[float, float],
    *,
    underlying: str = "NIFTY",
    otm: float = DEFAULT_OTM,
    width: float = DEFAULT_WIDTH,
    cost_points: float = DEFAULT_COST_POINTS,
) -> Optional[SpreadPlan]:
    """Pick the short put nearest ``spot * (1 - otm)`` and buy ``width`` lower.

    ``quotes`` maps strike -> premium at the entry timestamp.  Returns ``None``
    when the chain cannot express the trade (no strike below target, missing
    leg, non-positive credit, or credit >= width).
    """
    if spot <= 0 or width <= 0 or not quotes:
        return None
    target = spot * (1.0 - otm)
    below = [strike for strike in quotes if strike <= target]
    if not below:
        return None
    short_strike = max(below)
    long_strike = short_strike - width
    short_px = quotes.get(short_strike)
    long_px = quotes.get(long_strike)
    if short_px is None or long_px is None:
        return None
    credit = float(short_px) - float(long_px)
    if credit <= 0 or credit >= width:
        return None
    return SpreadPlan(
        underlying=underlying,
        day=day,
        expiry=expiry,
        spot=float(spot),
        short_strike=float(short_strike),
        long_strike=float(long_strike),
        width=float(width),
        credit=round(credit, 4),
        cost_points=float(cost_points),
    )


def settle(plan: SpreadPlan, exit_value: float,
           lots: int = 1) -> Dict[str, Any]:
    """Mark the spread closed at ``exit_value`` (debit to close)."""
    gross = plan.credit - float(exit_value)
    net = gross - plan.cost_points
    return {
        "gross_points": round(gross, 4),
        "net_points": round(net, 4),
        "net_inr": round(net * LOT_SIZE * lots, 2),
        "max_loss_points": round(plan.max_loss_points, 4),
        "green": net > 0,
        "break_even_win_pct": round(
            100.0 * plan.max_loss_points / (plan.max_loss_points + plan.credit),
            2,
        ),
    }


# ---------------------------------------------------------------------------
# Archive replay
# ---------------------------------------------------------------------------


def _ist_minute(utc_minute: str) -> str:
    hours, minutes = (int(part) for part in utc_minute.split(":"))
    total = hours * 60 + minutes + 330
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


def load_spot_by_day(index_csv: Path) -> Dict[str, float]:
    """09:40 bar close (the last completed bar before the 09:45 decision)."""
    import pandas as pd

    frame = pd.read_csv(index_csv)
    if "timestamp" not in frame.columns:
        stamp = "date" if "date" in frame.columns else frame.columns[0]
        frame = frame.rename(columns={
            stamp: "timestamp", "Open": "open", "High": "high",
            "Low": "low", "Close": "close", "Volume": "volume",
        })
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["timestamp"] = frame["timestamp"].dt.tz_convert("Asia/Kolkata")
    frame["day"] = frame["timestamp"].dt.strftime("%Y-%m-%d")
    frame["minute"] = frame["timestamp"].dt.strftime("%H:%M")
    chosen = frame[frame["minute"] == "09:40"]
    return {day: float(rows.iloc[-1]["close"])
            for day, rows in chosen.groupby("day")}


def load_day_chain(conn: sqlite3.Connection, day: str,
                   option_type: str = "PE") -> Dict[str, Dict[str, Dict[float, float]]]:
    """expiry -> minute -> {strike: close}, volume-filtered."""
    rows = conn.execute(
        """
        SELECT c.expiry_date, c.strike, substr(k.timestamp_utc, 12, 5), k.close
        FROM candles k JOIN contracts c ON c.contract_id = k.contract_id
        WHERE c.underlying = 'NIFTY' AND c.option_type = ?
          AND substr(k.timestamp_utc, 1, 10) = ?
          AND k.volume > 0 AND k.close > 0
        """,
        (option_type, day),
    ).fetchall()
    chain: Dict[str, Dict[str, Dict[float, float]]] = {}
    for expiry, strike, utc_minute, close in rows:
        minute = _ist_minute(utc_minute)
        chain.setdefault(str(expiry), {}).setdefault(minute, {})[
            round(float(strike), 2)] = float(close)
    return chain


def choose_expiry(chain: Mapping[str, Any], day: str,
                  min_strikes: int = 8) -> Optional[str]:
    candidates = [
        expiry for expiry, minutes in chain.items()
        if expiry > day and len(minutes.get(ENTRY_MINUTE, {})) >= min_strikes
    ]
    return min(candidates) if candidates else None


def replay_day(
    conn: sqlite3.Connection,
    day: str,
    spot: float,
    *,
    otm: float = DEFAULT_OTM,
    width: float = DEFAULT_WIDTH,
    cost_points: float = DEFAULT_COST_POINTS,
    min_strikes: int = 8,
    lots: int = 1,
) -> Optional[Dict[str, Any]]:
    """Build and settle one session's would-be spread from the archive."""
    chain = load_day_chain(conn, day)
    expiry = choose_expiry(chain, day, min_strikes)
    if expiry is None:
        return None
    entry_quotes = chain[expiry].get(ENTRY_MINUTE, {})
    plan = plan_bull_put_spread(
        day, expiry, spot, entry_quotes, otm=otm, width=width,
        cost_points=cost_points,
    )
    if plan is None:
        return None

    exit_value = None
    exit_reason = "time"
    for minute in sorted(chain[expiry]):
        if minute <= ENTRY_MINUTE:
            continue
        quotes = chain[expiry][minute]
        short_px = quotes.get(plan.short_strike)
        long_px = quotes.get(plan.long_strike)
        if short_px is None or long_px is None:
            continue
        value = float(short_px) - float(long_px)
        if minute >= EXIT_MINUTE:
            exit_value, exit_reason = value, "time"
            break
    if exit_value is None:
        return None

    outcome = settle(plan, exit_value, lots=lots)
    record = {
        "kind": "shadow_short_premium",
        "strategy": "bull_put_spread",
        "day": day,
        "expiry": expiry,
        "exit_value": round(float(exit_value), 4),
        "exit_reason": exit_reason,
        "lots": lots,
        "lot_size": LOT_SIZE,
        "source": "archive_replay",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "config": {"otm": otm, "width": width, "cost_points": cost_points},
        **asdict(plan),
        **outcome,
    }
    return record


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def _key(record: Mapping[str, Any]) -> tuple:
    return (
        str(record.get("day")),
        str(record.get("strategy")),
        str(record.get("expiry")),
        float(record.get("short_strike") or 0.0),
        float(record.get("long_strike") or 0.0),
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
    """Append unless the same (day, strategy, expiry, legs) already exists."""
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
    if not rows:
        return {}
    net = [float(row.get("net_points") or 0.0) for row in rows]
    wins = [value for value in net if value > 0]
    losses = [value for value in net if value <= 0]
    equity = peak = drawdown = 0.0
    for value in net:
        equity += value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    streak = worst_streak = 0
    for value in net:
        streak = streak + 1 if value <= 0 else 0
        worst_streak = max(worst_streak, streak)
    return {
        "sessions": len(rows),
        "green": len(wins),
        "green_pct": round(100.0 * len(wins) / len(rows), 1),
        "net_points": round(sum(net), 2),
        "net_inr": round(sum(net) * LOT_SIZE, 2),
        "avg_inr": round(sum(net) * LOT_SIZE / len(rows), 2),
        "worst_inr": round(min(net) * LOT_SIZE, 2),
        "best_inr": round(max(net) * LOT_SIZE, 2),
        "profit_factor": (round(sum(wins) / abs(sum(losses)), 2)
                          if losses else None),
        "max_drawdown_points": round(drawdown, 2),
        "max_loss_streak": worst_streak,
    }


__all__ = [
    "SpreadPlan", "plan_bull_put_spread", "settle", "load_spot_by_day",
    "load_day_chain", "choose_expiry", "replay_day", "read_records",
    "append_record", "summarise", "default_archive_path", "default_ledger_path",
    "ENTRY_MINUTE", "EXIT_MINUTE", "LOT_SIZE",
]
