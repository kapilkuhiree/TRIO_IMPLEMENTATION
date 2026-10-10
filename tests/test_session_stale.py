"""
TRIO — Stale-plan tests (Phase 4) + premarket stats (Phase 6)
Author: Kapil Kuhire <kapilkuhire89@gmail.com>
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.plan import find_latest_plan, validate_stale


def test_find_latest_rejects_future_file(tmp_path):
    """A future-dated JSON (clock skew) must not win over today."""
    from datetime import datetime, timedelta, timezone
    IST = timezone(timedelta(hours=5, minutes=30))
    tomorrow = (datetime.now(IST) + timedelta(days=5)).strftime("%Y-%m-%d")
    (tmp_path / f"{tomorrow}.json").write_text(json.dumps({"date": tomorrow}))
    today = datetime.now(IST).strftime("%Y-%m-%d")
    (tmp_path / f"{today}.json").write_text(json.dumps({"date": today}))
    got = find_latest_plan(plan_dir=tmp_path)
    assert got is not None
    assert got["date"] == today


def test_validate_stale_age_exceeded():
    cand = {"symbol": "TCS.NS", "entry_price": 100.0, "stop_loss": 90.0,
            "target": 115.0, "position_size": 10, "date": "2026-10-09"}
    r = validate_stale(cand, "2026-10-11")
    assert r is not None and "stale" in r.lower()


def test_validate_stale_price_deviation(tmp_path):
    cand = {"symbol": "X", "entry_price": 100.0, "stop_loss": 90.0,
            "target": 115.0, "position_size": 10, "date": "2026-10-10"}
    r = validate_stale(cand, "2026-10-10", live_price=105.0)
    assert r is not None and "deviation" in r.lower()


def test_validate_stale_ok():
    cand = {"symbol": "X", "entry_price": 100.0, "stop_loss": 90.0,
            "target": 115.0, "position_size": 10, "date": "2026-10-10"}
    assert validate_stale(cand, "2026-10-10", live_price=100.2) is None


def test_premarket_plan_has_requested_and_failed():
    """build_plan must surface requested/failed alongside scanned."""
    from scripts.premarket import build_plan
    with patch("scripts.premarket.resolve_basket",
               return_value=["A.NS", "B.NS", "C.NS"]), \
         patch("src.data_fetcher.fetch_multiple",
               return_value={
                   "A.NS": type("MD", (), {"latest_price": 100.0, "ohlcv": _ohlcv()})(),
               }), \
         patch("src.screener.screen", return_value=[]), \
         patch("scripts.premarket.load_config", return_value={
             "screener": {"min_rank": 40.0, "min_confidence": 60,
                          "max_positions_to_open": 3},
             "options": {"enabled": False},
         }):
        plan = build_plan("nifty100", "1d", 3, with_options=False)
    assert plan["requested"] == 3
    assert plan["scanned"] == 3
    assert "failed" in plan


def _ohlcv():
    import pandas as pd
    return pd.DataFrame({
        "Open": [99, 100], "High": [101, 101], "Low": [98, 99],
        "Close": [100, 100], "Volume": [1000, 1000],
    })
