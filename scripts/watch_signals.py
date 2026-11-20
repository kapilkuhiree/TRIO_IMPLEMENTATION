"""Tail the paper-session trade log and alert on new trading signals.

Lightweight companion to ``scripts/session.py``: it never talks to a broker,
never writes state, and only reads ``output/trade_log.jsonl``.

It alerts on:

* a tradable sleeve (orb / orb_retest) flipping to ``signal`` in ``options_scan``;
* a log-only ``shadow_signal`` (mean_reversion / ORRS / smc_liquidity);
* any ``order`` / ``exit`` event (a fill on the paper broker);
* ``fetch_errors`` or ``data_ok < requested`` (feed trouble).

Usage
-----
    python scripts/watch_signals.py
    python scripts/watch_signals.py --from-start --once
    python scripts/watch_signals.py --until 15:15 --beep

Alerts go to stdout (flush per line) and are appended to
``output/signal_monitor.log``.  Default stop time is 15:15 IST, i.e. after the
15:10 hard flat, so the watcher cannot outlive the session it watches.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_LOG = ROOT / "output" / "trade_log.jsonl"
DEFAULT_OUT = ROOT / "output" / "signal_monitor.log"


def now_ist() -> datetime:
    return datetime.now(IST)


def _stamp() -> str:
    return now_ist().strftime("%H:%M:%S")


def _beep(enabled: bool) -> None:
    if not enabled:
        return
    try:
        import winsound

        winsound.Beep(880, 180)
    except Exception:
        pass


class Monitor:
    def __init__(self, out_path: Path, *, beep: bool, repeat_minutes: float = 5.0) -> None:
        self.out_path = out_path
        self.beep = beep
        self.repeat_minutes = max(0.0, repeat_minutes)
        self._signal_state: Dict[Tuple[str, str], bool] = {}
        self._signal_alerted: Dict[Tuple[str, str], datetime] = {}
        self._seen_shadow: Set[Tuple[str, str, float, str]] = set()
        self._seen_orders: Set[Tuple[str, str]] = set()
        self._last_feed_warn: Dict[str, Tuple[datetime, str]] = {}
        self._alerts = 0

    # -- output -------------------------------------------------------
    def alert(self, kind: str, message: str, *, sound: bool = True) -> None:
        line = f"[{_stamp()}] {kind:<6} {message}"
        print(line, flush=True)
        try:
            self.out_path.parent.mkdir(parents=True, exist_ok=True)
            with self.out_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception:
            pass
        self._alerts += 1
        if sound:
            _beep(self.beep)

    def _throttle(self, key: str, reason: str, minutes: float = 5.0) -> bool:
        """True when this reason has not been reported for ``minutes``."""
        now = now_ist()
        previous = self._last_feed_warn.get(key)
        if previous is not None:
            stamp, last_reason = previous
            if last_reason == reason and (now - stamp) < timedelta(minutes=minutes):
                return False
        self._last_feed_warn[key] = (now, reason)
        return True

    # -- parsing ------------------------------------------------------
    def handle(self, record: Dict[str, Any]) -> None:
        event = str(record.get("event") or "")

        if event == "options_scan":
            self._scan(record)
        elif event == "shadow_signal":
            self._shadow(record)
        elif event in {"order", "exit"}:
            self._order(record)

    def _scan(self, record: Dict[str, Any]) -> None:
        requested = record.get("requested") or 0
        data_ok = record.get("data_ok") or 0
        fetch_errors = record.get("fetch_errors") or 0
        if fetch_errors and self._throttle("fetch", f"fetch_errors={fetch_errors}"):
            self.alert("FEED", f"fetch_errors={fetch_errors} (feed degraded)", sound=False)
        if requested and data_ok < requested and self._throttle("data", f"{data_ok}/{requested}"):
            detail = ""
            rejections = record.get("rejections") or []
            if rejections:
                first = rejections[0]
                detail = f" first={first.get('stage')}:{first.get('reason')}"
            self.alert("FEED", f"data_ok {data_ok}/{requested}{detail}", sound=False)

        for underlying, payload in (record.get("per_underlying") or {}).items():
            for strategy, status in ((payload or {}).get("strategies") or {}).items():
                state = str((status or {}).get("status") or "")
                key = (str(underlying), str(strategy))
                was_signal = self._signal_state.get(key, False)
                self._signal_state[key] = state == "signal"
                if state == "signal" and not was_signal:
                    last = self._signal_alerted.get(key)
                    now = now_ist()
                    if last is not None and (now - last) < timedelta(
                            minutes=self.repeat_minutes):
                        continue
                    self._signal_alerted[key] = now
                    self.alert("SIGNAL", f"{strategy} {underlying}")

        for rejection in record.get("entry_rejections") or []:
            self.alert(
                "BLOCK",
                f"{rejection.get('underlying', '?')} "
                f"{rejection.get('strategy', '')} {rejection.get('reason', '')}".strip(),
                sound=False,
            )

    def _shadow(self, record: Dict[str, Any]) -> None:
        key = (
            str(record.get("strategy") or ""),
            str(record.get("underlying") or ""),
            float(record.get("strike") or 0.0),
            str(record.get("kind") or ""),
        )
        if key in self._seen_shadow:
            return
        self._seen_shadow.add(key)
        self.alert(
            "SHADOW",
            f"{key[0]} {key[1]} {key[2]:.0f}{key[3]} ask {record.get('ask', 0)} "
            f"(log-only, no order)",
            sound=False,
        )

    def _order(self, record: Dict[str, Any]) -> None:
        key = (
            str(record.get("event") or ""),
            str(record.get("position_id") or record.get("order_id") or ""),
        )
        if key in self._seen_orders:
            return
        self._seen_orders.add(key)
        if record.get("event") == "exit":
            self.alert(
                "EXIT",
                f"{record.get('strategy', '')} pnl {record.get('pnl', 0)} "
                f"reason {record.get('reason', '')}",
            )
        else:
            self.alert(
                "FILL",
                f"{record.get('strategy', '')} {record.get('trading_symbol', '')} "
                f"@ {record.get('entry_premium', record.get('price', ''))}",
            )


def _stop_at(value: str) -> datetime:
    hour, minute = (int(part) for part in value.split(":", 1))
    current = now_ist()
    return current.replace(hour=hour, minute=minute, second=0, microsecond=0)


def follow(log_path: Path, monitor: Monitor, *, poll: float, deadline: datetime,
           from_start: bool, once: bool) -> int:
    if not log_path.exists():
        monitor.alert("ERROR", f"log not found: {log_path}", sound=False)
        return 1

    handle = log_path.open("r", encoding="utf-8", errors="replace")
    if not from_start:
        handle.seek(0, os.SEEK_END)

    buffer = ""
    try:
        while True:
            chunk = handle.read()
            if chunk:
                buffer += chunk
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    monitor.handle(record)
                continue

            if once:
                break
            if now_ist() >= deadline:
                monitor.alert("INFO", "monitor reached stop time", sound=False)
                break
            time.sleep(poll)
    except KeyboardInterrupt:
        monitor.alert("INFO", "monitor interrupted", sound=False)
    finally:
        handle.close()

    monitor.alert(
        "INFO",
        f"monitor stopped after {monitor._alerts} alert(s)",
        sound=False,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Alert on new signals in the paper trade log (read-only).")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--poll", type=float, default=2.0,
                        help="seconds between reads (default 2)")
    parser.add_argument("--until", default="15:15",
                        help="IST stop time HH:MM (default 15:15)")
    parser.add_argument("--repeat-minutes", type=float, default=5.0,
                        help="suppress a repeat signal alert for this long (default 5)")
    parser.add_argument("--beep", action="store_true",
                        help="beep on signal/fill alerts")
    parser.add_argument("--from-start", action="store_true",
                        help="replay the existing log instead of tailing from EOF")
    parser.add_argument("--once", action="store_true",
                        help="read to EOF and exit (for testing)")
    return parser


def main(argv: Any = None) -> int:
    args = build_parser().parse_args(argv)
    monitor = Monitor(args.out, beep=args.beep,
                      repeat_minutes=args.repeat_minutes)
    return follow(args.log, monitor, poll=max(0.5, args.poll),
                  deadline=_stop_at(args.until), from_start=args.from_start,
                  once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
