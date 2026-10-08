"""
TRIO — Daily audit digest (GitHub Actions, after close)
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

Reads the trade log, summarizes today's (IST) session and sends it to
Telegram. This is the evening review loop: what traded, PASS/FAIL, win
rate, why things were skipped, and how many shadow-invalidation signals
fired (log-only exits we are measuring).

    python scripts/digest.py [--date YYYY-MM-DD] [--no-alert]

DISCLAIMER: Educational purposes only. Not financial advice.
"""

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.utils import load_env, get_logger  # noqa: E402

load_env()
logger = get_logger("digest")

IST = timezone(timedelta(hours=5, minutes=30))
LOG_PATH = ROOT / "output" / "trade_log.jsonl"


def _ist_date(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(IST).strftime("%Y-%m-%d")
    except Exception:
        return ""


def collect(date: str) -> Dict[str, Any]:
    events: List[Dict[str, Any]] = []
    if LOG_PATH.exists():
        for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if _ist_date(rec.get("ts", "")) == date and rec.get("mode") != "test":
                events.append(rec)

    closes = [e for e in events if e.get("event") == "close"]
    orders = [e for e in events if e.get("event") == "order"]
    skips = [e for e in events if e.get("event") == "skip"]
    shadows = [e for e in events if e.get("event") == "shadow-exit"]

    # Equity vs options: options records carry asset="options"; legacy
    # equity records have no asset key (or "equity").
    eq_closes = [c for c in closes if c.get("asset", "equity") != "options"]
    opt_closes = [c for c in closes if c.get("asset") == "options"]
    eq_orders = [o for o in orders if o.get("asset", "equity") != "options"]
    opt_orders = [o for o in orders if o.get("asset") == "options"]

    def _summ(items):
        p = sum(float(c.get("pnl") or 0) for c in items)
        np_ = sum(1 for c in items if c.get("result") == "PASS")
        nf = len(items) - np_
        return (round(p, 2), np_, nf,
                round(np_ / len(items) * 100, 1) if items else 0.0)

    pnl, npass, nfail, win_rate = _summ(closes)
    eq_pnl, eq_pass, eq_fail, eq_wr = _summ(eq_closes)
    opt_pnl, opt_pass, opt_fail, opt_wr = _summ(opt_closes)
    skip_reasons = Counter(s.get("reason", "?") for s in skips)

    return {
        "date": date,
        "orders": orders,
        "closes": closes,
        "skips": skips,
        "shadows": shadows,
        "pnl": pnl,
        "npass": npass,
        "nfail": nfail,
        "win_rate": win_rate,
        "eq": {"pnl": eq_pnl, "pass": eq_pass, "fail": eq_fail,
               "wr": eq_wr, "n_orders": len(eq_orders),
               "n_closes": len(eq_closes)},
        "opt": {"pnl": opt_pnl, "pass": opt_pass, "fail": opt_fail,
                "wr": opt_wr, "n_orders": len(opt_orders),
                "n_closes": len(opt_closes)},
        "skip_reasons": dict(skip_reasons),
    }


def _order_line(o: Dict[str, Any]) -> str:
    if o.get("asset") == "options":
        return (f"  {o.get('symbol')} [{o.get('strategy')}] "
                f"debit {o.get('net_debit')} lots {o.get('lots')} "
                f"maxLoss {o.get('maxLoss')}")
    return (f"  {o.get('symbol')} {o.get('action')} "
            f"@{o.get('entry')} stop {o.get('stop')} "
            f"target {o.get('target')}")


def _close_line(c: Dict[str, Any]) -> str:
    if c.get("asset") == "options":
        return (f"  {c.get('symbol')} [{c.get('side')}] "
                f"{c.get('reason')} {c.get('result')} "
                f"entry {c.get('entry')} exit {c.get('exit')} "
                f"P&L {c.get('pnl')}")
    return (f"  {c.get('symbol')} {c.get('side')} "
            f"{c.get('reason')} {c.get('result')} "
            f"P&L {c.get('pnl')}")


def format_digest(d: Dict[str, Any]) -> str:
    lines = [f"*TRIO daily digest — {d['date']}*"]
    lines.append(f"Orders placed: {len(d['orders'])} | "
                 f"Closed: {len(d['closes'])}  (PASS {d['npass']} / "
                 f"FAIL {d['nfail']})")
    lines.append(f"Day P&L: {d['pnl']:+.2f} | Win rate: {d['win_rate']}%")
    eq, opt = d.get("eq", {}), d.get("opt", {})
    lines.append(
        f"Equity: orders {eq.get('n_orders', 0)} closes "
        f"{eq.get('n_closes', 0)} ({eq.get('pass', 0)}P/{eq.get('fail', 0)}F) "
        f"P&L {eq.get('pnl', 0.0):+.2f}")
    lines.append(
        f"Options: orders {opt.get('n_orders', 0)} closes "
        f"{opt.get('n_closes', 0)} ({opt.get('pass', 0)}P/{opt.get('fail', 0)}F) "
        f"P&L {opt.get('pnl', 0.0):+.2f}")
    lines.append(f"Shadow-exit signals (log-only): {len(d['shadows'])}")
    if d["orders"]:
        lines.append("Entries:")
        for o in d["orders"]:
            lines.append(_order_line(o))
    if d["closes"]:
        lines.append("Closed:")
        for c in d["closes"]:
            lines.append(_close_line(c))
    if d["skip_reasons"]:
        lines.append("Skips: " + ", ".join(
            f"{k}={v}" for k, v in d["skip_reasons"].items()))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="TRIO daily digest")
    parser.add_argument("--date", default=None)
    parser.add_argument("--no-alert", action="store_true")
    args = parser.parse_args()

    date = args.date or datetime.now(IST).strftime("%Y-%m-%d")
    d = collect(date)
    text = format_digest(d)
    print(text)

    if not args.no_alert:
        try:
            from src.alerts import send_telegram
            send_telegram(text)
        except Exception as exc:
            logger.warning("Digest Telegram failed: %s", exc)


if __name__ == "__main__":
    main()