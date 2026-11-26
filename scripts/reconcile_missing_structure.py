"""Settle a paper structure whose remote legs are already flat.

Read-only against the broker (GET only).  It never sends an order: it only
books the runtime book entry for a position that the mirror proves is no
longer held.  Used for the 2026-11-25 incident where other trades sharing the
same option symbols sold all remote units.

Usage (from repo root, with the session stopped):
    python scripts/reconcile_missing_structure.py
    python scripts/reconcile_missing_structure.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
import sys  # noqa: E402

sys.path.insert(0, str(ROOT))

from src.options_pricing import round_trip_cost  # noqa: E402

STATE = Path(
    r"C:\Users\Kapil Kuhire\AppData\Local\TRIO\options_intraday\state.json"
)
POSITION_ID = "OPT_NIFTY26D0125900CE_NIFTY26D0125900PE"
CE_EXIT = 202.30  # MegaBull remote order 886899, 12:39:26 IST
PE_EXIT = 370.75  # MegaBull remote order 887114, 13:17:18 IST
REASON = "remote-already-flat"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _remote_positions() -> Dict[str, int]:
    """Return the live remote book; fail closed if it cannot be read."""
    from src.utils import load_env

    load_env()
    from src.broker.megabull import MegaBullBroker, load_dotenv_key

    key = os.environ.get("MEGABULL_API_KEY") or load_dotenv_key("MEGABULL_API_KEY")
    if not key:
        raise RuntimeError("MEGABULL_API_KEY unavailable; refusing to reconcile")
    broker = MegaBullBroker(api_key=key)
    broker.refresh_positions()
    return {
        symbol: int(getattr(pos, "quantity", 0) or 0)
        for symbol, pos in broker.positions.items()
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=STATE)
    parser.add_argument("--position-id", default=POSITION_ID)
    parser.add_argument("--ce-exit", type=float, default=CE_EXIT)
    parser.add_argument("--pe-exit", type=float, default=PE_EXIT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--skip-remote-check", action="store_true",
        help="only for offline replay; never use against a live book",
    )
    args = parser.parse_args(argv)

    remote = {} if args.skip_remote_check else _remote_positions()
    if remote:
        raise RuntimeError(
            f"Remote book still has positions {remote}; refusing to settle "
            f"{args.position_id}"
        )

    state_path: Path = args.state
    data: Dict[str, Any] = json.loads(state_path.read_text(encoding="utf-8"))
    position = (data.get("positions") or {}).get(args.position_id)
    if not isinstance(position, dict):
        print(f"Nothing to reconcile: {args.position_id} is not in state")
        return 0

    entry = float(position.get("entry_premium") or 0.0)
    lots = int(position.get("lots") or 0) or 1
    lot_size = int(position.get("lot_size") or 0) or 65
    exit_premium = round(args.ce_exit + args.pe_exit, 2)
    gross = round((exit_premium - entry) * lots * lot_size, 2)
    cost = round_trip_cost(
        str(position.get("strategy") or "long_straddle"),
        entry, exit_premium, lots, lot_size,
    )
    pnl = round(gross - float(cost), 2)
    closed_at = _utc_now()

    record = {
        "asset": "options",
        "close_id": f"options:{args.position_id}|{closed_at}",
        "closed_at": closed_at,
        "direction": position.get("direction"),
        "entry_premium": entry,
        "entry_ts": position.get("opened_at"),
        "execution_class": position.get("execution_class"),
        "exit_bid": exit_premium,
        "expiry": position.get("expiry"),
        "historical_options_pnl": "NOT_READY_FOR_OPTIONS_PNL",
        "kind": position.get("kind"),
        "lot_size": lot_size,
        "lots": lots,
        "option_underlying": position.get("option_underlying"),
        "pnl": pnl,
        "position_id": args.position_id,
        "reason": REASON,
        "setup": position.get("setup"),
        "signal_action": position.get("signal_action"),
        "signal_underlying": position.get("signal_underlying"),
        "strategy": position.get("strategy"),
        "strike": position.get("strike"),
        "symbol": args.position_id,
        "trading_symbol": position.get("trading_symbol"),
    }

    print(f"position      : {args.position_id}")
    print(f"entry premium : {entry:.2f}")
    print(f"exit premium  : {exit_premium:.2f} "
          f"(CE {args.ce_exit:.2f} + PE {args.pe_exit:.2f})")
    print(f"gross P&L     : {gross:+.2f}")
    print(f"cost          : {float(cost):.2f}")
    print(f"net P&L       : {pnl:+.2f}")
    if args.dry_run:
        print("dry run: no files changed")
        return 0

    backup = state_path.with_name(
        state_path.name + ".bak_reconcile_" +
        datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    shutil.copy2(state_path, backup)
    print(f"backup        : {backup}")

    data.setdefault("closed", []).append(record)
    data["positions"].pop(args.position_id, None)
    for intent in (data.get("intents") or {}).values():
        if isinstance(intent, dict) and intent.get("position_id") == args.position_id:
            intent["status"] = "closed"
            intent["closed_at"] = closed_at
    for key in (
        "broker_blocked", "mirror_failures", "last_mirror_error",
        "mirror_unhealthy_since_ms", "mirror_flatten_attempted_ms",
        "mirror_flatten_since_ms", "mirror_last_alert_ms",
    ):
        data.pop(key, None)
    data["last_reconciled_at"] = closed_at

    temp = state_path.with_name(state_path.name + ".reconcile.tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp.replace(state_path)
    print(f"reconciled    : {state_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
