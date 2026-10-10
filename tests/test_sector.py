"""
TRIO — Sector exposure tests (Phase 7)
Author: Kapil Kuhire <kapilkuhire89@gmail.com>
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.risk_manager import (
    sector_exposure, would_breach_sector, reset_risk_state,
)
from src.sector_map import sector_for


def setup_function():
    reset_risk_state()


def test_sector_for_known_and_unknown():
    assert sector_for("HDFCBANK.NS") == "Financials"
    assert sector_for("ICICIBANK.NS") == "Financials"
    assert sector_for("TCS.NS") == "Technology"
    assert sector_for("UNKNOWN_XYZ.NS") == "Unknown"


def test_sector_exposure_pct():
    # 3 banks, each 10 * 1000 = 30k notional, 100k capital => 30% each? no: sector sum 30k.
    # Use sizes that sum visibly.
    pos = {
        "HDFCBANK.NS": {"size": 10, "entry": 1000.0},
        "ICICIBANK.NS": {"size": 10, "entry": 1000.0},
        "RELIANCE.NS": {"size": 10, "entry": 1000.0},
    }
    exp = sector_exposure(pos, capital=100000.0)
    assert exp["Financials"] == 20.0
    assert exp["Energy"] == 10.0


def test_would_breach_sector_correlated_banking():
    with patch("src.risk_manager.load_config", return_value={
        "risk_management": {"capital": 100000,
                            "max_exposure_per_sector_pct": 30}}):
        existing = {
            "HDFCBANK.NS": {"size": 10, "entry": 1000.0},  # 10k
            "ICICIBANK.NS": {"size": 10, "entry": 1000.0},  # 10k -> Financials 20%
        }
        # Adding a third bank 20k would push Financials to 40% > 30% -> breach
        assert would_breach_sector("SBIN.NS", 20, 1000.0,
                                   existing=existing, limit_pct=30) is True
        # Adding tech should not breach
        assert would_breach_sector("TCS.NS", 10, 1000.0,
                                   existing=existing, limit_pct=30) is False


def test_execute_plan_skips_sector_breached_symbol():
    from src.paper_trader import PaperTrader
    from src.broker.paper import PaperBroker
    t = PaperTrader(symbols=["X"], timeframe="1d", initial_capital=500000,
                     test_mode=True)
    t.broker = PaperBroker(initial_capital=500000)
    # Seed 2 banks already held
    t.broker.place_order("HDFCBANK.NS", "BUY", 10, price=1000.0)
    t.broker.place_order("ICICIBANK.NS", "BUY", 10, price=1000.0)
    # Also seed RiskState so the sector gate sees them
    from src.risk_manager import add_position
    add_position("HDFCBANK.NS", 10, 1000.0, 100.0)
    add_position("ICICIBANK.NS", 10, 1000.0, 100.0)
    with patch("src.risk_manager.load_config", return_value={
        "risk_management": {"capital": 500000,
                            "max_exposure_per_sector_pct": 10,
                            "max_open_positions": 10},
        "trading": {"allow_shorts": False},
        "broker": {"provider": "paper"},
    }):
        plan = {"candidates": [
            {"symbol": "SBIN.NS", "action": "BUY", "entry_price": 600.0,
             "stop_loss": 560.0, "target": 660.0,
             "position_size": 50, "confidence": 80, "rank": 90.0,
             "setup_name": "pullback-long"},
            {"symbol": "TCS.NS", "action": "BUY", "entry_price": 3000.0,
             "stop_loss": 2900.0, "target": 3150.0,
             "position_size": 5, "confidence": 80, "rank": 85.0,
             "setup_name": "pullback-long"},
        ]}
        placed = t.execute_plan(plan)
    # Banking SBIN should be skipped, tech TCS should still place
    assert any(r["symbol"] == "TCS.NS" for r in placed)
    assert any(r["symbol"] != "SBIN.NS" for r in placed)
