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

    # Load today's plan; if missing fall back to the most recent one so the
    # session never sits idle — exactly like the manual daily scans you ran
    # before automation. Yesterday's plan's signals are still valid for today
    # (they were ranked on yesterday's close), and the live fallback also
    # refreshes them from today's bars.
    plan = None if args.no_plan else load_plan(date)
    if plan is None and not args.no_plan:
        from src.plan import find_latest_plan

        latest = find_latest_plan()
        if latest is not None:
            latest_date = latest.get("date", "?")
            logger.warning(
                "No plan for %s — using latest plan (%s) with %d candidate(s).",
                date, latest_date, len(latest.get("candidates", [])))
            plan = latest
            # Also build a fresh scan for today in parallel — the session
            # trades the latest (yesterday) plan now and the fresh plan's
            # candidates will be available next loop after _save_plan.
            try:
                from scripts.premarket import build_plan
                from src.plan import save_plan as _save_plan
                cfg_universe = cfg.get("universe", {}).get("premarket", "nifty100")
                fresh = build_plan(cfg_universe, "1d",
                                   cfg.get("screener", {}).get("max_positions_to_open", 3))
                fresh["date"] = date
                _save_plan(fresh, date=date)
                logger.info("Fresh today's plan also built: %d candidate(s) for %s.",
                            len(fresh.get("candidates", [])), date)
                # Trade the union but dedupe by symbol — fresh intraday scan
                # wins when the same symbol appears with a newer signal.
                seen = {c.get("symbol") for c in fresh.get("candidates", [])}
                merged = list(fresh.get("candidates", []))
                for c in latest.get("candidates", []) or []:
                    if c.get("symbol") not in seen:
                        merged.append(c)
                # Keep ranking order and cap to top_n
                top_n = cfg.get("screener", {}).get("max_positions_to_open", 3)
                merged.sort(key=lambda x: x.get("rank", 0), reverse=True)
                plan = dict(fresh)
                plan["candidates"] = merged[: top_n * 2]  # small headroom for union
                plan["merged_from"] = latest_date
            except Exception as exc:
                logger.warning("Fresh today's scan failed: %s — trading latest plan only.", exc)
        else:
            # No plan at all on disk — build today's from scratch so the
            # session never sits idle, same as the manual scans we did before.
            logger.warning("No plan for %s and no prior plan — running live scan.",
                           date)
            try:
                from scripts.premarket import build_plan
                from src.plan import save_plan as _save_plan
                cfg_universe = cfg.get("universe", {}).get("premarket", "nifty100")
                plan = build_plan(cfg_universe, "1d",
                                  cfg.get("screener", {}).get("max_positions_to_open", 3))
                plan["date"] = date
                _save_plan(plan, date=date)
                logger.info("Live fallback plan built: %d candidate(s) for %s.",
                            len(plan.get("candidates", [])), date)
            except Exception as exc:
                logger.error("Live fallback scan failed: %s — manage-only.", exc)
                plan = None

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

    # Options paper overlay (shadow until proven): place spreads for
    # candidates carrying option_legs, on the options paper broker only.
    # Equity flow below is untouched. Spreads are marked to the live chain
    # mid on every guard tick and squared off at EOD alongside equities.
    opt_broker = None
    try:
        opt_cfg = cfg.get("options", {}) or {}
        if opt_cfg.get("enabled", True) and plan is not None and any(
                (c.get("option_legs") or {}).get("strategy") not in
                (None, "", "none") for c in plan.get("candidates", []) or []):
            from src.broker.options_paper import OptionsPaperBroker
            opt_broker = OptionsPaperBroker(
                initial_capital=(cfg.get("risk_management", {}) or {}).get(
                    "capital", 500000),
                lot_size=int(opt_cfg.get("lot_size", 50)))
            trader.execute_option_plan(plan, opt_broker=opt_broker)
            # expose to the guard loop below
            trader.opt_broker = opt_broker
    except Exception as exc:
        logger.warning("Options paper overlay failed: %s", exc)

    # run_session places the plan, restores SL/TP state, and loops guards.
    # The options overlay marks live-chain mid every tick and EOD-squares
    # alongside equities (see _manage_options below) so spreads never sit
    # open past 15:10 IST.
    try:
        trader.run_session(plan=plan, interval_seconds=args.interval,
                           max_seconds=args.max_seconds,
                           tick_hook=(lambda: _manage_options(
                               trader, opt_broker, cfg)))
    finally:
        try:
            _squareoff_options(opt_broker, "eod-squareoff")
        except Exception as exc:
            logger.warning("Options EOD square-off failed: %s", exc)
        if plan is not None and not plan.get("executed"):
            mark_executed(plan, [])
        logger.info("Session job finished at %s",
                    datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"))


def _chain_mid_for(spread_id: str, legs: list, expiry: str,
                   underlying: str = "NIFTY",
                   hv_proxy: float = 0.20) -> Optional[float]:
    """Net mid for one spread from the live chain (fallback BS sim)."""
    try:
        from src.options_chain import fetch_chain
        from src.options_pricing import bs_price
        from datetime import datetime, timezone as _tz
        chain = fetch_chain(underlying)
        if chain is None:
            return None
        rows = {r["strike"]: r for r in
                (chain.get("by_expiry", {}).get(expiry, []) or [])}
        if not rows:
            return None
        try:
            days = max((datetime.strptime(expiry, "%d-%b-%Y").replace(
                tzinfo=_tz.utc) - datetime.now(_tz.utc)).days, 1)
        except ValueError:
            days = 7
        t_yrs = days / 365.0
        spot = float(chain.get("underlying") or 0.0)
        net = 0.0
        for leg in legs or []:
            row = rows.get(float(leg.get("strike") or 0))
            if row is None:
                return None
            q = (row.get(leg.get("kind", "CE")) or {})
            bid = float(q.get("bidPrice") or 0.0)
            ask = float(q.get("askPrice") or 0.0)
            if bid > 0 and ask > 0:
                px = (bid + ask) / 2.0
            elif float(q.get("lastPrice") or 0.0) > 0:
                px = float(q.get("lastPrice"))
            else:
                sigma = max(float(q.get("iv") or 0.0) / 100.0, hv_proxy)
                px = bs_price(spot, float(leg.get("strike") or 0.0),
                              t_yrs, sigma, leg.get("kind", "CE"))
            net += px if leg.get("side") == "BUY" else -px
        return round(max(net, 0.0), 2)
    except Exception:
        return None


def _manage_options(trader, opt_broker, cfg) -> None:
    """Mark every open spread to live-chain mid (theta captured intraday)."""
    if opt_broker is None:
        return
    try:
        positions = list(getattr(opt_broker, "positions", {}).keys())
    except Exception:
        return
    for sid in positions:
        try:
            detail = (getattr(opt_broker, "spreads", {}) or {}).get(sid) or {}
            legs = detail.get("legs") or []
            expiry = detail.get("expiry", "")
            mid = _chain_mid_for(sid, legs, expiry)
            if mid is not None:
                opt_broker.mark_spread(sid, mid)
        except Exception:
            continue


def _squareoff_options(opt_broker, reason: str = "eod-squareoff") -> int:
    """Close every open spread at live mid. Returns closed count."""
    if opt_broker is None:
        return 0
    n = 0
    try:
        sids = list(getattr(opt_broker, "positions", {}).keys())
    except Exception:
        return 0
    for sid in sids:
        try:
            detail = (getattr(opt_broker, "spreads", {}) or {}).get(sid) or {}
            legs = detail.get("legs") or []
            mid = _chain_mid_for(sid, legs, detail.get("expiry", ""))
            if mid is None:
                # stale chain: close at last mark
                pos = opt_broker.positions.get(sid)
                mid = float(pos.current_price or pos.avg_price or 0.0)
            rec = opt_broker.close_spread(sid, mid, reason)
            if rec:
                n += 1
        except Exception:
            continue
    return n


if __name__ == "__main__":
    main()