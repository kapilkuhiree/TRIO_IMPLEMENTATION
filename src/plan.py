"""
TRIO — Trade Plan I/O
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

The overnight premarket job ranks the universe on daily bars and writes one
plan per trading day. The session job reads that plan at the open and places
the orders. Splitting analysis (after close) from execution (at open) keeps
the 09:20 open fast: the session process never re-screens 100 symbols.

Plan file: output/plans/YYYY-MM-DD.json
{
  "date": "2026-10-08",
  "generated_at": "<utc iso>",
  "universe": "nifty100",
  "timeframe": "1d",
  "scanned": 100, "failed": 0,
  "candidates": [ {signal fields..., "rank", "edge_atr", "setup_name"} ],
  "executed": false
}

DISCLAIMER: Educational purposes only. Not financial advice.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.utils import get_logger

logger = get_logger("plan")

IST = timezone(timedelta(hours=5, minutes=30))
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLAN_DIR = PROJECT_ROOT / "output" / "plans"


def today_ist_str() -> str:
    """Return today's date (IST) as YYYY-MM-DD."""
    return datetime.now(IST).strftime("%Y-%m-%d")


def next_trading_day_ist_str(start: Optional[str] = None) -> str:
    """Return the next NSE session date, skipping weekends and holidays."""
    from src.utils import load_config

    if start:
        current = datetime.strptime(str(start), "%Y-%m-%d").date()
    else:
        current = datetime.now(IST).date()
    try:
        holidays = set(str(x) for x in (load_config().get(
            "market", {}).get("holidays", []) or []))
    except Exception:
        holidays = set()
    current += timedelta(days=1)
    while current.weekday() >= 5 or current.isoformat() in holidays:
        current += timedelta(days=1)
    return current.isoformat()


def plan_path(date: Optional[str] = None, plan_dir: Optional[Path] = None) -> Path:
    """Path to a plan file for a given date (defaults to today IST)."""
    d = plan_dir or PLAN_DIR
    return d / f"{date or today_ist_str()}.json"


def save_plan(payload: Dict[str, Any], date: Optional[str] = None,
              plan_dir: Optional[Path] = None) -> Path:
    """Write a plan JSON file atomically and return its path."""
    path = plan_path(date, plan_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    logger.info("Plan written: %s (%d candidates)",
                path, len(payload.get("candidates", [])))
    return path


def load_plan(date: Optional[str] = None,
              plan_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Load a plan file (defaults to today IST). None when missing/corrupt."""
    path = plan_path(date, plan_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        reason = validate_plan_identity(payload, expected_date=path.stem)
        if reason:
            logger.warning("Plan rejected (%s): %s", path, reason)
            return None
        return payload
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Plan unreadable (%s): %s", path, exc)
        return None


def validate_plan_identity(payload: Any, *, expected_date: Optional[str] = None,
                            current_date: Optional[str] = None,
                            require_current: bool = False) -> Optional[str]:
    """Return a reason when a plan cannot be used for the requested session.

    Research may inspect historical plans, so this helper does not reject an
    old date by default.  Production callers opt into ``require_current`` and
    must also provide the date represented by the filename/request.  Keeping
    the filename/payload check here prevents a renamed or mismatched artifact
    from becoming an executable plan.
    """
    if not isinstance(payload, dict):
        return "plan is not an object"
    payload_date = str(payload.get("date") or "").strip()
    if not payload_date:
        return "plan date is missing"
    try:
        datetime.strptime(payload_date, "%Y-%m-%d")
    except ValueError:
        return "plan date is invalid"
    if expected_date is not None and payload_date != str(expected_date):
        return f"plan date {payload_date} does not match requested {expected_date}"
    if require_current:
        wanted = str(current_date or today_ist_str())
        if payload_date != wanted:
            return f"plan date {payload_date} is not current session {wanted}"
    return None


def find_latest_plan(plan_dir: Optional[Path] = None,
                     max_age_days: int = 365) -> Optional[Dict[str, Any]]:
    """Return the most recent (lexically latest YYYY-MM-DD) plan on disk.

    Args:
        plan_dir:     Override directory.
        max_age_days: Plans older than this are ignored (stale).
    """
    d = plan_dir or PLAN_DIR
    if not d.exists():
        return None
    json_files = sorted(d.glob("*.json"))
    if not json_files:
        return None
    # Reject a future-dated filename (clock skew / bad write)
    cutoff = datetime.now(IST).strftime("%Y-%m-%d")
    json_files = [p for p in json_files if p.stem <= cutoff]
    if max_age_days is not None and max_age_days >= 0:
        try:
            cutoff_early = (datetime.now(IST) - timedelta(days=max_age_days)
                            ).strftime("%Y-%m-%d")
            json_files = [p for p in json_files if p.stem >= cutoff_early]
        except Exception:
            pass
    if not json_files:
        return None
    for latest_file in reversed(json_files):
        try:
            payload = json.loads(latest_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if validate_plan_identity(payload, expected_date=latest_file.stem):
            continue
        return payload
    return None


def validate_stale(cand: Dict[str, Any], date: str,
                   live_price: Optional[float] = None,
                   max_age_days: int = 1,
                   max_deviation_pct: float = 1.0,
                   source_date: Optional[str] = None) -> Optional[str]:
    """Return None if *cand* is safe to trade when stale, else a reason."""
    try:
        from datetime import datetime as _dt1
        # A candidate commonly has no own date.  In that case use the source
        # plan date, never the requested current date, so stale fallback cannot
        # turn yesterday's candidate into a same-day candidate.
        cand_date = str(cand.get("date") or source_date or date)
        days = (_dt1.strptime(date, "%Y-%m-%d") -
                _dt1.strptime(cand_date, "%Y-%m-%d")).days
        if days > max_age_days:
            return f"stale {days}d > {max_age_days}d"
        if live_price is not None:
            ep = float(cand.get("entry_price") or 0) or 1.0
            dev = abs(live_price - ep) / ep * 100
            if dev > max_deviation_pct:
                return f"price-deviation {dev:.1f}% > {max_deviation_pct}%"
        if not cand.get("stop_loss") or not cand.get("target"):
            return "missing stop/target"
        if int(cand.get("position_size") or 0) <= 0:
            return "invalid qty"
        return None
    except Exception as exc:
        return f"stale-check error: {exc}"


def mark_executed(payload: Dict[str, Any], order_ids: List[str],
                  date: Optional[str] = None,
                  plan_dir: Optional[Path] = None) -> Path:
    """Record which orders the session job placed, so a re-run never doubles."""
    payload["executed"] = True
    payload["executed_at"] = datetime.now(timezone.utc).isoformat()
    payload["order_ids"] = list(order_ids)
    return save_plan(payload, date, plan_dir)
