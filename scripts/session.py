"""
TRIO — Session job (GitHub Actions)
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

Runs during the NSE session. Reads today's plan (written by the premarket
job), places the plan at the open, rebuilds stop/target state, then manages
positions every ``--interval`` seconds until the window closes and the EOD
square-off fires. Designed for ONE long job on a public free repo:

    09:15 IST -> 15:10 IST, loop.

    python scripts/session.py [--date YYYY-MM-DD] [--interval 60]
                              [--max-seconds 21600] [--no-plan]

DISCLAIMER: Educational purposes only. Not financial advice.
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.utils import load_env, load_config, get_logger  # noqa: E402
from src.paper_trader import PaperTrader  # noqa: E402
from src.plan import load_plan, today_ist_str, mark_executed  # noqa: E402

load_env()
logger = get_logger("session")

IST = timezone(timedelta(hours=5, minutes=30))


def main() -> None:
    parser = argparse.ArgumentParser(description="TRIO session runner")
    parser.add_argument("--date", default=None, help="Plan date (IST)")
    parser.add_argument("--interval", type=int, default=60,
                        help="Seconds between open-position checks")
    parser.add_argument("--max-seconds", type=int, default=6 * 3600)
    parser.add_argument("--no-plan", action="store_true",
                        help="Skip reading a plan (management only)")
    args = parser.parse_args()

    date = args.date or today_ist_str()
    cfg = load_config()

    plan = None if args.no_plan else load_plan(date)
    if plan is None and not args.no_plan:
        logger.warning("No plan for %s — session will MANAGE only "
                       "(no new entries).", date)

    # Session uses the plan's symbols so position data can be fetched.
    symbols = list(plan.get("symbols", [])) if plan else []
    if not symbols and plan is not None:
        symbols = [c.get("symbol") for c in plan.get("candidates", [])]
    if not symbols:
        # No plan: still square off/hold whatever MegaBull holds.
        symbols = []

    trader = PaperTrader(
        symbols=symbols or None,
        timeframe="15m",
        initial_capital=(cfg.get("risk_management", {}) or {}).get("capital"),
    )
    logger.info("Session job: plan=%s candidates=%d",
                date, len((plan or {}).get("candidates", [])))

    # Startup "Good morning" Telegram: tells Mr Kapil Kuhire the job woke up,
    # which plan it loaded, and the broker mirror status — sent BEFORE the
    # trading loop starts so a silent boot is never mistaken for a dead bot.
    try:
        from src.alerts import send_telegram
        _cand_lines = []
        for _c in (plan or {}).get("candidates", []) or []:
            _cand_lines.append(
                f"{_c.get('symbol')} {_c.get('action')} "
                f"@{_c.get('entry_price')} SL {_c.get('stop_loss')} "
                f"TP {_c.get('target')} qty {_c.get('position_size')}")
        _msg = ["☀️ *Good morning, Mr Kapil Kuhire Sir!*",
                f"TRIO session started — {date}.",
                f"Plan: {len((plan or {}).get('candidates', []))} candidate(s)."]
        if _cand_lines:
            _msg.append("\n".join(_cand_lines))
        else:
            _msg.append("No candidates in today's plan — manage-only mode.")
        send_telegram("\n".join(_msg))
    except Exception as exc:
        logger.warning("Startup good-morning alert failed: %s", exc)

    # run_session places the plan, restores SL/TP state, and loops guards.
    try:
        trader.run_session(plan=plan, interval_seconds=args.interval,
                           max_seconds=args.max_seconds)
    finally:
        if plan is not None and not plan.get("executed"):
            mark_executed(plan, [])
        logger.info("Session job finished at %s",
                    datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"))


if __name__ == "__main__":
    main()