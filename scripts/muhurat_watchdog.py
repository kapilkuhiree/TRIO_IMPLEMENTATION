"""TRIO - independent Muhurat hard-flat watchdog.

Author: Kapil Kuhire <kapilkuhire89@gmail.com>

The main runner is flat at 15:15 IST.  This process is the second pair of
hands: it cancels every working order and squares off every NFO holding at
15:16 IST, regardless of the runner's state.  Run it in a second terminal.

It refuses to touch the account on any date other than the declared session
unless ``--force`` is passed, so an accidental start on a normal weekday is
harmless.

DISCLAIMER: Educational purposes only. Not financial advice.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import os
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

IST = timezone(timedelta(hours=5, minutes=30))
SESSION_DATE = "2026-11-08"
DEFAULT_FLAT_AT = "15:16"


def _load_env(env_file: Optional[str]) -> None:
    candidates = [Path(env_file).expanduser()] if env_file else [
        ROOT / ".env", ROOT.parent / ".env"]
    for path in candidates:
        if not path.exists():
            continue
        try:
            from dotenv import load_dotenv
            load_dotenv(path)
        except ImportError:
            for line in path.read_text(encoding="utf-8").splitlines():
                text = line.strip()
                if text and not text.startswith("#") and "=" in text:
                    key, value = text.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip().strip("'\""))
        return


def _broker(args: Optional[argparse.Namespace] = None):
    if getattr(args, "broker", "zerodha") == "megabull":
        from src.broker.megabull import MegaBullBroker
        from src.broker.megabull_options import MegaBullOptionsBroker

        scripts = str(ROOT / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        from muhurat_live import _MegaBullRunnerAdapter

        mega = MegaBullBroker(
            api_key=os.environ.get("MEGABULL_API_KEY", ""),
            initial_capital=500000.0,
        )
        options = MegaBullOptionsBroker(mega, lot_size=65,
                                        initial_capital=500000.0)
        return _MegaBullRunnerAdapter(mega, options)

    from src.broker.zerodha_options import ZerodhaOptionsBroker
    from src.zerodha_market_data import ZerodhaMarketData

    market_data = ZerodhaMarketData.from_env()
    # No underlying filter: the watchdog must be able to close any NFO
    # holding, even one outside the strategy's NIFTY/BANKNIFTY universe.
    return ZerodhaOptionsBroker(
        market_data,
        product="NRML",
        order_timeout_seconds=8.0,
        exit_timeout_seconds=5.0,
        tag="TRIO-WATCHDOG",
    )


def _target_today(at: str) -> datetime:
    hour, minute = (int(part) for part in at.split(":", 1))
    now = datetime.now(IST)
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _flatten_until_flat(broker, attempts: int = 3) -> int:
    remaining = []
    for attempt in range(1, attempts + 1):
        print(f"flatten attempt {attempt}/{attempts}")
        result = broker.flatten_all(reason="watchdog-hard-flat")
        print(f"  cancelled={result.get('cancelled')} closed={result.get('closed')} "
              f"positions_read={result.get('positions_ok')}")
        remaining = result.get("remaining") or []
        if not remaining and result.get("positions_ok") is not False:
            print("  account is flat")
            return 0
        if result.get("positions_ok") is False:
            print("  positions could not be read; retrying")
        print(f"  remaining={remaining}")
        time.sleep(3.0)
    return 2


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Muhurat hard-flat watchdog")
    parser.add_argument("--env-file")
    parser.add_argument("--at", default=DEFAULT_FLAT_AT,
                        help="IST time to flatten (default 15:16)")
    parser.add_argument("--session-date", default=SESSION_DATE)
    parser.add_argument("--force", action="store_true",
                        help="flatten even when today is not the session date")
    parser.add_argument("--now", action="store_true",
                        help="flatten immediately (still date-gated)")
    parser.add_argument("--check", action="store_true",
                        help="report holdings/orders without trading")
    parser.add_argument("--broker", choices=("zerodha", "megabull"),
                        default="zerodha",
                        help="execution venue: zerodha (live) or megabull (paper)")
    args = parser.parse_args(argv)

    _load_env(args.env_file)
    os.environ["TRADING_MODE"] = "options_intraday"
    os.environ["TRIO_MARKET_DATA_PROVIDER"] = "zerodha"
    os.environ["TRIO_OPTIONS_CHAIN_PROVIDER"] = "zerodha"
    if args.broker == "megabull":
        os.environ["TRIO_OPTIONS_LIVE"] = "0"
        os.environ["TRIO_BROKER_PROVIDER"] = "megabull"
        os.environ["PAPER_TRADING"] = "true"
        os.environ["TRIO_REMOTE_REQUIRED"] = "true"

    today = datetime.now(IST).date().isoformat()
    if today != args.session_date and not args.force:
        print(f"watchdog: today is {today}, session is {args.session_date}; "
              "refusing to touch the account (use --force to override)")
        return 1

    broker = _broker(args)
    if not broker.refresh_positions():
        print("WARNING: positions endpoint unavailable; flatten will retry")
    print(f"holdings: {sorted(broker.positions)}")

    if args.check:
        if args.broker == "megabull":
            print("open orders: not tracked separately (spread legs close with "
                  "each position)")
            return 0
        try:
            orders = broker.kite.orders() or []
        except Exception as exc:
            print(f"order book unavailable: {exc}")
            return 2
        open_orders = [row.get("order_id") for row in orders
                       if isinstance(row, dict) and
                       str(row.get("status") or "").upper() in {
                           "OPEN", "TRIGGER PENDING", "VALIDATION PENDING",
                           "PUT ORDER REQ RECEIVED", "MODIFY PENDING"}]
        print(f"open orders: {open_orders}")
        return 0

    if not args.now:
        target = _target_today(args.at)
        while True:
            now = datetime.now(IST)
            if now >= target:
                break
            remaining = (target - now).total_seconds()
            print(f"watchdog armed for {target.isoformat()} "
                  f"({remaining:.0f}s); Ctrl+C to abort")
            time.sleep(min(60.0, max(1.0, remaining)))

    return _flatten_until_flat(broker)


if __name__ == "__main__":
    raise SystemExit(main())
