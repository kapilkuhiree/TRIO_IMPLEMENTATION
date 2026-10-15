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
from copy import deepcopy
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.utils import load_env, load_config, utc_now, get_logger  # noqa: E402
from src.screener import screen, resolve_basket  # noqa: E402
from src.plan import (save_plan, today_ist_str, next_trading_day_ist_str)  # noqa: E402
from src.options_intraday import (  # noqa: E402
    COMBINED,
    combined_mode_enabled,
    options_intraday_enabled,
    resolve_trading_mode,
)
from src.options_intraday_plan import build_intraday_plan  # noqa: E402

load_env()
logger = get_logger("premarket")


def build_plan(universe: str, timeframe: str, top_n: int,
               with_options: bool = True,
               trading_mode: str = None) -> Dict[str, Any]:
    """Run the daily screen and return the plan payload.

    When config `options.enabled` is true, each candidate also carries
    `option_legs` (the defined-risk NIFTY expression picked by
    options_selector from the live chain / BS sim). Stock fields stay
    untouched so the equity path never depends on options.
    """
    cfg = load_config()
    mode = resolve_trading_mode(cfg, trading_mode)
    if options_intraday_enabled(cfg, mode):
        return build_intraday_plan(cfg)
    combined = combined_mode_enabled(cfg, mode)
    sc_cfg = cfg.get("screener", {})
    symbols = resolve_basket(universe)

    # Clear any stale counters first: if screen() is patched/mocked it records
    # nothing, and we must not report a previous scan's numbers.
    try:
        from src.screener import reset_last_stats
        reset_last_stats()
    except Exception:
        pass

    opt_cfg = cfg.get("options", {}) or {}
    # Combined mode deliberately uses the existing equity screen without the
    # legacy option overlay.  Its strict options plan is kept in a separate
    # nested payload below, so PaperTrader/session cannot route these legs via
    # the old overlay path.
    options_on = (with_options and bool(opt_cfg.get("enabled", True))
                  and not combined)
    options_top_n = int(sc_cfg.get("options_max_candidates", 6) or 6)
    options_min_rank = float(sc_cfg.get(
        "options_min_rank", sc_cfg.get("min_rank", 60.0)))
    options_min_conf = int(sc_cfg.get(
        "options_min_confidence", sc_cfg.get("min_confidence", 70)) or 0)
    equity_min_rank = float(sc_cfg.get("min_rank", 60.0))
    equity_min_conf = int(sc_cfg.get("min_confidence", 70) or 0)
    scan_top_n = max(int(top_n), options_top_n if options_on else int(top_n))
    scan_min_rank = min(equity_min_rank,
                        options_min_rank if options_on else equity_min_rank)
    scan_min_conf = min(equity_min_conf,
                        options_min_conf if options_on else equity_min_conf)
    screened = screen(symbols=symbols, timeframe=timeframe,
                      top_n=scan_top_n, min_rank=scan_min_rank,
                      min_confidence=scan_min_conf)
    equity_candidates = [c for c in screened
                         if c.rank >= equity_min_rank
                         and int(getattr(c.signal, "confidence", 0) or 0)
                         >= equity_min_conf][:int(top_n)]
    option_candidates = [c for c in screened
                         if c.rank >= options_min_rank
                         and int(getattr(c.signal, "confidence", 0) or 0)
                         >= options_min_conf][:options_top_n] if options_on else []
    cand_dicts = [c.to_dict() for c in equity_candidates]
    option_dicts = [c.to_dict() for c in option_candidates]

    # Real counters from the screener (fetch/screen failures only). The old
    # formula (`requested - len(candidates)`) reported ~99 "failed" on every
    # 100-symbol basket because screen() returns only the top-N picks — so a
    # healthy scan looked like a total data outage in the Telegram summary.
    try:
        from src.screener import get_last_stats
        stats = get_last_stats()
    except Exception:
        stats = {}
    # `scanned` = symbols we attempted (== requested). `failed` = genuine
    # fetch/screen errors from the screener, NOT "didn't make the top N".
    n_scanned = len(symbols)
    if "requested" in stats:
        n_failed = int(stats.get("failed", 0) or 0)
        n_actionable = int(stats.get("actionable", len(cand_dicts)) or 0)
    else:
        # No stats available (mocked/legacy screen). Assume no fetch failures
        # rather than inventing a bogus count.
        n_failed = 0
        n_actionable = len(cand_dicts)

    # Options overlay (shadow until proven): attach legs per candidate.
    if options_on and option_dicts:
        try:
            from src.options_chain import (
                fetch_chain,
                nearest_expiry,
                expiry_status,
            )
            from src.options_selector import select
            chain = fetch_chain(str(opt_cfg.get("underlying", "NIFTY")))
            if chain is not None:
                expiry = nearest_expiry(chain) or ""
                chain_expiry_status = expiry_status(chain, expiry)
                plan_chain_diagnostic = dict(chain.get("diagnostics") or {})
                plan_chain_diagnostic["selected_expiry"] = expiry
                plan_chain_diagnostic["selected_expiry_status"] = chain_expiry_status
                for c in option_dicts:
                    try:
                        from src.signal_engine import TradeSignal
                        from src.data_fetcher import fetch_market_data
                        from src.indicators import compute_indicators
                        sig = TradeSignal(
                            symbol=c.get("symbol", ""),
                            action=c.get("action", "HOLD"),
                            entry_price=c.get("entry_price"),
                            confidence=int(c.get("confidence") or 0))
                        adx_v = None
                        ivr = None
                        try:
                            _md = fetch_market_data(
                                c.get("symbol", ""), "1d")
                            _rd = compute_indicators(
                                _md.ohlcv, c.get("symbol", ""), "1d")
                            for _k, _v in _rd.indicators.items():
                                if _k.startswith("adx_") and _v.value:
                                    adx_v = float(_v.value)
                                    break
                            _ivr = _rd.indicators.get("iv_rank")
                            if _ivr is not None and _ivr.value is not None:
                                ivr = float(_ivr.value)
                        except Exception:
                            pass
                        if ivr is None:
                            ivr = c.get("iv_rank")
                        pick = select(sig, chain, expiry=expiry,
                                      iv_rank=ivr, adx=adx_v)
                        c["option_legs"] = pick.to_dict()
                        c["option_diagnostic"] = {
                            "signal_underlying": c.get("symbol", ""),
                            "option_underlying": pick.underlying,
                            "underlying_mapping": pick.underlying_mapping,
                            "requested_expiry": expiry,
                            "expiry_status": chain_expiry_status,
                            "selection_stage": (
                                "selected" if pick.strategy not in (None, "", "none")
                                else "contract_selection"),
                            "rejection_reasons": list(pick.reasoning)
                            if pick.strategy in (None, "", "none") else [],
                        }
                        c["iv_rank"] = ivr
                        c["adx"] = adx_v
                    except Exception as exc:
                        c["option_legs"] = {
                            "strategy": "none",
                            "underlying": str(opt_cfg.get("underlying", "NIFTY")).upper(),
                            "expiry": expiry,
                            "legs": [],
                            "reasoning": [f"overlay_exception: {exc}"],
                        }
                        c["option_diagnostic"] = {
                            "signal_underlying": c.get("symbol", ""),
                            "option_underlying": str(
                                opt_cfg.get("underlying", "NIFTY")).upper(),
                            "underlying_mapping": "configured_index_overlay",
                            "requested_expiry": expiry,
                            "expiry_status": chain_expiry_status,
                            "selection_stage": "overlay_exception",
                            "rejection_reasons": [f"overlay_exception: {exc}"],
                        }
                        logger.warning("Options overlay failed for %s: %s",
                                       c.get("symbol"), exc)
                # Kept on the plan so the persisted artifact remains useful
                # even when output/chain_cache is intentionally ignored.
                chain_diagnostic = plan_chain_diagnostic
            else:
                chain_diagnostic = {
                    "symbol": str(opt_cfg.get("underlying", "NIFTY")).upper(),
                    "retrieval": {"success": False},
                    "failure_reason": "chain_unavailable",
                }
        except Exception as exc:
            chain_diagnostic = {
                "symbol": str(opt_cfg.get("underlying", "NIFTY")).upper(),
                "retrieval": {"success": False},
                "failure_reason": f"overlay_exception: {exc}",
            }
            logger.warning("Options overlay unavailable: %s", exc)
    else:
        chain_diagnostic = {
            "symbol": str(opt_cfg.get("underlying", "NIFTY")).upper(),
            "retrieval": {"success": False},
            "failure_reason": "options_disabled_or_no_candidates",
        }

    # Screener already logs per-symbol failures; surface the same number
    # here so Telegram/dashboard and the JSON file all agree. `scanned` is
    # what actually fetched, `actionable` is what cleared confidence+rank,
    # and `failed` is genuine fetch/screen errors only.
    plan = {
        "date": today_ist_str(),
        "generated_at": utc_now(),
        "universe": universe,
        "timeframe": timeframe,
        "scanned": n_scanned,
        "requested": len(symbols),
        "failed": n_failed,
        "actionable": n_actionable,
        "min_rank": sc_cfg.get("min_rank", 40.0),
        "min_confidence": sc_cfg.get("min_confidence", 60),
        "candidates": cand_dicts,
        "option_candidates": option_dicts,
        "options_mapping_policy": (
            "configured_index_overlay: stock signals are expressed with the "
            f"{str(opt_cfg.get('underlying', 'NIFTY')).upper()} index chain"
            if options_on else "options_disabled"),
        "option_chain_diagnostic": chain_diagnostic,
        "options_pnl_readiness": "NOT_READY_FOR_OPTIONS_PNL",
        "research_only": True,
        "trading_mode": mode,
        "executed": False,
        "asset": "equity+options",
    }

    if combined:
        try:
            options_plan = deepcopy(build_intraday_plan(cfg))
            if not isinstance(options_plan, dict):
                raise TypeError("options plan builder returned a non-mapping")
        except Exception as exc:
            # A strict options data failure must never discard the equity
            # screen.  Keep a complete, auditable nested plan for the session
            # owner to rebuild at the open.
            options_plan = {
                "date": today_ist_str(),
                "generated_at": utc_now(),
                "trading_mode": "options_intraday",
                "asset": "options",
                "candidates": [],
                "option_candidates": [],
                "option_chain": None,
                "intraday_data_diagnostic": {
                    "valid": False,
                    "failure_reason": "options_plan_build_exception",
                },
                "option_chain_diagnostic": {
                    "valid": False,
                    "failure_reason": f"options_plan_build_exception: {exc}",
                },
                "executed": False,
            }

        # build_intraday_plan is also used by a same-session owner.  The
        # premarket artifact itself is a preview unless a caller explicitly
        # proves that the nested plan is execution-ready.
        options_plan["trading_mode"] = "options_intraday"
        options_plan.setdefault("asset", "options")
        options_plan.setdefault("candidates", [])
        options_plan.setdefault("option_candidates", [])
        options_plan.setdefault("option_chain", None)
        if not isinstance(options_plan.get("option_chain_diagnostic"), dict):
            options_plan["option_chain_diagnostic"] = {}
        if not isinstance(options_plan.get("intraday_data_diagnostic"), dict):
            options_plan["intraday_data_diagnostic"] = {}
        options_plan.setdefault("generated_at", utc_now())
        chain = options_plan.get("option_chain") or {}
        option_candidates = options_plan.get("option_candidates") or []
        if not isinstance(options_plan.get("timestamps"), dict):
            options_plan["timestamps"] = {}
        timestamps = options_plan["timestamps"]
        timestamps.setdefault("generated_at", options_plan.get("generated_at"))
        timestamps.setdefault(
            "option_chain_quote",
            chain.get("quote_timestamp") if isinstance(chain, dict) else None,
        )
        timestamps.setdefault(
            "latest_signal",
            next((candidate.get("signal_timestamp")
                  for candidate in option_candidates
                  if isinstance(candidate, dict)
                  and candidate.get("signal_timestamp")), None),
        )
        diagnostic_text = " ".join(
            str(value).lower()
            for value in (
                (options_plan.get("option_chain_diagnostic") or {}).get(
                    "failure_reason", ""),
                (options_plan.get("intraday_data_diagnostic") or {}).get(
                    "failure_reason", ""),
            )
        )
        preview_or_stale = any(
            options_plan.get(key) is True
            for key in ("preview", "is_preview", "stale", "is_stale")
        ) or "preview" in diagnostic_text or "stale" in diagnostic_text
        if options_plan.get("execution_ready") is not True or preview_or_stale:
            options_plan["execution_ready"] = False
            options_plan.setdefault(
                "execution_blocked_reason",
                "combined premarket options plan is a preview; rebuild at session open",
            )

        plan["options_intraday_plan"] = options_plan
        plan["legacy_options_overlay"] = False

    return plan


