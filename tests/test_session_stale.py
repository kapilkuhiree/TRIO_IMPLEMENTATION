"""
TRIO — Stale-plan tests (Phase 4) + premarket stats (Phase 6)
Author: Kapil Kuhire <kapilkuhire89@gmail.com>
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.plan import (find_latest_plan, load_plan, validate_plan_identity,
                       validate_stale, next_trading_day_ist_str)


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


def test_load_plan_rejects_filename_payload_date_mismatch(tmp_path):
    (tmp_path / "2026-10-14.json").write_text(
        json.dumps({"date": "2026-10-15", "candidates": []}))
    assert load_plan("2026-10-14", plan_dir=tmp_path) is None


def test_validate_plan_identity_rejects_previous_session_for_production():
    reason = validate_plan_identity(
        {"date": "2026-10-14"}, expected_date="2026-10-14",
        current_date="2026-10-15", require_current=True)
    assert reason is not None
    assert "current session" in reason


def test_find_latest_skips_mismatched_payload_and_uses_valid_file(tmp_path):
    (tmp_path / "2026-10-14.json").write_text(
        json.dumps({"date": "2026-10-13"}))
    (tmp_path / "2026-10-13.json").write_text(
        json.dumps({"date": "2026-10-13", "candidates": []}))
    got = find_latest_plan(plan_dir=tmp_path)
    assert got is not None
    assert got["date"] == "2026-10-13"


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


def test_validate_stale_missing_candidate_date_uses_source_plan_date():
    cand = {"symbol": "X", "entry_price": 100.0, "stop_loss": 90.0,
            "target": 115.0, "position_size": 10}
    reason = validate_stale(
        cand, "2026-10-15", source_date="2026-10-13")
    assert reason is not None
    assert "stale" in reason.lower()


def test_next_trading_day_skips_weekend_and_configured_holiday():
    with patch("src.utils.load_config", return_value={
            "market": {"holidays": ["2026-10-12"]}}):
        # Friday -> Monday, but Monday is configured as a holiday -> Tuesday.
        assert next_trading_day_ist_str("2026-10-09") == "2026-10-13"


def test_premarket_plan_has_requested_and_failed():
    """build_plan must surface requested/failed alongside scanned."""
    from scripts.premarket import build_plan
    with patch("scripts.premarket.resolve_basket",
               return_value=["A.NS", "B.NS", "C.NS"]), \
         patch("src.data_fetcher.fetch_multiple",
               return_value={
                   "A.NS": type("MD", (), {"latest_price": 100.0, "ohlcv": _ohlcv()})(),
               }), \
         patch("scripts.premarket.screen", return_value=[]), \
         patch("scripts.premarket.load_config", return_value={
             "screener": {"min_rank": 40.0, "min_confidence": 60,
                          "max_positions_to_open": 3},
             "options": {"enabled": False},
         }):
        plan = build_plan("nifty100", "1d", 3, with_options=False)
    assert plan["requested"] == 3
    assert plan["scanned"] == 3
    assert "failed" in plan


def test_premarket_keeps_equity_cap_but_builds_wider_option_pool():
    """Options can inspect more ranked ideas without widening equity orders."""
    from types import SimpleNamespace
    from src.signal_engine import TradeSignal
    from src.screener import RankedCandidate
    from scripts.premarket import build_plan

    def candidate(symbol, rank):
        sig = TradeSignal(symbol=symbol, action="BUY", entry_price=100.0,
                          confidence=80)
        return RankedCandidate(rank=rank, signal=sig, edge_atr=2.0,
                               setup_name="pullback-long")

    screened = [candidate("A.NS", 120), candidate("B.NS", 110),
                candidate("C.NS", 100)]
    option_pick = SimpleNamespace(to_dict=lambda: {
        "strategy": "long-call", "underlying": "NIFTY", "legs": []})
    with patch("scripts.premarket.resolve_basket",
               return_value=["A.NS", "B.NS", "C.NS"]), \
         patch("scripts.premarket.screen", return_value=screened), \
         patch("scripts.premarket.load_config", return_value={
             "screener": {"min_rank": 60.0, "min_confidence": 70,
                          "max_positions_to_open": 1,
                          "options_max_candidates": 3,
                          "options_min_rank": 60.0,
                          "options_min_confidence": 70},
             "options": {"enabled": True, "underlying": "NIFTY"},
         }), \
         patch("src.options_chain.fetch_chain", return_value={"ok": True}), \
         patch("src.options_chain.nearest_expiry", return_value="30-Oct-2026"), \
         patch("src.options_selector.select", return_value=option_pick):
        # Exercise pool sizing, not external quote/history providers.
        with patch("src.data_fetcher.fetch_market_data", return_value=SimpleNamespace(ohlcv=_ohlcv())):
            plan = build_plan("nifty100", "1d", 1)

    assert len(plan["candidates"]) == 1
    assert len(plan["option_candidates"]) == 3


def _ohlcv():
    import pandas as pd
    return pd.DataFrame({
        "Open": [99, 100], "High": [101, 101], "Low": [98, 99],
        "Close": [100, 100], "Volume": [1000, 1000],
    })
