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
from typing import Optional

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
    parser.add_argument("--allow-stale-plan", action="store_true",
                        help="Allow stale-plan fallback (default: management-only)")
    args = parser.parse_args()

    date = args.date or today_ist_str()
    cfg = load_config()

    plan = None if args.no_plan else load_plan(date)
    if plan is not None:
        # Use today's plan as-is (validated on its own screen).
        pass
    elif args.no_plan:
        plan = None
    elif args.allow_stale_plan:
        # Explicit flag required: try stale -> fresh union or build from scratch.
        from src.plan import find_latest_plan
        latest = find_latest_plan()
        if latest is not None:
            latest_date = latest.get("date", "?")
            logger.warning(
                "No plan for %s — stale allowed, using latest (%s).",
                date, latest_date)
            # Validate staleness + price deviation per candidate before merging.
            try:
                from src.data_fetcher import fetch_market_data
                safe_latest: List[Dict[str, Any]] = []
                for _c in (latest.get("candidates", []) or []):
                    lp = None
                    try:
                        lp = fetch_market_data(
                            _c.get("symbol", ""), "1d").latest_price
                    except Exception:
                        pass
                    r = None
                    try:
                        from src.plan import validate_stale
                        r = validate_stale(_c, date, live_price=lp)
                    except Exception:
                        r = None
                    if r is None:
                        safe_latest.append(_c)
                    else:
                        logger.warning("Stale %s rejected: %s",
                                       _c.get("symbol"), r)
                latest_safe = {**latest, "candidates": safe_latest}
                if not safe_latest:
                    logger.warning("All stale candidates rejected — not merging.")
                try:
                    from scripts.premarket import build_plan
                    from src.plan import save_plan as _save_plan
                    cfg_universe = cfg.get("universe", {}).get("premarket", "nifty100")
                    fresh = build_plan(cfg_universe, "1d",
                                       cfg.get("screener", {}).get("max_positions_to_open", 3))
                    fresh["date"] = date
                    _save_plan(fresh, date=date)
                    logger.info("Fresh plan also built: %d candidate(s) for %s.",
                                len(fresh.get("candidates", [])), date)
                    seen = {c.get("symbol") for c in fresh.get("candidates", [])}
                    merged = list(fresh.get("candidates", []))
                    for c in latest_safe.get("candidates", []) or []:
                        if c.get("symbol") not in seen:
                            merged.append(c)
                    top_n = cfg.get("screener", {}).get("max_positions_to_open", 3)
                    merged.sort(key=lambda x: x.get("rank", 0), reverse=True)
                    plan = dict(fresh)
                    plan["candidates"] = merged[: top_n * 2]
                    if safe_latest:
                        plan["merged_from"] = latest_date
                except Exception as exc:
                    logger.warning("Fresh scan failed: %s — trading stale only.", exc)
                    plan = latest_safe or None
            except Exception as exc:
                logger.warning("Stale fallback failed: %s — manage-only.", exc)
                plan = None
        else:
            logger.warning("No plan for %s and no prior plan — running live scan.", date)
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
    else:
        # Default: no today's plan -> build a fresh one; _never_ trade stale
        # automatically. Without --allow-stale-plan the runner is management-only
        # until a fresh plan arrives.
        logger.warning("No plan for %s — no stale trading without "
                       "--allow-stale-plan; building a fresh plan.", date)
        try:
            from scripts.premarket import build_plan
            from src.plan import save_plan as _save_plan
            cfg_universe = cfg.get("universe", {}).get("premarket", "nifty100")
            plan = build_plan(cfg_universe, "1d",
                               cfg.get("screener", {}).get("max_positions_to_open", 3))
            plan["date"] = date
            _save_plan(plan, date=date)
            logger.info("Fresh plan built: %d candidate(s) for %s.",
                        len(plan.get("candidates", [])), date)
        except Exception as exc:
            logger.error("Fresh plan build failed: %s — manage-only.", exc)
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

    # Check for /start subscribers on BOTH bots so intraday + options
    # broadcasts are always reachable — stale per-bot offset is isolated.
    try:
        from src.alerts import handle_all_joins
        _new = handle_all_joins()
        if _new:
            logger.info("Found %d new Telegram subscriber(s) at session boot.", len(_new))
    except Exception as exc:
        logger.warning("Startup subscriber check failed: %s", exc)

    # Startup "Good morning" Telegram: tells Mr Kapil Kuhire the job woke up,
    # which plan it loaded, which broker platforms are selected, and that it is
    # waiting for the 09:15 NSE market open before firing entries.
    try:
        from src.alerts import send_telegram
        _cand_lines = []
        _opt_lines = []
        for _c in (plan or {}).get("candidates", []) or []:
            _cand_lines.append(
                f"{_c.get('symbol')} {_c.get('action')} "
                f"@{_c.get('entry_price')} SL {_c.get('stop_loss')} "
                f"TP {_c.get('target')} qty {_c.get('position_size')}")
            _opt = _c.get("option_legs") or {}
            if _opt.get("strategy") and _opt.get("strategy") != "none":
                _legs = " + ".join(
                    f"{_l.get('side')} {_l.get('strike')}{_l.get('kind')}"
                    f"@{_l.get('premium')}"
                    for _l in (_opt.get("legs") or []))
                _opt_lines.append(
                    f"  + {_c.get('symbol')}: {_opt.get('strategy')} {_legs} "
                    f"debit {_opt.get('net_debit')} "
                    f"maxLoss {_opt.get('maxLoss')}")

        eq_prov = (cfg.get("broker", {}) or {}).get("provider", "megabull").upper()
        opt_prov = (cfg.get("options", {}) or {}).get("broker", "megabull").upper()

        _msg = [
            "☀️ *Good morning, Mr Kapil Kuhire Sir!*",
            "TRIO session started at 09:10 IST — *Waiting for market open (09:15 IST)*.",
            "",
            f"📍 *Platforms targeted today:*",
            f"• *Equity Intraday:* `{eq_prov}` (MIS paper fills)",
            f"• *Index Options:* `{opt_prov}` (with instant local-sim shadow fallback)",
            "",
            f"📋 *Today's Plan ({date}):* {len((plan or {}).get('candidates', []))} candidate(s)"
        ]
        # Split deliver: equity -> old bot, options -> new bot (8877508167).
        # When options legs are empty the options bot gets a short "no spreads
        # — waiting for chain" note so you know it woke up too.
        _equity_msg = list(_msg)
        _options_msg = list(_msg)
        if _cand_lines:
            _equity_msg.append("\n*Equity Intraday Candidates:*")
            _equity_msg.append("\n".join(_cand_lines))
        if _opt_lines:
            _options_msg.append("\n*NIFTY Options Hedging Spreads:*")
            _options_msg.append("\n".join(_opt_lines))
        else:
            _options_msg.append("\n*NIFTY Options:* no spreads in today's plan "
                                "(chain/expiry pending — live re-scans will fill in).")
        if not _cand_lines and not _opt_lines:
            _equity_msg.append("\nNo equity candidates — manage-only mode.")
        send_telegram("\n".join(_equity_msg))
        from src.alerts import send_options_telegram as _sopt
        _sopt("\n".join(_options_msg))
    except Exception as exc:
        logger.warning("Startup good-morning alert failed: %s", exc)

    # Options overlay (shadow until proven): place spreads for candidates
    # carrying option_legs. Broker comes from config `options.broker`:
    # "paper" (default) keeps the local options_paper sim; "megabull"
    # routes both legs to the MegaBull remote paper account so the user
    # can SEE the NIFTY spread in the MegaBull app. Equity flow below is
    # untouched either way. Until MegaBull accepts option orders (their
    # buysell rejected every lots/units variant on 2026-10-08) the
    # MegaBull leg failures skip SILENTLY — the local paper sim still
    # books the P&L, so the overlay never blocks the equity session.
    # Spreads are marked to the live chain mid on every guard tick and
    # squared off at EOD alongside equities.
    opt_broker = None
    try:
        opt_cfg = cfg.get("options", {}) or {}
        if opt_cfg.get("enabled", True) and plan is not None and any(
                (c.get("option_legs") or {}).get("strategy") not in
                (None, "", "none") for c in plan.get("candidates", []) or []):
            which = str(opt_cfg.get("broker", "paper")).strip().lower()
            opt_lot = int(opt_cfg.get("lot_size", 50))
            if which == "megabull":
                try:
                    from src.broker.megabull_options import (
                        MegaBullOptionsBroker)
                    eq = getattr(trader, "broker", None)
                    if "megabull" not in str(
                            getattr(eq, "provider_name", "")).lower():
                        raise RuntimeError(
                            "equity broker is not MegaBull; remote option "
                            "legs need the MegaBull account")
                    opt_broker = MegaBullOptionsBroker(eq, lot_size=opt_lot)
                    logger.info("Options broker: MegaBull remote paper.")
                except Exception as exc:
                    logger.warning(
                        "MegaBull options broker unavailable (%s) — "
                        "falling back to local paper sim.", exc)
                    opt_broker = None
            if opt_broker is None:
                from src.broker.options_paper import OptionsPaperBroker
                opt_broker = OptionsPaperBroker(
                    initial_capital=(cfg.get("risk_management", {}) or {}).get(
                        "capital", 500000),
                    lot_size=opt_lot)
                if which == "megabull":
                    logger.info("Options broker: local paper sim "
                                "(MegaBull legs rejected — shadow only).")
            placed_opt = trader.execute_option_plan(plan,
                                                      opt_broker=opt_broker)
            # expose to the guard loop below
            trader.opt_broker = opt_broker
            # Loud routing verdict for Telegram: the user asked whether
            # MegaBull really holds the NIFTY spread, so say WHERE each
            # spread landed — remote (with the MegaBull umbrella order id)
            # or local shadow (with the refusal reason). Never silent.
            try:
                from src.alerts import send_telegram as _tg
                _opt_status = []
                for _r in placed_opt or []:
                    _venue = str(_r.get("venue", ""))
                    if _r.get("shadow") or "paper" in _venue.lower():
                        _opt_status.append(
                            f"🧪 {_r.get('symbol')}: "
                            f"{_r.get('strategy')} shadow LOCAL SIM "
                            f"(MegaBull refused legs) "
                            f"debit {_r.get('net_debit')} lots "
                            f"{_r.get('lots')}")
                    else:
                        _opt_status.append(
                            f"🎯 {_r.get('symbol')}: "
                            f"{_r.get('strategy')} ON MEGABULL PAPER "
                            f"{_r.get('order_id')} debit "
                            f"{_r.get('net_debit')} lots {_r.get('lots')} "
                            f"— check the MegaBull app")
                if _opt_status:
                    try:
                        from src.alerts import send_options_telegram as _otg
                        _otg("*Options routing — where the NIFTY spread sits (8877508167):*\n"
                             + "\n".join(_opt_status))
                    except Exception:
                        _tg("*Options routing — where the NIFTY spread sits:*\n"
                            + "\n".join(_opt_status))
            except Exception as _tg_exc:
                logger.warning("Options routing alert failed: %s", _tg_exc)
    except Exception as exc:
        logger.warning("Options overlay failed: %s", exc)

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
            _squareoff_options(opt_broker, "eod-squareoff", trader=trader)
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
    """Mark every open spread to live-chain mid (theta captured intraday).

    Includes the local shadow book (kept when MegaBull legs reject) so
    shadow spreads track the same mid and square off at EOD with the
    rest — one mark loop, both books.
    """
    brokers = [b for b in [opt_broker] +
               list(getattr(trader, "_opt_shadows", []) or [])
               if b is not None]
    # _opt_shadows holds (broker, order) tuples; unwrap to brokers.
    flat = []
    for b in brokers:
        flat.append(b[0] if isinstance(b, tuple) else b)
    for br in dict.fromkeys(flat):
        try:
            positions = list(getattr(br, "positions", {}).keys())
        except Exception:
            continue
        for sid in positions:
            try:
                detail = (getattr(br, "spreads", {}) or {}).get(sid) or {}
                legs = detail.get("legs") or []
                expiry = detail.get("expiry", "")
                mid = _chain_mid_for(sid, legs, expiry)
                if mid is not None:
                    br.mark_spread(sid, mid)
            except Exception:
                continue