def format_summary(plan: Dict[str, Any], kind: str = "all") -> str:
    """Phone-friendly Telegram summary. kind=all|equity|options."""
    _req = plan.get("requested", plan.get("scanned", "?"))
    _fail = plan.get("failed", "?")
    tag = " — Options (8877508167)" if kind == "options" else (
        " — Equity" if kind == "equity" else "")
    lines = [f"*TRIO premarket plan — {plan['date']}{tag}*",
             f"Universe: {plan['universe']} ({plan['scanned']} scanned, {_fail} failed, {plan.get('requested', _req)} requested) "
             f"on {plan['timeframe']}"]
    option_plan = plan.get("options_intraday_plan", {}) or {}
    option_source = option_plan if plan.get("trading_mode") == COMBINED else plan
    cands = ((option_source.get("option_candidates", []) or [])
             if kind == "options" else (plan.get("candidates", []) or []))
    if kind == "options":
        opts = [c for c in cands if (c.get("option_legs") or {}).get("strategy") not in (None, "", "none")]
        if not opts:
            lines.append("No options spreads met the bar today.")
            return "\n".join(lines)
        lines.append(f"{len(opts)} NIFTY options spread(s):")
        for c in opts:
            opt = c.get("option_legs") or {}
            legs = " + ".join(
                f"{l.get('side')} {l.get('strike')}{l.get('kind')}@{l.get('premium')}"
                for l in (opt.get("legs") or []))
            lines.append(f"• {c.get('symbol')} {opt.get('strategy')} {legs} "
                         f"debit {opt.get('net_debit')} maxLoss {opt.get('maxLoss')}")
        return "\n".join(lines)
    # equity (or all): show equity legs; options detail only for all
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
        if kind == "all":
            opt = c.get("option_legs") or {}
            if opt.get("strategy") and opt.get("strategy") != "none":
                legs = " + ".join(
                    f"{l.get('side')} {l.get('strike')}{l.get('kind')}@{l.get('premium')}"
                    for l in (opt.get("legs") or []))
                lines.append(
                    f"  + {opt.get('strategy')} {legs} "
                    f"debit {opt.get('net_debit')} "
                    f"maxLoss {opt.get('maxLoss')}")
    return "\n".join(lines)


