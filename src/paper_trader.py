"""
TRIO — Paper Trader
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

Runs the full signal pipeline in a loop at configurable intervals,
 generating signals and simulating trades via the paper broker.

DISCLAIMER: Educational purposes only. Not financial advice.
"""

import csv
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.utils import (get_logger, load_config, utc_now, remote_paper_required,
                        local_option_shadow_allowed)
from src.data_fetcher import fetch_market_data
from src.indicators import compute_indicators
from src.sentiment import analyze_sentiment
from src.signal_engine import generate_signal, TradeSignal
from src.risk_manager import apply_risk_management, get_risk_state
from src.broker.paper import PaperBroker

logger = get_logger("paper_trader")


class PaperTrader:
    """
    Paper trading loop. Scans symbols at regular intervals,
    generates signals, and simulates trades.

    Usage:
        trader = PaperTrader(symbols=["RELIANCE.NS", "TCS.NS"])
        trader.run(interval_seconds=300)
    Author: Kapil Kuhire
    """

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        timeframe: Optional[str] = None,
        initial_capital: Optional[float] = None,
        test_mode: bool = False,
    ):
        cfg = load_config()

        self.symbols = symbols or cfg.get("symbols", [])
        self.timeframe = timeframe or cfg.get("timeframes", {}).get("default", "15m")
        self.test_mode = test_mode  # smoke/unit runs write a SEPARATE log file

        rm_cfg = cfg.get("risk_management", {})
        capital = initial_capital or rm_cfg.get("capital", 100000)

        # Provider MUST be explicit. Any typo like "megabul" now fails fast
        # instead of silently returning a paper simulator that hides the error.
        allowed = {"paper", "megabull"}
        # Optional: add "zerodha" here when that adapter is fully implemented.
        raw = provider = (
            os.environ.get("TRIO_BROKER_PROVIDER", "").strip().lower()
            or (cfg.get("broker", {}) or {}).get("provider", "paper")
        )
        if raw not in allowed:
            raise ValueError(
                f"Unknown broker provider '{raw}' — allowed: {sorted(allowed)}. "
                f"Refusing to trade silently on a typoed simulator.")
        if remote_paper_required(cfg):
            from src.remote_paper import validate_remote_config
            validate_remote_config(cfg, provider=provider)
        # MegaBull paper requires intent: env or config "megabull" plus a
        # non-empty API key, otherwise fail-closed before any order.
        if provider == "megabull":
            from src.broker.megabull import MegaBullBroker, load_dotenv_key
            api_key = os.environ.get("MEGABULL_API_KEY", "") or \
                load_dotenv_key("MEGABULL_API_KEY")
            if not api_key:
                raise ValueError(
                    "Broker 'megabull' selected but no MEGABULL_API_KEY found "
                    "in the environment or .env — refusing to run without credentials.")
            # Live mode must be confirmed explicitly: TRIO_LIVE=1 or
            # forward_test.live_confirmed:true — never silently extend the
            # remote paper balance to a real account.
            mode = str((cfg.get("general", {}) or {}).get(
                "mode", cfg.get("mode", ""))).lower()
            if mode in ("live", "real") \
                    and not str(os.environ.get("TRIO_LIVE", "")).strip():
                if not bool((cfg.get("forward_test", {}) or {}).get("live_confirmed")):
                    raise RuntimeError(
                        "Config 'mode: live' requires TRIO_LIVE=1 or "
                        "forward_test.live_confirmed: true to proceed.")
            self.broker = MegaBullBroker(
                api_key=api_key,
                initial_capital=capital,
            )
            logger.info("Broker provider: MegaBull (remote paper fills).")
        else:
            self.broker = PaperBroker(initial_capital=capital)
        self.broker_name = (
            getattr(self.broker, "provider_name", "paper"))
        self.signals_log: List[Dict[str, Any]] = []
        self._eod_squared = False  # one EOD square-off per session
        self._last_plan_order_ids: List[str] = []
        self._last_option_order_ids: List[str] = []

        # Trade log: one JSON line per event (scan/screen/order/guard/close).
        # Path from config `logging.trade_log`; CSV mirror written at session end.
        # Test runs (smoke/unit/smoke keywords in the path or explicit
        # test_mode) go to a guarded sidecar file so price=100 test orders
        # NEVER pollute live P&L analysis again.
        log_cfg = cfg.get("logging", {})
        default_log = str(Path(cfg.get("general", {}).get("output_dir", "output"))
                          / "trade_log.jsonl")
        self.trade_log_path = Path(log_cfg.get("trade_log", default_log))
        # The configured live ledger. Never written while TRIO_TEST_MODE=1.
        self._live_log_path = self.trade_log_path

        logger.info(
            "PaperTrader initialized: %d symbols, timeframe=%s, capital=%.2f",
            len(self.symbols), self.timeframe, capital,
        )
