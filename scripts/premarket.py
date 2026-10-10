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


def build_plan(universe: str, timeframe: str, top_n: int,
               with_options: bool = True) -> Dict[str, Any]:
    """Run the daily screen and return the plan payload.

    When config `options.enabled` is true, each candidate also carries
    `option_legs` (the defined-risk NIFTY expression picked by
    options_selector from the live chain / BS sim). Stock fields stay
    untouched so the equity path never depends on options.
    """
    cfg = load_config()
    sc_cfg = cfg.get("screener", {})
    symbols = resolve_basket(universe)

    candidates = screen(symbols=symbols, timeframe=timeframe, top_n=top_n)
    cand_dicts = [c.to_dict() for c in candidates]

    # Options overlay (shadow until proven): attach legs per candidate.
    opt_cfg = cfg.get("options", {}) or {}
    options_on = with_options and bool(opt_cfg.get("enabled", True))
    if options_on and cand_dicts:
        try:
            from src.options_chain import fetch_chain, nearest_expiry
            from src.options_selector import select
            chain = fetch_chain(str(opt_cfg.get("underlying", "NIFTY")))
            if chain is not None:
                expiry = nearest_expiry(chain) or ""
                for i, c in enumerate(cand_dicts):
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
                        c["iv_rank"] = ivr
                        c["adx"] = adx_v
                    except Exception as exc:
                        logger.warning("Options overlay failed for %s: %s",
                                       c.get("symbol"), exc)
        except Exception as exc:
            logger.warning("Options overlay unavailable: %s", exc)

    # Screener already logs per-symbol failures; surface the same number
    # here so Telegram/dashboard and the JSON file all agree.
    # Failed is the gap between requested symbols and the surviving candidates
    # when both are driven by the same fetch. A later pass will make the
    # screener return (candidates, stats) and we can copy stats["failed"] here.
    return {
        "date": today_ist_str(),
        "generated_at": utc_now(),
        "universe": universe,
        "timeframe": timeframe,
        "scanned": len(symbols),
        "requested": len(symbols),
        "failed": max(0, len(symbols) - len(cand_dicts)),
        "min_rank": sc_cfg.get("min_rank", 40.0),
        "min_confidence": sc_cfg.get("min_confidence", 60),
        "candidates": cand_dicts,
        "executed": False,
        "asset": "equity+options",
    }


def format_summary(plan: Dict[str, Any], kind: str = "all") -> str:
    """Phone-friendly Telegram summary. kind=all|equity|options."""
    _req = plan.get("requested", plan.get("scanned", "?"))
    _fail = plan.get("failed", "?")
    tag = " — Options (8877508167)" if kind == "options" else (
        " — Equity" if kind == "equity" else "")
    lines = [f"*TRIO premarket plan — {plan['date']}{tag}*",
             f"Universe: {plan['universe']} ({plan['scanned']} scanned, {_fail} failed, {plan.get('requested', _req)} requested) "
             f"on {plan['timeframe']}"]
    cands = plan.get("candidates", []) or []
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