def format_equity_summary(plan: Dict[str, Any]) -> str:
    return format_summary(plan, kind="equity")


def format_options_summary(plan: Dict[str, Any]) -> str:
    return format_summary(plan, kind="options")


def prepare_plan_for_premarket(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Mark the post-close artifact as a preview for the next session.

    Strict options candidates use same-session completed bars and exchange
    quotes, so they must be rebuilt after the next session opens.  The
    artifact is retained for diagnostics, but the session runner will not
    treat it as executable.
    """
    prepared = dict(plan or {})
    prepared["date"] = next_trading_day_ist_str()
    if prepared.get("trading_mode") == "options_intraday":
        prepared["execution_ready"] = False
        prepared["execution_blocked_reason"] = (
            "options_intraday plans are rebuilt during the live session")
    elif prepared.get("trading_mode") == COMBINED:
        nested = deepcopy(prepared.get("options_intraday_plan") or {})
        nested["trading_mode"] = "options_intraday"
        nested["execution_ready"] = False
        nested["execution_blocked_reason"] = (
            "combined premarket options plan is rebuilt during the live session")
        prepared["options_intraday_plan"] = nested
    return prepared


def main() -> None:
    # Check for /start on BOTH bots so broadcasts stay separate.
    try:
        from src.alerts import handle_all_joins
        handle_all_joins()
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="TRIO premarket plan builder")
    parser.add_argument("--universe", default=None,
                        help="Basket name (nifty100) or comma list; "
                             "defaults to config universe.premarket")
    parser.add_argument("--timeframe", default="1d")
    parser.add_argument("--top-n", type=int, default=None)
    parser.add_argument("--no-alert", action="store_true")
    parser.add_argument("--trading-mode", default=None,
                        choices=("production", "combined", "options_intraday"),
                        help="production (default), combined, or options_intraday")
    args = parser.parse_args()

    cfg = load_config()
    universe = args.universe or cfg.get("universe", {}).get(
        "premarket", "nifty100")
    top_n = args.top_n or cfg.get("screener", {}).get(
        "max_positions_to_open", 3)

    logger.info("Premarket scan: universe=%s timeframe=%s top_n=%d",
                universe, args.timeframe, top_n)
    trading_mode = resolve_trading_mode(cfg, args.trading_mode)
    plan = build_plan(universe, args.timeframe, top_n,
                      trading_mode=trading_mode)
    # The post-close premarket run belongs to the next valid NSE session.
    plan = prepare_plan_for_premarket(plan)
    path = save_plan(plan, date=plan["date"])
    print(f"Plan written: {path}")
    print(format_summary(plan))

    if not args.no_alert:
        try:
            from src.alerts import send_telegram
            send_telegram(format_equity_summary(plan))
        except Exception as exc:
            logger.warning("Equity premarket Telegram failed: %s", exc)
        try:
            from src.alerts import send_options_telegram
            send_options_telegram(format_options_summary(plan))
        except Exception as exc:
            logger.warning("Options premarket Telegram failed: %s", exc)


if __name__ == "__main__":
    main()
