"""
TRIO — shared pytest configuration.
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

Pins the broker provider to the local in-memory simulator so the suite
is deterministic and offline, regardless of config.yaml (which defaults
to the MegaBull remote API for live trading). Individual tests can
still construct MegaBullBroker directly with a fake HTTP session.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("TRIO_BROKER_PROVIDER", "paper")
os.environ["TRIO_REMOTE_REQUIRED"] = "0"

# Hard guarantee: every PaperTrader built inside a test run writes to the
# *_test sidecar ledger, never the live output/trade_log.jsonl. On 2026-10-06
# price=100 unit fixtures booked into the live ledger; this makes that
# impossible even when a test forgets `test_mode=True`.
os.environ["TRIO_TEST_MODE"] = "1"


@pytest.fixture(autouse=True)
def _restore_offline_broker_environment(monkeypatch):
    """Keep one test's broker-mode mutation from leaking into later tests."""
    monkeypatch.setenv("TRIO_BROKER_PROVIDER", "paper")
    monkeypatch.setenv("TRIO_REMOTE_REQUIRED", "0")
    monkeypatch.setenv("TRIO_TEST_MODE", "1")
    monkeypatch.setenv("TRIO_LIVE", "0")
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    try:
        yield
    finally:
        # Some tests intentionally mutate the process environment with
        # patch.dict/os.environ rather than this fixture's MonkeyPatch. Reset
        # the complete offline boundary before pytest unwinds MonkeyPatch so
        # those mutations cannot affect the next test's PaperTrader setup.
        os.environ["TRIO_BROKER_PROVIDER"] = "paper"
        os.environ["TRIO_REMOTE_REQUIRED"] = "0"
        os.environ["TRIO_TEST_MODE"] = "1"
        os.environ["TRIO_LIVE"] = "0"
        os.environ["PYTHON_DOTENV_DISABLED"] = "1"
        os.environ.pop("TRIO_TRADING_MODE", None)
