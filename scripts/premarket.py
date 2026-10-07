"""
TRIO — Premarket job (GitHub Actions)
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

Run after the NSE close (cron), Mon–Fri. Ranks the configured universe on
daily bars, writes output/plans/YYYY-MM-DD.json for the next session, and
sends a Telegram summary. The session job reads that plan at the open.

    python scripts/premarket.py [--universe nifty100] [--timeframe 1d]
                                [--top-n 3] [--no-alert]

DISCLAIMER: Educational purposes only. Not financial advice.
"""

import argparse
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.utils import load_env, load_config, utc_now, get_logger  # noqa: E402
from src.screener import screen, resolve_basket  # noqa: E402
from src.plan import save_plan, today_ist_str  # noqa: E402

load_env()
logger = get_logger("premarket")


def build_plan(universe: str, timeframe: str, top_n: int) -> Dict[str, Any]:
    """Run the daily screen and return the plan payload."""
    cfg = load_config()
    sc_cfg = cfg.get("screener", {})
    symbols = resolve_basket(universe)

    candidates = screen(symbols=symbols, timeframe=timeframe, top_n=top_n)
    cand_dicts = [c.to_dict() for c in candidates]

    return {
        "date": today_ist_str(),
        "generated_at": utc_now(),
        "universe": universe,
        "timeframe": timeframe,
        "scanned": len(symbols),
        "failed": max(0, len(symbols) - 0),  # screen logs real count
        "top_n": top_n,
        "min_rank": sc_cfg.get("min_rank", 40.0),
        "min_confidence": sc_cfg.get("min_confidence", 60),
        "candidates": cand_dicts,
        "executed": False,
    }


def format_summary(plan: Dict[str, Any]) -> str:
    """Phone-friendly Telegram summary of the plan."""
    lines = [f"*TRIO premarket plan — {plan['date']}*",
             f"Universe: {plan['universe']} ({plan['scanned']} scanned) "
             f"on {plan['timeframe']}"]
    cands = plan.get("candidates", [])
    if not cands:
        lines.append("No setups met the bar today — no trades planned.")
        return "\n".join(lines)
    lines.append(f"{len(cands)} candidate(s), best first:")
    for c in cands:
        lines.append(
            f"• {c.get('symbol')} {c.get('action')} "
            f"entry {c.get('entry_price')} stop {c.get('stop_loss')} "
            f"target {c.get('target')} qty {c.get('position_size')} "
            f"conf {c.get('confidence')} rank {c.get('rank')} "
            f"({c.get('setup_name')})")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="TRIO premarket plan builder")
    parser.add_argument("--universe", default=None,
                        help="Basket name (nifty100) or comma list; "
                             "defaults to config universe.premarket")
    parser.add_argument("--timeframe", default="1d")
    parser.add_argument("--top-n", type=int, default=None)
    parser.add_argument("--no-alert", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    universe = args.universe or cfg.get("universe", {}).get(
        "premarket", "nifty100")
    top_n = args.top_n or cfg.get("screener", {}).get(
        "max_positions_to_open", 3)

    logger.info("Premarket scan: universe=%s timeframe=%s top_n=%d",
                universe, args.timeframe, top_n)
    plan = build_plan(universe, args.timeframe, top_n)
    path = save_plan(plan)
    print(f"Plan written: {path}")
    print(format_summary(plan))

    if not args.no_alert:
        try:
            from src.alerts import send_telegram
            send_telegram(format_summary(plan))
        except Exception as exc:
            logger.warning("Premarket Telegram summary failed: %s", exc)


if __name__ == "__main__":
    main()