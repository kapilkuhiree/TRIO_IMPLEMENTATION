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

    pnl = sum(float(c.get("pnl") or 0) for c in closes)
    npass = sum(1 for c in closes if c.get("result") == "PASS")
    nfail = len(closes) - npass
    skip_reasons = Counter(s.get("reason", "?") for s in skips)

    return {
        "date": date,
        "orders": orders,
        "closes": closes,
        "skips": skips,
        "shadows": shadows,
        "pnl": round(pnl, 2),
        "npass": npass,
        "nfail": nfail,
        "win_rate": round(npass / len(closes) * 100, 1) if closes else 0.0,
        "skip_reasons": dict(skip_reasons),
    }


def format_digest(d: Dict[str, Any]) -> str:
    lines = [f"*TRIO daily digest — {d['date']}*"]
    lines.append(f"Orders placed: {len(d['orders'])} | "
                 f"Closed: {len(d['closes'])}  (PASS {d['npass']} / "
                 f"FAIL {d['nfail']})")
    lines.append(f"Day P&L: {d['pnl']:+.2f} | Win rate: {d['win_rate']}%")
    lines.append(f"Shadow-exit signals (log-only): {len(d['shadows'])}")
    if d["orders"]:
        lines.append("Entries:")
        for o in d["orders"]:
            lines.append(f"  {o.get('symbol')} {o.get('action')} "
                         f"@{o.get('entry')} stop {o.get('stop')} "
                         f"target {o.get('target')}")
    if d["closes"]:
        lines.append("Closed:")
        for c in d["closes"]:
            lines.append(f"  {c.get('symbol')} {c.get('side')} "
                         f"{c.get('reason')} {c.get('result')} "
                         f"P&L {c.get('pnl')}")
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