def _squareoff_options(opt_broker, reason: str = "eod-squareoff",
                       trader=None) -> int:
    """Close every open spread at live mid. Returns closed count.

    Every close is ALSO appended to the shared trade ledger via
    ``trader._log_event("close", rec)`` (plus the CSV mirror) so options
    P&L shows in the evening digest alongside equities. Without this the
    options broker's private ``closed_trades`` list dies with the runner.
    """
    if opt_broker is None:
        return 0
    n = 0
    try:
        sids = list(getattr(opt_broker, "positions", {}).keys())
    except Exception:
        return 0
    # main book plus local shadows (MegaBull legs rejected)
    brokers = [opt_broker]
    if trader is not None:
        for item in list(getattr(trader, "_opt_shadows", []) or []):
            brokers.append(item[0] if isinstance(item, tuple) else item)
    sids_all = []
    for br in brokers:
        if br is None:
            continue
        try:
            for sid in list(getattr(br, "positions", {}).keys()):
                sids_all.append((br, sid))
        except Exception:
            continue
    for br, sid in sids_all:
        try:
            detail = (getattr(br, "spreads", {}) or {}).get(sid) or {}
            legs = detail.get("legs") or []
            mid = _chain_mid_for(sid, legs, detail.get("expiry", ""))
            if mid is None:
                # stale chain: close at last mark
                pos = br.positions.get(sid)
                mid = float(pos.current_price or pos.avg_price or 0.0)
            rec = br.close_spread(sid, mid, reason)
            if rec:
                n += 1
                if trader is not None:
                    try:
                        trader._log_event("close", rec)
                    except Exception:
                        pass
        except Exception:
            continue
    if trader is not None and n:
        try:
            trader._write_trade_log_csv()
        except Exception:
            pass
    return n


if __name__ == "__main__":
    main()