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
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.utils import load_env, load_config, get_logger  # noqa: E402
from src.paper_trader import PaperTrader  # noqa: E402
from src.plan import (load_plan, today_ist_str, mark_executed,
                      validate_plan_identity)  # noqa: E402
from src.options_intraday import (  # noqa: E402
    COMBINED,
    combined_mode_enabled,
    options_intraday_enabled,
    resolve_trading_mode,
    strict_options_enabled,
)

env_path = Path(r"C:\Users\Kapil Kuhire\Downloads\TRIO_IMPLEMENTATION\.env")
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, val = line.split("=", 1)
            os.environ[key.strip()] = val.strip().strip('"').strip("'")
    env_path = Path(r"C:\Users\Kapil Kuhire\Downloads\TRIO_IMPLEMENTATION\.env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, val = line.split("=", 1)
                os.environ[key.strip()] = val.strip().strip('"').strip("'")
    load_env()
    os.environ["TRIO_BROKER_PROVIDER"] = "megabull"
    os.environ["PAPER_TRADING"] = "true"
    os.environ["TRIO_REMOTE_REQUIRED"] = "true"
logger = get_logger("session")

IST = timezone(timedelta(hours=5, minutes=30))


def _refresh_strict_options_subplan(plan: Optional[Dict[str, Any]],
                                    cfg: Dict[str, Any],
                                    session_date: str,
                                    *, combined: bool) -> Optional[Dict[str, Any]]:
    """Rebuild only the strict options leg from current completed bars/quotes.

    Combined mode retains its already-generated equity candidates.  The
    nested options plan is always rebuilt when it is a premarket preview or
    belongs to another session, so a stale option quote cannot be routed.
    """
    if plan is None:
        return plan
    if combined:
        nested = plan.get("options_intraday_plan")
        needs_refresh = (not isinstance(nested, dict) or
                         nested.get("execution_ready") is False or
                         str(nested.get("date", "")) != str(session_date))
        if needs_refresh:
            from src.options_intraday_plan import build_intraday_plan
            refreshed = build_intraday_plan(cfg)
            refreshed["date"] = session_date
            plan["options_intraday_plan"] = refreshed
        return plan
    if str(plan.get("trading_mode", "")).strip().lower() == "options_intraday":
        if plan.get("execution_ready") is False or str(plan.get("date", "")) != str(session_date):
            from src.options_intraday_plan import build_intraday_plan
            refreshed = build_intraday_plan(cfg)
            refreshed["date"] = session_date
            return refreshed
    return plan


def _run_options_only_session(cfg: Dict[str, Any], trading_mode: str,
                              *, interval_seconds: int,
                              max_seconds: Optional[int],
                              management_only: bool) -> None:
    """Start the strict live runtime without reading or creating any plan."""
    trader = PaperTrader(
        timeframe="15m",
        initial_capital=(cfg.get("risk_management", {}) or {}).get("capital"),
        trading_mode=trading_mode,
    )
    try:
        from src.alerts import handle_all_joins, send_options_telegram
        handle_all_joins()
        status = ("Management only; entry scans disabled." if management_only else
                  "Fresh live scans start at the first entry-eligible tick.")
        send_options_telegram("TRIO options_intraday session started. " + status)
    except Exception as exc:
        logger.warning("Options session startup alert failed: %s", exc)

    try:
        trader.ensure_option_broker(cfg)
        trader.run_session(
            plan=None, interval_seconds=interval_seconds,
            max_seconds=max_seconds, management_only=management_only)
    finally:
        try:
            trader._get_options_runtime(cfg).finalize()
        except Exception as exc:
            logger.critical("Options intraday finalization unresolved: %s", exc)
            trader._options_unresolved_exits = {"strict_options": str(exc)}
            raise RuntimeError(
                "strict options session failed: remote position was not confirmed flat"
            ) from exc
        finally:
            logger.info("Options session finished at %s",
                        datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"))


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
    parser.add_argument("--trading-mode", default=None,
                        help="production (default), combined, or options_intraday")
    args = parser.parse_args()

    date = args.date or today_ist_str()
    cfg = load_config()
    trading_mode = resolve_trading_mode(cfg, args.trading_mode)
    options_only = options_intraday_enabled(cfg, trading_mode)
    combined = combined_mode_enabled(cfg, trading_mode)
    strict_options = strict_options_enabled(cfg, trading_mode)
    current_session_date = today_ist_str()

    # An explicit historical date is useful for research, but it must never
    # become a production entry session.  Management/finalization can still
    # run with no plan so an existing remote position is not abandoned.
    if strict_options and date != current_session_date:
        logger.error(
            "Strict options session date %s is not current IST session %s; "
            "running management-only.", date, current_session_date)
        args.no_plan = True

    # The stale-plan flag is intentionally incompatible with strict options.
    # A previous day's candidates, strikes, expiries, or quotes must never be
    # used as today's options instructions, even when the operator supplied an
    # explicit fallback flag.
    if strict_options and args.allow_stale_plan:
        logger.warning(
            "Ignoring --allow-stale-plan in strict options mode; "
            "options require fresh current-session data.")
        args.allow_stale_plan = False

    if options_only:
        _run_options_only_session(
            cfg, trading_mode, interval_seconds=args.interval,
            max_seconds=args.max_seconds, management_only=args.no_plan)
        return

    plan = None if args.no_plan else load_plan(date)
    if plan is not None:
        identity_reason = validate_plan_identity(
            plan, expected_date=date, current_date=current_session_date,
            require_current=strict_options)
        if identity_reason:
            logger.error("Executable plan rejected: %s", identity_reason)
            plan = None
    if combined and plan is not None and plan.get("trading_mode") != COMBINED:
        logger.info("Ignoring non-combined plan for %s; rebuilding equity + "
                    "strict-options plan.", date)
        plan = None
    if options_only and plan is not None and (
            plan.get("execution_ready") is False or
            plan.get("trading_mode") != "options_intraday"):
        logger.info("Ignoring non-executable options preview for %s; "
                    "building a same-session intraday plan.", date)
        plan = None
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
                        r = validate_stale(
                            _c, date, live_price=lp,
                            source_date=str(latest.get("date") or ""))
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
                                       cfg.get("screener", {}).get("max_positions_to_open", 3),
                                       trading_mode=trading_mode)
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
                                   cfg.get("screener", {}).get("max_positions_to_open", 3),
                                   trading_mode=trading_mode)
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
                               cfg.get("screener", {}).get("max_positions_to_open", 3),
                               trading_mode=trading_mode)
            plan["date"] = date
            _save_plan(plan, date=date)
            logger.info("Fresh plan built: %d candidate(s) for %s.",
                        len(plan.get("candidates", [])), date)
        except Exception as exc:
            logger.error("Fresh plan build failed: %s — manage-only.", exc)
            plan = None

    if strict_options and plan is not None and not args.no_plan:
        try:
            plan = _refresh_strict_options_subplan(
                plan, cfg, date, combined=combined)
        except Exception as exc:
            logger.error("Strict options plan refresh failed: %s", exc)
            if options_only:
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
        trading_mode=trading_mode,
    )
    logger.info("Session job: plan=%s candidates=%d",
                date, len((plan or {}).get("candidates", [])))
    unresolved_failure = None

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
        _opt_rejection_lines = []
        for _c in (plan or {}).get("candidates", []) or []:
            _cand_lines.append(
                f"{_c.get('symbol')} {_c.get('action')} "
                f"@{_c.get('entry_price')} SL {_c.get('stop_loss')} "
                f"TP {_c.get('target')} qty {_c.get('position_size')}")
            _opt = _c.get("option_legs") or {}
            _diag = _c.get("option_diagnostic") or {}
            if _opt.get("strategy") in (None, "", "none") and _diag:
                _reasons = _diag.get("rejection_reasons") or _opt.get("reasoning") or [
                    "unspecified_option_rejection"]
                _opt_rejection_lines.append(
                    f"  • {_c.get('symbol')}: "
                    f"stage={_diag.get('selection_stage', 'contract_selection')} "
                    f"reason={'; '.join(map(str, _reasons))}")
                logger.warning(
                    "Options candidate rejected: symbol=%s stage=%s reasons=%s",
                    _c.get("symbol"),
                    _diag.get("selection_stage", "contract_selection"),
                    _reasons,
                )
            if _opt.get("strategy") and _opt.get("strategy") != "none":
                _legs = " + ".join(
                    f"{_l.get('side')} {_l.get('strike')}{_l.get('kind')}"
                    f"@{_l.get('premium')}"
                    for _l in (_opt.get("legs") or []))
                _opt_lines.append(
                    f"  + {_c.get('symbol')}: {_opt.get('strategy')} {_legs} "
                    f"debit {_opt.get('net_debit')} "
                    f"maxLoss {_opt.get('maxLoss')}")
        _primary_symbols = {c.get("symbol") for c in
                            (plan or {}).get("candidates", []) or []}
        for _c in (plan or {}).get("option_candidates", []) or []:
            if _c.get("symbol") in _primary_symbols:
                continue
            _opt = _c.get("option_legs") or {}
            _diag = _c.get("option_diagnostic") or {}
            if _opt.get("strategy") in (None, "", "none"):
                _reasons = _diag.get("rejection_reasons") or _opt.get("reasoning") or [
                    "unspecified_option_rejection"]
                _opt_rejection_lines.append(
                    f"  • {_c.get('symbol')}: "
                    f"stage={_diag.get('selection_stage', 'contract_selection')} "
                    f"reason={'; '.join(map(str, _reasons))}")
                logger.warning(
                    "Options candidate rejected: symbol=%s stage=%s reasons=%s",
                    _c.get("symbol"),
                    _diag.get("selection_stage", "contract_selection"),
                    _reasons,
                )
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

        _option_route = ("remote paper only; local shadow disabled"
                         if strict_options else
                         "with instant local-sim shadow fallback")
        _msg = [
            "☀️ *Good morning, Mr Kapil Kuhire Sir!*",
            "TRIO session started at 09:10 IST — *Waiting for market open (09:15 IST)*.",
            "",
            f"📍 *Platforms targeted today:*",
            f"• *Equity Intraday:* `{eq_prov}` (MIS paper fills)",
            f"• *Index Options:* `{opt_prov}` ({_option_route})",
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
            if _opt_rejection_lines:
                _options_msg.append("\n*Option candidate diagnostics:*")
                _options_msg.extend(_opt_rejection_lines[:8])
        if not _cand_lines and not _opt_lines:
            _equity_msg.append("\nNo equity candidates — manage-only mode.")
        send_telegram("\n".join(_equity_msg))
        from src.alerts import send_options_telegram as _sopt
        _sopt("\n".join(_options_msg))
    except Exception as exc:
        logger.warning("Startup good-morning alert failed: %s", exc)

    # The ordinary production path retains its existing optional options
    # overlay.  The explicit options_intraday path only attaches the strict
    # remote broker here; PaperTrader.run_session owns its lifecycle runtime
    # and never invokes the local shadow overlay.
    opt_broker = None
    try:
        opt_cfg = cfg.get("options", {}) or {}
        option_candidates = (plan or {}).get("option_candidates")
        if option_candidates is None:
            option_candidates = (plan or {}).get("candidates", []) or []
        if strict_options:
            # Strict modes attach the remote options broker even when the
            # opening plan is empty so a later live rescan can execute.
            opt_broker = trader.ensure_option_broker(cfg)
        elif opt_cfg.get("enabled", True) and plan is not None and any(
                (c.get("option_legs") or {}).get("strategy") not in
                (None, "", "none") for c in option_candidates):
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
        if options_only:
            trader.opt_broker = opt_broker
            logger.info("options_intraday: remote paper options only; "
                        "equity entry route disabled; runtime owns execution.")
        elif combined:
            trader.opt_broker = opt_broker
            logger.info("combined: MegaBull equity intraday plus strict "
                        "remote-paper options; legacy overlay disabled.")
    except Exception as exc:
        logger.warning("Options overlay failed: %s", exc)

    # run_session places the plan, restores SL/TP state, and loops guards.
    # The options overlay marks live-chain mid every tick and EOD-squares
    # alongside equities (see _manage_options below) so spreads never sit
    # open past 15:10 IST.
    try:
        trader.run_session(plan=plan, interval_seconds=args.interval,
                           max_seconds=args.max_seconds,
                           tick_hook=(None if strict_options else
                                      (lambda: _manage_options(
                                          trader, opt_broker, cfg))))
    finally:
        if strict_options:
            try:
                runtime = trader._get_options_runtime(cfg)
                runtime.finalize()
            except Exception as exc:
                logger.critical("Options intraday finalization unresolved: %s", exc)
                trader._options_unresolved_exits = {
                    "strict_options": str(exc),
                }
                unresolved_failure = str(exc)
        else:
            try:
                _squareoff_options(opt_broker, "eod-squareoff", trader=trader)
            except Exception as exc:
                logger.warning("Options EOD square-off failed: %s", exc)
        if (plan is not None and not plan.get("executed") and
                not getattr(trader, "_options_unresolved_exits", {})):
            order_ids = list(getattr(trader, "_last_plan_order_ids", []) or [])
            order_ids.extend(getattr(trader, "_last_option_order_ids", []) or [])
            mark_executed(plan, order_ids, date=date)
            nested = plan.get("options_intraday_plan") if isinstance(plan, dict) else None
            if isinstance(nested, dict):
                nested["executed"] = True
                nested["order_ids"] = list(order_ids)
        logger.info("Session job finished at %s",
                    datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"))
        if unresolved_failure:
            raise RuntimeError(
                "strict options session failed: remote position was not confirmed flat")


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
    if opt_broker is None and not getattr(trader, "_opt_shadows", None):
        return
    try:
        from src.market_clock import market_clock_from_config
        now = datetime.now(IST)
        if market_clock_from_config(cfg).is_hard_flat(now):
            _squareoff_options(opt_broker, "eod-squareoff", trader=trader)
            return
    except Exception as exc:
        logger.warning("Options hard-flat clock check failed: %s", exc)
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
    if opt_broker is None and not (trader is not None and
                                   getattr(trader, "_opt_shadows", None)):
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
    unresolved = []
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
                        from src.execution_lifecycle import record_confirmed_close
                        record_confirmed_close(trader, sid, rec)
                        trader._forget_option_risk(sid)
                    except Exception as exc:
                        logger.warning("Options risk close accounting failed for %s: %s",
                                       sid, exc)
            else:
                unresolved.append(f"{getattr(br, 'provider_name', '?')}:{sid}")
                logger.critical("Options exit not confirmed for %s; retaining position.",
                                sid)
        except Exception as exc:
            unresolved.append(f"{getattr(br, 'provider_name', '?')}:{sid}")
            logger.critical("Unresolved options EOD exit for %s: %s", sid, exc)
            continue
    if unresolved and trader is not None:
        trader._options_unresolved_exits = {
            key: reason for key, reason in
            ((item, "exit-unconfirmed") for item in unresolved)
        }
        logger.critical("Options remain unresolved at EOD: %s",
                        ", ".join(unresolved))
        try:
            from src.alerts import send_options_telegram
            send_options_telegram("CRITICAL: unresolved options exit(s): "
                                  + ", ".join(unresolved))
        except Exception:
            pass
    elif trader is not None:
        trader._options_unresolved_exits = {}
    if trader is not None and n:
        try:
            trader._write_trade_log_csv()
        except Exception:
            pass
    return n


if __name__ == "__main__":
    main()
