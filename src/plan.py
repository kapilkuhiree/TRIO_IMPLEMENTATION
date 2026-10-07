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


def mark_executed(payload: Dict[str, Any], order_ids: List[str],
                  date: Optional[str] = None,
                  plan_dir: Optional[Path] = None) -> Path:
    """Record which orders the session job placed, so a re-run never doubles."""
    payload["executed"] = True
    payload["executed_at"] = datetime.now(timezone.utc).isoformat()
    payload["order_ids"] = list(order_ids)
    return save_plan(payload, date, plan_dir)