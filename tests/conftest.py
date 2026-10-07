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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("TRIO_BROKER_PROVIDER", "paper")

# Hard guarantee: every PaperTrader built inside a test run writes to the
# *_test sidecar ledger, never the live output/trade_log.jsonl. On 2026-10-06
# price=100 unit fixtures booked into the live ledger; this makes that
# impossible even when a test forgets `test_mode=True`.
os.environ["TRIO_TEST_MODE"] = "1"
