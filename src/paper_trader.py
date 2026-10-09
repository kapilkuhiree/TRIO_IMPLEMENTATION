"""
TRIO — Paper Trader
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

Runs the full signal pipeline in a loop at configurable intervals,
generating signals and simulating trades via the paper broker.

DISCLAIMER: Educational purposes only. Not financial advice.
"""

import csv
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.utils import get_logger, load_config, utc_now
from src.data_fetcher import fetch_market_data
from src.indicators import compute_indicators
from src.sentiment import analyze_sentiment
from src.signal_engine import generate_signal, TradeSignal
from src.risk_manager import apply_risk_management, get_risk_state
from src.broker.paper import PaperBroker

logger = get_logger("paper_trader")


class PaperTrader:
    """
    Paper trading loop. Scans symbols at regular intervals,
    generates signals, and simulates trades.

    Usage:
        trader = PaperTrader(symbols=["RELIANCE.NS", "TCS.NS"])
        trader.run(interval_seconds=300)
    Author: Kapil Kuhire
    """

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        timeframe: Optional[str] = None,
        initial_capital: Optional[float] = None,
        test_mode: bool = False,
    ):
        cfg = load_config()

        self.symbols = symbols or cfg.get("symbols", [])
        self.timeframe = timeframe or cfg.get("timeframes", {}).get("default", "15m")
        self.test_mode = test_mode  # smoke/unit runs write a SEPARATE log file

        rm_cfg = cfg.get("risk_management", {})
        capital = initial_capital or rm_cfg.get("capital", 100000)

        # Provider precedence: explicit env (TRIO_BROKER_PROVIDER=paper|
        # megabull) > config `broker.provider` > local paper. The test
        # suite pins `paper` via tests/conftest.py so the suite never
        # touches the network, whatever config.yaml says.
        provider = (
            os.environ.get("TRIO_BROKER_PROVIDER", "").strip().lower()
            or (cfg.get("broker", {}) or {}).get("provider", "paper")
        )
        if provider == "megabull":
            from src.broker.megabull import MegaBullBroker, load_dotenv_key
            api_key = os.environ.get("MEGABULL_API_KEY", "") or \
                load_dotenv_key("MEGABULL_API_KEY")
            self.broker = MegaBullBroker(
                api_key=api_key,
                initial_capital=capital,
            )
            logger.info("Broker provider: MegaBull (remote paper fills).")
        else:
            self.broker = PaperBroker(initial_capital=capital)
        self.broker_name = (
            getattr(self.broker, "provider_name", "paper"))
        self.signals_log: List[Dict[str, Any]] = []
        self._eod_squared = False  # one EOD square-off per session

        # Trade log: one JSON line per event (scan/screen/order/guard/close).
        # Path from config `logging.trade_log`; CSV mirror written at session end.
        # Test runs (smoke/unit/smoke keywords in the path or explicit
        # test_mode) go to a guarded sidecar file so price=100 test orders
        # NEVER pollute live P&L analysis again.
        log_cfg = cfg.get("logging", {})
        default_log = str(Path(cfg.get("general", {}).get("output_dir", "output"))
                          / "trade_log.jsonl")
        self.trade_log_path = Path(log_cfg.get("trade_log", default_log))
        # The configured live ledger. Never written while TRIO_TEST_MODE=1.
        self._live_log_path = self.trade_log_path

        logger.info(
            "PaperTrader initialized: %d symbols, timeframe=%s, capital=%.2f",
            len(self.symbols), self.timeframe, capital,
        )

    def scan_once(self) -> List[TradeSignal]:
        """
        Run one full scan: guard open positions first, then screen for new
        entries. Guards run BEFORE entries on every scan, so a position that
        breaks down is exited in the same pass — never held a full idle
        interval waiting for the next candle.

        Settlement rule (matches a real CNC/delivery account): a SELL is
        only placed when the broker actually holds the stock. Naked shorts
        are skipped with a logged reason instead of inventing a SHORT
        position the user could never exit on Zerodha. Set
        ``allow_shorts: true`` in config only if the account is an
        intraday/MIS setup where shorting is genuinely possible.

        Returns:
            List of TradeSignal objects executed (not every signal seen —
            only ranked candidates the screener picked and settled).
        """
        from src.screener import screen

        # SESSION-HOURS GUARD: outside 09:20–15:15 IST the NSE prints no
        # fresh candles, so any "signal" is stale-candle fiction (this bit
        # us live: a 21:23 SELL filled on dead data). Open positions still
        # get stop/target management below — only NEW entries are blocked.
        # NOTE: scripts/day_session.py (history replay) bypasses scan_once
        # entirely, so this guard never affects backtests. Do not "fix".
        if not self._session_open():
            logger.info("Market closed — scan skipped (positions still managed).")
            # EOD: square everything off once, at the closing print, before
            # any stale-price guard math. Without this the loop held 2
            # positions past 15:15 on 2026-10-06 — no exit, no record,
            # no alert.
            self._maybe_eod_square_off()
            self._guard_open_positions()
            self._manage_open_positions()
            self._log_event("scan", {"session_open": False,
                                     "positions": len(self.broker.positions)})
            return []

        # Session is open: a new trading day (or a reopen) — arm the EOD
        # square-off flag again.
        self._eod_squared = False

        # GUARD FIRST: re-score every open position on fresh bars. A live
        # position whose setup has broken (trend lost, MACD flipped, price
        # under its stop) is closed here, before any new entry is even
        # considered. This is the "keep navigating after taking the trade"
        # behaviour: the scan interval only spaces out ENTRIES, exits are
        # re-evaluated on every pass with the newest print.
        self._guard_open_positions()

        # Then the usual stop/target management on the latest price.
        self._manage_open_positions()

        cfg = load_config()
        allow_shorts = cfg.get("trading", {}).get("allow_shorts", False)

        # Screen, rank, and take only the top candidates.
        candidates = screen(symbols=self.symbols, timeframe=self.timeframe)
        self._log_event("screen", {
            "scanned": len(self.symbols),
            "candidates": [
                {"symbol": c.signal.symbol if c.signal else "?",
                 "action": c.signal.action if c.signal else "?",
                 "rank": c.rank, "setup": c.setup_name,
                 "confidence": c.signal.confidence if c.signal else 0}
                for c in candidates],
        })

        signals: List[TradeSignal] = []
        for cand in candidates:
            signal = cand.signal
            signals.append(signal)
            self.signals_log.append(cand.to_dict())

            # HOLD-SKIP (shorts-enabled accounts): never re-enter a symbol
            # already held on the SAME side. Re-scanning the same name
            # every 5 minutes pyramided ULTRACEMCO 8x and HDFCBANK 6x on
            # 2026-10-06. A SELL into an existing LONG still executes as
            # a settlement close (CNC clip below); anything else held is
            # skipped. One net position per symbol.
            held = self.broker.positions.get(signal.symbol)
            same_side_held = (
                held is not None and held.quantity > 0 and (
                    (signal.action == "SELL" and held.side == "SHORT")
                    or (signal.action == "BUY" and held.side == "LONG")))
            if same_side_held:
                logger.info(
                    "SKIP %s %s: already holding %d (%s) — rank %.1f kept "
                    "in log, no order placed.",
                    signal.action, signal.symbol, held.quantity,
                    held.side, cand.rank)
                self.signals_log[-1]["skipped"] = "already-holding"
                self._log_event("skip", {
                    "symbol": signal.symbol, "action": signal.action,
                    "rank": cand.rank, "setup": cand.setup_name,
                    "confidence": signal.confidence,
                    "reason": "already-holding"})
                continue

            # Settlement check: never SELL what the account does not hold.
            if signal.action == "SELL" and not allow_shorts:
                held_qty = held.quantity if held and held.side == "LONG" else 0
                if held_qty <= 0:
                    logger.warning(
                        "SKIP SELL %s: no holdings (would be rejected on a "
                        "real CNC account with 'insufficient holdings'). "
                        "Rank %.1f kept in log, no order placed.",
                        signal.symbol, cand.rank)
                    self.signals_log[-1]["skipped"] = "no-holdings"
                    continue
                if signal.position_size > held_qty:
                    logger.warning(
                        "CLIP SELL %s: signal wants %d but only %d held — "
                        "clipping to holdings (real broker would reject the "
                        "excess).", signal.symbol, signal.position_size,
                        held_qty)
                    signal.position_size = held_qty

            try:
                order = self.broker.place_order(
                    symbol=signal.symbol,
                    side=signal.action,
                    quantity=signal.position_size,
                    price=signal.entry_price,
                    stop_loss=signal.stop_loss,
                    target=signal.target,
                )
                if order.status == "REJECTED":
                    logger.warning("Order rejected for %s: %s",
                                   signal.symbol, order.error)
                    self.signals_log[-1]["skipped"] = "rejected"
                    self._log_event("skip", {
                        "symbol": signal.symbol, "action": signal.action,
                        "rank": cand.rank, "setup": cand.setup_name,
                        "confidence": signal.confidence,
                        "reason": f"rejected: {order.error}"})
                    continue
                logger.info("Paper order (rank %.1f, %s): %s",
                            cand.rank, cand.setup_name, order.to_dict())
                self._log_event("order", {
                    "symbol": signal.symbol, "action": signal.action,
                    "entry": signal.entry_price, "stop": signal.stop_loss,
                    "target": signal.target, "qty": signal.position_size,
                    "rank": cand.rank, "setup": cand.setup_name,
                    "confidence": signal.confidence,
                    "reasoning": (signal.reasoning or [])[:4],
                })
                # Telegram entry alert: what / where / stop / target / why.
                try:
                    from src.alerts import send_signal_alert
                    send_signal_alert(cand.to_dict())
                except Exception as exc:
                    logger.warning("Entry alert failed for %s: %s",
                                   signal.symbol, exc)
            except Exception as exc:
                logger.error("Error placing order for %s: %s",
                             signal.symbol, exc)

        return signals

    def _maybe_eod_square_off(self, now_ist=None) -> List[Dict[str, Any]]:
        """Square off all positions once, after the session end time.

        Runs from the market-closed branch of :meth:`scan_once`, so BOTH
        the CLI loop and the dashboard's polling loop settle daily. The
        flag resets when the next session opens. `now_ist` accepts an
        injected time for tests; production reads the real IST clock.

        Returns the list of closed-trade records (empty if not EOD yet).
        """
        if self._eod_squared or not self.broker.positions:
            return []
        from datetime import datetime, timezone, timedelta

        cfg = load_config()
        ft = cfg.get("forward_test", {})
        try:
            end = ft.get("session_end", "15:15") or "15:15"
            eh, em = (end.split(":"))
            end_min = int(eh) * 60 + int(em)
        except (ValueError, AttributeError):
            return []
        if now_ist is None:
            ist = timezone(timedelta(hours=5, minutes=30))
            now_ist = datetime.now(ist)
        if now_ist.hour * 60 + now_ist.minute < end_min:
            return []  # pre-open / mid-session: not end of day yet
        closed = self.square_off_session(reason="eod-squareoff")
        self._eod_squared = True
        logger.info("EOD square-off: closed %d position(s).", len(closed))
        return closed

    def _session_open(self, now_ist=None) -> bool:
        """True when the NSE session window covers right now (IST).

        Window comes from config `forward_test.session_start/session_end`
        ("09:20"/"15:15"). `now_ist` accepts an injected time for tests;
        production path reads the real IST wall clock. A late stop hit on
        the closing-auction print still counts — only new entries stop.
        """
        from datetime import datetime, timezone, timedelta

        cfg = load_config()
        ft = cfg.get("forward_test", {})
        try:
            sh, sm = (ft.get("session_start", "09:20") or "09:20").split(":")
            eh, em = (ft.get("session_end", "15:15") or "15:15").split(":")
            start = (int(sh), int(sm))
            end = (int(eh), int(em))
        except (ValueError, AttributeError):
            return True  # misconfigured window must not halt trading

        if now_ist is None:
            ist = timezone(timedelta(hours=5, minutes=30))
            now = datetime.now(ist).time()
            cur = (now.hour, now.minute)
        else:
            cur = (now_ist.hour, now_ist.minute)
        return start <= cur <= end

    def _manage_open_positions(self) -> None:
        """Check trailing stops, fixed stops and targets on open positions.

        Fetches the latest price per open symbol and closes the paper
        position if its stop or target was touched.
        """
        if not self.broker.positions:
            return

        for sym in list(self.broker.positions.keys()):
            try:
                md = fetch_market_data(sym, self.timeframe)
                price = md.latest_price
                if price is None:
                    continue
                # mark to market for pnl tracking
                self.broker.update_position(sym, price)

                pos = self.broker.positions.get(sym)
                if pos is None:
                    continue

                # Walk the stored SL/target from the opening order.
                sl = None
                tgt = None
                for order in self.broker.orders.values():
                    if order.symbol == sym and order.status == "FILLED":
                        if order.stop_loss:
                            sl = order.stop_loss
                        if order.target:
                            tgt = order.target

                if sl and price <= sl and pos.side == "LONG":
                    rec = self.broker.close_position(sym, price, "stop-hit")
                    if rec:
                        self._log_event("close", rec)
                elif sl and price >= sl and pos.side == "SHORT":
                    rec = self.broker.close_position(sym, price, "stop-hit")
                    if rec:
                        self._log_event("close", rec)
                elif tgt and price >= tgt and pos.side == "LONG":
                    rec = self.broker.close_position(sym, price, "target-hit")
                    if rec:
                        self._log_event("close", rec)
                elif tgt and price <= tgt and pos.side == "SHORT":
                    rec = self.broker.close_position(sym, price, "target-hit")
                    if rec:
                        self._log_event("close", rec)
            except Exception as exc:
                logger.error("Error managing %s: %s", sym, exc)

    def _guard_open_positions(self) -> List[Dict[str, Any]]:
        """Active Manager — Ladder (Phase 1 + Phase 2).

        Phase 1 (always): T1 at +0.8R — close 50%, move rest to breakeven.
        Phase 2 (shadow until ladder.enabled_phase2): T2 at +1.5R — close
        30% of original, arm the trailing stop on the 20% runner; T3 is
        the runner's trailing exit. Shadow logs are emitted without acting
        so forward paper can measure them vs the breakeven hold.

        Runs every `guard_interval` seconds. Stops/targets stay in
        _manage_open_positions for the slow scan; this guard only arms
        the scale-outs.
        """
        exited: List[Dict[str, Any]] = []
        try:
            provider = getattr(self, "broker_name", "paper")
        except Exception:
            provider = "paper"
        if provider != "paper":
            return exited
        if not self.broker.positions:
            return exited

        cfg = load_config()
        rm_cfg = cfg.get("risk_management", {})
        ladder_cfg = rm_cfg.get("ladder", {}) if isinstance(
            rm_cfg.get("ladder"), dict) else {}
        t1_at_r = ladder_cfg.get("t1_at_r", rm_cfg.get("partial_at_r", 0.8))
        t2_at_r = ladder_cfg.get("t2_at_r", 1.5)
        t1_frac = ladder_cfg.get("t1_fraction",
                                  rm_cfg.get("partial_fraction", 0.5))
        t2_frac = ladder_cfg.get("t2_fraction", 0.30)
        phase2 = bool(ladder_cfg.get("enabled_phase2", False))
        trail_mult = float(((ladder_cfg.get("trailing_after_t2") or {}).get(
            "atr_multiplier")) or 3.0)

        def _atr() -> Optional[float]:
            try:
                md = fetch_market_data(sym, self.timeframe)
                rd = compute_indicators(md.ohlcv, sym, self.timeframe)
                for k, ind in rd.indicators.items():
                    if k.startswith("atr_") and ind.value:
                        return float(ind.value)
            except Exception:
                pass
            return None

        for sym in list(self.broker.positions.keys()):
            pos = self.broker.positions.get(sym)
            if pos is None: continue

            try:
                md = fetch_market_data(sym, self.timeframe)
                price = md.latest_price
                if price is None: continue
                self.broker.update_position(sym, price)

                order = next((o for o in reversed(list(
                    self.broker.orders.values()))
                    if o.symbol == sym and o.status == "FILLED"), None)
                if order is None or not order.stop_loss: continue
                sl = order.stop_loss

                risk = abs(pos.avg_price - sl)
                if risk <= 0: continue
                rr = ((price - pos.avg_price) / risk if pos.side == "LONG"
                      else (pos.avg_price - price) / risk)

                # Ladder T1: 50% at +0.8R, rest to breakeven (risk-free)
                if rr >= t1_at_r and not pos.halved:
                    rec = self.broker.close_position(
                        sym, price, "partial@T1-0.8R", fraction=t1_frac)
                    if rec:
                        logger.info("LADDER T1 %s @ %.2f (%.1f%%) — "
                                    "half closed, stop to breakeven",
                                    sym, price, rec["pnl_pct"])
                        self._log_event("guard", {"symbol": sym,
                                                  "price": price,
                                                  "rr": round(rr, 2),
                                                  "decision": "partial@T1-0.8R",
                                                  "record": rec})
                    order.stop_loss = pos.avg_price
                    pos.halved = True
                    setattr(pos, "ladder_t1_hit", True)

                if not getattr(pos, "ladder_t1_hit", False):
                    # Still eligible for shadow T1 invalidation check
                    pass
                else:
                    # SHADOW: T2 at +1.5R (30% scale + trail), runner T3 trailing.
                    pass  # handled next block; fallthrough keeps rr check

                # Ladder Phase 2: handled here (shadow unless enabled).
                # T1 must have fired before T2/T3 are eligible — the runner
                # without breakeven breaks the risk-free invariant.
                if getattr(pos, "ladder_t1_hit", False) \
                        and rr >= t2_at_r and not getattr(
                            pos, "ladder_t2_hit", False):
                    if phase2:
                        # Live: 30% at T2, then arm the trailing stop on the 20% runner.
                        rec = self.broker.close_position(
                            sym, price, "partial@T2-1.5R", fraction=t2_frac)
                        if rec:
                            logger.info("LADDER T2 %s @ %.2f (%.1f%%) — "
                                        "trailing armed",
                                        sym, price, rec["pnl_pct"])
                            self._log_event("guard", {"symbol": sym,
                                                      "price": price,
                                                      "rr": round(rr, 2),
                                                      "decision": "partial@T2-1.5R",
                                                      "record": rec})
                        setattr(pos, "ladder_t2_hit", True)
                        try:
                            atr_v = _atr()
                            if atr_v:
                                trail = (price - atr_v * trail_mult
                                         if pos.side == "LONG"
                                         else price + atr_v * trail_mult)
                                order.stop_loss = float(trail)
                        except Exception:
                            pass
                    else:
                        self._log_event("shadow-exit", {
                            "symbol": sym, "side": pos.side,
                            "entry": pos.avg_price, "price": price,
                            "rr": round(rr, 2),
                            "reason": "ladder-T2", "would_be": "partial@T2-1.5R",
                        })

                    # 2. Shadow invalidation (log-only; never closes)
                    try:
                        _shadow = compute_indicators(
                            fetch_market_data(sym, self.timeframe).ohlcv, sym, self.timeframe)
                        if self._should_shadow_exit(pos, price, _shadow):
                            self._log_event("shadow-exit", {
                                "symbol": sym, "side": pos.side,
                                "entry": pos.avg_price, "price": price,
                                "rr": round(rr, 2),
                                "reason": "thesis-invalidation",
                                "would_be": "exit",
                            })
                    except Exception:
                        pass

                    # 3. Hard stop check (skip if a partial just fired)
                    if not (rr >= pt_r and not pos.halved):
                        if (pos.side == "LONG" and price <= sl) or \
                                (pos.side == "SHORT" and price >= sl):
                            rec = self.broker.close_position(sym, price, "stop-hit")
                            if rec:
                                self._log_event("close", rec)
                        elif (pos.side == "LONG" and price >= tgt) or \
                                (pos.side == "SHORT" and price <= tgt):
                            rec = self.broker.close_position(sym, price, "target-hit")
                            if rec:
                                self._log_event("close", rec)
            except Exception as exc:
                logger.error("Guard failed for %s: %s", sym, exc)
        return exited

    def _should_shadow_exit(self, pos: Any, price: float, readings: Any) -> bool:
        """Rule-based thesis-invalidation signal (SHADOW ONLY — never closes).

        Fires when the position is behind its entry AND MACD has flipped
        against it. We log the event for ~2 weeks and compare shadow-exit
        P&L vs the real stop/target outcome before ever acting on it.
        """
        try:
            is_long = pos.side == "LONG"
            behind = price <= pos.avg_price if is_long else price >= pos.avg_price
            if not behind:
                return False
            indicators = getattr(readings, "indicators", {}) or {}
            macd = indicators.get("macd")
            sig = None
            if macd is not None and getattr(macd, "extra", None):
                sig = macd.extra.get("signal_line")
            if macd is None or macd.value is None or sig is None:
                return True  # behind entry, no MACD data: flag for review
            return macd.value < sig if is_long else macd.value > sig
        except Exception:
            return False

    def _analyze_symbol(self, symbol: str) -> TradeSignal:
        """Run full analysis pipeline for a single symbol."""
        # Fetch data
        market_data = fetch_market_data(symbol, self.timeframe)
        df = market_data.ohlcv

        # Compute indicators
        readings = compute_indicators(df, symbol, self.timeframe)

        # Sentiment (may be cached)
        cfg = load_config()
        sentiment = None
        if cfg.get("sentiment", {}).get("enabled", False):
            try:
                sentiment = analyze_sentiment(symbol)
            except Exception as exc:
                logger.warning("Sentiment analysis failed for %s: %s", symbol, exc)

        # Generate signal
        signal = generate_signal(
            symbol=symbol,
            latest_price=market_data.latest_price,
            technical_readings=readings,
            sentiment_result=sentiment,
        )

        # Get ATR for risk management
        atr_value = None
        for key, ind in readings.indicators.items():
            if key.startswith("atr_"):
                atr_value = ind.value
                break

        # Apply risk management (swing-based stop, matching the backtest)
        swing = readings.swing_low if signal.action == "BUY" else readings.swing_high
        signal = apply_risk_management(signal, atr_value, swing_level=swing)

        return signal

    def run(self, interval_seconds: int = 300, max_iterations: Optional[int] = None) -> None:
        """
        Run the paper trading loop: fast guard pass + slow entry pass.

        EOD square-off: once the session window closes (15:15 IST), the
        entry pass stops — but any open positions are squared off ONCE at
        their latest print first, instead of sitting open forever. On
        2026-10-06 the loop held 2 positions 25 minutes past the close
        with no exit, no alert, no record.
        """
        cfg = load_config()
        guard_interval = cfg.get("forward_test", {}).get("guard_interval_seconds", 60)
        # Caller-provided interval (--interval) overrides config, so GitHub ticks
        # and local runs honour the requested scan rate.
        entry_interval = interval_seconds if interval_seconds is not None else \
            cfg.get("forward_test", {}).get("interval_seconds", 300)

        logger.info("Starting (dual-clock) paper trading loop...")

        last_guard_run = 0
        last_entry_run = 0
        iteration = 0

        try:
            while True:
                now = time.time()

                # Guard pass (fast clock: re-scores open positions)
                if now - last_guard_run >= guard_interval:
                    self._guard_open_positions()
                    last_guard_run = now

                # Entry pass (slow clock: screens for new trades)
                if now - last_entry_run >= entry_interval:
                    iteration += 1
                    logger.info("--- Scan %d at %s ---", iteration, utc_now())
                    self.scan_once()  # EOD square-off happens inside
                    last_entry_run = now

                time.sleep(1) # Keep CPU load down

                if max_iterations and iteration >= max_iterations:
                    break

        except KeyboardInterrupt:
            logger.info("Paper trading stopped by user.")

        self.square_off_session()
        # ... remainder of log ...

    def square_off_session(self, reason: str = "squareoff") -> List[Dict[str, Any]]:
        """Close every open position at its latest price.

        Called at session end (EOD, auto) and at the end of `run()`
        (Ctrl+C). Each close lands in `broker.closed_trades` with a
        PASS/FAIL result, so the dashboard and the session log always
        show the full scoreboard.
        """
        closed = []
        for sym in list(self.broker.positions.keys()):
            try:
                md = fetch_market_data(sym, self.timeframe)
                if md.latest_price is None:
                    continue
                rec = self.broker.close_position(sym, md.latest_price,
                                                 reason)
                if rec:
                    closed.append(rec)
                    self._log_event("close", rec)
            except Exception as exc:
                logger.error("Square-off failed for %s: %s", sym, exc)
        self._write_trade_log_csv()
        return closed

    def _log_event(self, event: str, payload: Dict[str, Any]) -> None:
        """Append one audit event to the trade log (JSONL, one line each).

        Every record is stamped with ``mode`` (``paper`` | ``test``) and
        ``broker`` so smoke-test orders and live orders can be separated
        in analysis — 2026-10-06 mixed both in one file, booking −700 of
        test P&L into the live ledger.

        Never raises: a logging failure must not break trading.
        """
        try:
            target = self.trade_log_path
            # Tests must NEVER append to the configured live ledger, even
            # when a test forgets test_mode=True. A test that deliberately
            # points trade_log_path at tmp_path still gets its own file.
            force_sidecar = self.test_mode or (
                os.environ.get("TRIO_TEST_MODE") == "1"
                and Path(target) == Path(self._live_log_path))
            if force_sidecar:
                target = target.parent / (
                    target.stem + "_test" + target.suffix)
            target.parent.mkdir(parents=True, exist_ok=True)
            rec = {"ts": utc_now(), "event": event,
                   "mode": "test" if force_sidecar else "paper",
                   "broker": getattr(self, "broker_name", "paper")}
            rec.update(payload if isinstance(payload, dict) else {"data": payload})
            with open(target, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except Exception as exc:
            logger.warning("Trade-log write failed: %s", exc)

    def _write_trade_log_csv(self) -> None:
        """Mirror the JSONL trade log as a CSV for spreadsheet review."""
        try:
            import json as _json
            if not self.trade_log_path.exists():
                return
            rows = []
            with open(self.trade_log_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        rows.append(_json.loads(line))
            if not rows:
                return
            keys = ["ts", "event", "mode", "broker", "asset", "strategy",
                    "symbol", "action", "entry", "stop",
                    "target", "qty", "rank", "setup", "confidence",
                    "exit", "pnl", "pnl_pct", "reason", "result",
                    "price", "rr", "decision", "scanned", "candidates",
                    "positions", "session_open"]
            csv_path = self.trade_log_path.with_suffix(".csv")
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
                w.writeheader()
                for r in rows:
                    flat = dict(r)
                    cands = flat.get("candidates")
                    if isinstance(cands, list):
                        flat["candidates"] = "; ".join(
                            f"{c.get('symbol')}:{c.get('action')}:"
                            f"{c.get('rank')}" for c in cands[:10])
                    w.writerow(flat)
            logger.info("Trade log CSV written: %s (%d events)",
                        csv_path, len(rows))
        except Exception as exc:
            logger.warning("Trade-log CSV write failed: %s", exc)

    # ------------------------------------------------------------------
    # Premarket plan execution (GitHub session job)
    # ------------------------------------------------------------------

    def rehydrate_orders(self, plan: Dict[str, Any]) -> int:
        """Rebuild local stop/target records for positions opened from a plan.

        On an ephemeral runner ``broker.orders`` starts empty, so the guard
        would not know a position's stop/target. We rebuild one filled
        BrokerOrder per symbol named in today's plan whose position is
        currently open on the broker. Returns the number restored.
        """
        if not plan:
            return 0
        from src.broker.base import BrokerOrder

        try:
            self.broker.refresh_positions()
        except Exception:
            pass
        restored = 0
        for cand in plan.get("candidates", []) or []:
            sym = cand.get("symbol")
            pos = self.broker.positions.get(sym)
            if pos is None or pos.quantity <= 0:
                continue
            already = any(
                getattr(o, "symbol", "") == sym and o.status == "FILLED"
                for o in self.broker.orders.values())
            if already:
                continue
            oid = f"plan_{sym}_{utc_now()}"
            self.broker.orders[oid] = BrokerOrder(
                order_id=oid, symbol=sym, side=pos.side,
                quantity=pos.quantity, order_type="PLAN",
                price=float(pos.avg_price or 0.0),
                stop_loss=float(cand.get("stop_loss") or 0.0),
                target=float(cand.get("target") or 0.0),
                status="FILLED", broker=getattr(self, "broker_name", "paper"),
                placed_at=utc_now(), filled_at=utc_now(),
            )
            restored += 1
        if restored:
            logger.info("Rehydrated stop/target for %d open plan position(s).",
                        restored)
        return restored

    def execute_option_plan(self, plan: Optional[Dict[str, Any]],
                              opt_broker: Any = None) -> List[Dict[str, Any]]:
        """Place today's option legs (paper) for candidates carrying them.

        Uses the attached ``option_legs`` (spread/condor picked by
        options_selector) on the options paper broker — never touches the
        equity broker. Lots sized by ``risk_amount / net_debit`` capped at
        one spread per candidate. Shadow until proven: the caller decides.
        Returns the list of placed spread records.
        """
        if not plan:
            return []
        if opt_broker is None:
            try:
                from src.broker.options_paper import OptionsPaperBroker
                opt_broker = OptionsPaperBroker(
                    initial_capital=(load_config().get(
                        "risk_management", {}) or {}).get("capital", 500000))
            except Exception as exc:
                logger.warning("execute_option_plan: no options broker: %s", exc)
                return []
        placed: List[Dict[str, Any]] = []
        for cand in plan.get("candidates", []) or []:
            opt = cand.get("option_legs") or {}
            if not opt or opt.get("strategy") in (None, "", "none"):
                continue
            try:
                from src.options_selector import OptionTradeCandidate, OptionLeg
                legs = [OptionLeg(**{k: l.get(k, v) for k, v in
                                      OptionLeg().__dict__.items()})
                        for l in (opt.get("legs") or [])]
                pick = OptionTradeCandidate(
                    strategy=opt.get("strategy", ""),
                    underlying=opt.get("underlying", "NIFTY"),
                    spot=float(opt.get("spot") or 0.0),
                    expiry=str(opt.get("expiry") or ""),
                    legs=legs, net_debit=float(opt.get("net_debit") or 0.0),
                    maxLoss=float(opt.get("maxLoss") or 0.0),
                    maxGain=float(opt.get("maxGain") or 0.0),
                    breakeven=float(opt.get("breakeven") or 0.0),
                    rank=float(opt.get("rank") or 0.0),
                    confidence=int(opt.get("confidence")
                                   or cand.get("confidence") or 0))
                risk_amt = float(cand.get("risk_amount") or 0.0)
                lot_sz = int(load_config().get("options", {}).get(
                    "lot_size", 50))
                lots = 1
                if pick.net_debit > 0 and risk_amt > 0:
                    lots = max(1, int(risk_amt // (pick.net_debit * lot_sz)))
                order = opt_broker.open_spread(pick, lots=lots,
                                               reason="options-plan")
                if order.status != "FILLED":
                    self._log_event("skip", {
                        "symbol": cand.get("symbol"), "asset": "options",
                        "strategy": pick.strategy,
                        "reason": f"rejected: {order.error}"})
                    # MegaBull silently rejects option legs (endpoint
                    # refused lots=1/units=50/75 on 2026-10-08). When the
                    # remote broker is selected but a leg can't fill, keep
                    # the LOCAL paper sim as the shadow book so the
                    # overlay's P&L/marking stays testable and the equity
                    # session is never blocked.
                    if "megabull" in str(
                            getattr(opt_broker, "provider_name", "")).lower():
                        try:
                            from src.broker.options_paper import (
                                OptionsPaperBroker)
                            shadow = OptionsPaperBroker(
                                initial_capital=self.broker.capital
                                if hasattr(self.broker, "capital")
                                else 500000.0,
                                lot_size=lot_sz)
                            s_order = shadow.open_spread(
                                pick, lots=lots, reason="options-shadow")
                            if s_order.status == "FILLED":
                                record = {
                                    "symbol": cand.get("symbol"),
                                    "asset": "options",
                                    "strategy": pick.strategy,
                                    "expiry": pick.expiry,
                                    "net_debit": pick.net_debit,
                                    "lots": lots,
                                    "maxLoss": pick.maxLoss,
                                    "maxGain": pick.maxGain,
                                    "order_id": s_order.order_id,
                                    "rank": cand.get("rank"),
                                    "setup": cand.get("setup_name"),
                                    "shadow": True,
                                    "venue": "local-sim",
                                }
                                placed.append(record)
                                self._log_event("order", record)
                                if not hasattr(self, "_opt_shadows"):
                                    self._opt_shadows = []
                                self._opt_shadows.append((shadow, s_order))
                                logger.info(
                                    "options-shadow booked locally for %s "
                                    "(MegaBull leg rejected).",
                                    cand.get("symbol"))
                        except Exception as sh_exc:
                            logger.warning(
                                "Options shadow fallback failed: %s", sh_exc)
                    continue
                record = {
                    "symbol": cand.get("symbol"), "asset": "options",
                    "strategy": pick.strategy, "expiry": pick.expiry,
                    "net_debit": pick.net_debit, "lots": lots,
                    "maxLoss": pick.maxLoss, "maxGain": pick.maxGain,
                    "order_id": order.order_id,
                    "rank": cand.get("rank"),
                    "setup": cand.get("setup_name"),
                    "venue": getattr(
                        opt_broker, "provider_name", "options_paper"),
                }
                placed.append(record)
                self._log_event("order", record)
            except Exception as exc:
                logger.error("execute_option_plan failed for %s: %s",
                             cand.get("symbol"), exc)
        if placed:
            logger.info("execute_option_plan: placed %d spread(s).", len(placed))
        return placed

    def execute_plan(self, plan: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Place today's premarket candidates on the broker.

        Skips symbols already held on the same side (idempotent across
        re-runs and ephemeral runners, since the broker book is refreshed
        from MegaBull). Honours the CNC settlement rule for SELLs.
        Returns the list of placed order records.
        """
        if not plan:
            logger.warning("execute_plan: no plan supplied.")
            return []
        cfg = load_config()
        allow_shorts = cfg.get("trading", {}).get("allow_shorts", False)
        max_open = cfg.get("risk_management", {}).get("max_open_positions", 5)

        try:
            self.broker.refresh_positions()
        except Exception as exc:
            logger.warning("execute_plan: position refresh failed: %s", exc)

        placed: List[Dict[str, Any]] = []
        for cand in plan.get("candidates", []) or []:
            if len(self.broker.positions) >= max_open:
                logger.info("execute_plan: max_open_positions (%d) reached.",
                            max_open)
                break
            signal = TradeSignal(
                symbol=cand.get("symbol", ""),
                action=cand.get("action", "HOLD"),
                entry_price=cand.get("entry_price"),
                stop_loss=cand.get("stop_loss"),
                target=cand.get("target"),
                position_size=int(cand.get("position_size") or 0),
                confidence=int(cand.get("confidence") or 0),
                reasoning=list(cand.get("reasoning") or []),
            )
            if signal.action not in ("BUY", "SELL") or not signal.position_size:
                continue

            held = self.broker.positions.get(signal.symbol)
            same_side = (
                held is not None and held.quantity > 0 and (
                    (signal.action == "SELL" and held.side == "SHORT")
                    or (signal.action == "BUY" and held.side == "LONG")))
            if same_side:
                logger.info("execute_plan: SKIP %s %s (already holding).",
                            signal.action, signal.symbol)
                continue

            if signal.action == "SELL" and not allow_shorts:
                held_qty = held.quantity if held and held.side == "LONG" else 0
                if held_qty <= 0:
                    logger.warning("execute_plan: SKIP SELL %s (no holdings).",
                                   signal.symbol)
                    self._log_event("skip", {
                        "symbol": signal.symbol, "action": "SELL",
                        "rank": cand.get("rank"), "setup": cand.get("setup_name"),
                        "reason": "no-holdings"})
                    continue
                signal.position_size = min(signal.position_size, held_qty)

            try:
                order = self.broker.place_order(
                    symbol=signal.symbol, side=signal.action,
                    quantity=signal.position_size, price=signal.entry_price,
                    stop_loss=signal.stop_loss, target=signal.target,
                    reason="premarket-plan",
                )
            except Exception as exc:
                logger.error("execute_plan: order error for %s: %s",
                             signal.symbol, exc)
                continue

            if order.status != "FILLED":
                logger.warning("execute_plan: %s rejected: %s",
                               signal.symbol, order.error)
                self._log_event("skip", {
                    "symbol": signal.symbol, "action": signal.action,
                    "rank": cand.get("rank"), "setup": cand.get("setup_name"),
                    "reason": f"rejected: {order.error}"})
                continue

            record = {
                "symbol": signal.symbol, "action": signal.action,
                "entry": signal.entry_price, "stop": signal.stop_loss,
                "target": signal.target, "qty": signal.position_size,
                "rank": cand.get("rank"), "setup": cand.get("setup_name"),
                "confidence": signal.confidence, "order_id": order.order_id,
            }
            placed.append(record)
            self._log_event("order", record)
            try:
                from src.alerts import send_signal_alert
                send_signal_alert({**cand, "order_id": order.order_id})
            except Exception as exc:
                logger.warning("Entry alert failed for %s: %s",
                               signal.symbol, exc)

        logger.info("execute_plan: placed %d order(s).", len(placed))
        return placed

    def run_session(self, plan: Optional[Dict[str, Any]] = None,
                    interval_seconds: int = 60,
                    max_seconds: Optional[int] = None,
                    tick_hook: Any = None) -> None:
        """Session job: execute the plan once, then manage until EOD.

        Designed for a single GitHub Actions job (09:15–15:10 IST). Places
        the premarket plan if the session is open (otherwise holds until
        the next open), rebuilds stop/target state, and loops guard + manage
        every ``interval_seconds`` until the session closes (EOD square-off
        fires inside ``scan_once`` when the window shuts) or ``max_seconds``
        elapses. New entries only come from the plan.

        ``tick_hook`` (optional callable, no args) runs at the END of every
        guard tick — used by scripts/session.py to mark option spreads to
        the live chain mid. Never raises out of this loop.
        """
        if self._session_open():
            self.execute_plan(plan)
        else:
            logger.info("Session is CLOSED — plan execution delayed "
                        "until the next open (managing only).")
            # Still rebuild stop/target for any positions held, so the guard
            # can manage exits on the next direct tick.
            self.rehydrate_orders(plan or {})

        start = time.time()
        plan_done = plan is None  # no plan -> nothing to execute
        while True:
            try:
                if not plan_done and self._session_open():
                    self.execute_plan(plan)
                    plan_done = True
                # Manage existing positions (stops/targets/partials).
                self._guard_open_positions()
                self._manage_open_positions()
                # EOD: square off once the window closes.
                self._maybe_eod_square_off()
                self._log_event("scan", {
                    "session_open": self._session_open(),
                    "positions": len(self.broker.positions)})
                try:
                    from src.alerts import handle_joins
                    handle_joins()
                except Exception:
                    pass
                if tick_hook is not None:
                    try:
                        tick_hook()
                    except Exception as hook_exc:
                        logger.warning("run_session tick_hook failed: %s",
                                       hook_exc)
            except Exception as exc:
                logger.error("run_session tick failed: %s", exc)
            # Session closed after open -> final square-off and exit.
            if plan_done and not self._session_open():
                logger.info("Session closed — final square-off.")
                self._maybe_eod_square_off()
                break
            if max_seconds and (time.time() - start) >= max_seconds:
                logger.info("run_session: max_seconds reached.")
                break
            time.sleep(max(5, interval_seconds))
        self._write_trade_log_csv()
