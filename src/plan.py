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
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Plan unreadable (%s): %s", path, exc)
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
    latest_file = json_files[-1]
    try:
        return json.loads(latest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def validate_stale(cand: Dict[str, Any], date: str,
                   live_price: Optional[float] = None,
                   max_age_days: int = 1,
                   max_deviation_pct: float = 1.0) -> Optional[str]:
    """Return None if *cand* is safe to trade when stale, else a reason."""
    try:
        from datetime import datetime as _dt1
        cand_date = str(cand.get("date") or date)
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