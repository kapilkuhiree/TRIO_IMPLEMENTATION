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

        # Provider MUST be explicit. Any typo like "megabul" now fails fast
        # instead of silently returning a paper simulator that hides the error.
        allowed = {"paper", "megabull"}
        # Optional: add "zerodha" here when that adapter is fully implemented.
        raw = provider = (
            os.environ.get("TRIO_BROKER_PROVIDER", "").strip().lower()
            or (cfg.get("broker", {}) or {}).get("provider", "paper")
        )
        if raw not in allowed:
            raise ValueError(
                f"Unknown broker provider '{raw}' — allowed: {sorted(allowed)}. "
                f"Refusing to trade silently on a typoed simulator.")
        # MegaBull paper requires intent: env or config "megabull" plus a
        # non-empty API key, otherwise fail-closed before any order.
        if provider == "megabull":
            from src.broker.megabull import MegaBullBroker, load_dotenv_key
            api_key = os.environ.get("MEGABULL_API_KEY", "") or \
                load_dotenv_key("MEGABULL_API_KEY")
            if not api_key:
                raise ValueError(
                    "Broker 'megabull' selected but no MEGABULL_API_KEY found "
                    "in the environment or .env — refusing to run without credentials.")
            # Live mode must be confirmed explicitly: TRIO_LIVE=1 or
            # forward_test.live_confirmed:true — never silently extend the
            # remote paper balance to a real account.
            if str(cfg.get("mode", "")).lower() in ("live", "real") \
                    and not str(os.environ.get("TRIO_LIVE", "")).strip():
                if not bool((cfg.get("forward_test", {}) or {}).get("live_confirmed")):
                    raise RuntimeError(
                        "Config 'mode: live' requires TRIO_LIVE=1 or "
                        "forward_test.live_confirmed: true to proceed.")
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
        """Square off all positions once, at/after the hard-flat time.

        Hard-flat comes from :mod:`market_clock` (market.yaml or the legacy
        forward_test fallback), defaulting to 15:10 IST when no config is
        available. The flag resets when the next session opens.

        `now_ist` may be None (real clock) or a test-injected time. When
        None, the clock's time-only path is used so a stale position can
        still square off on a non-trading day (tests patch session_end to
        00:00 and run on Saturdays, where the weekday check would otherwise
        block the EOD).

        Returns the list of closed-trade records (empty if not EOD yet).
        """
        if self._eod_squared or not self.broker.positions:
            return []
        # For EOD, treat "right now" (None) as a time-only query so the
        # hard-flat minute always wins, regardless of the weekday/holiday
        # (a stale position must close even if today is a Saturday in the
        # test runner).
        if now_ist is None:
            from datetime import datetime, timezone, timedelta
            now_for_clock = datetime.now(
                timezone(timedelta(hours=5, minutes=30))).time()
        else:
            now_for_clock = now_ist
        try:
            from src.market_clock import market_clock_from_config
            clock = market_clock_from_config(load_config())
            if not clock.is_hard_flat(now_for_clock):
                return []
        except Exception:
            # If the clock cannot be built, never auto-flat — the runner
            # must be told explicitly to close.
            return []
        closed = self.square_off_session(reason="eod-squareoff")
        self._eod_squared = True
        logger.info("EOD square-off: closed %d position(s).", len(closed))
        return closed

    def _session_open(self, now_ist=None) -> bool:
        """True when the runner is still allowed to open NEW positions (IST).

        Delegates to :mod:`market_clock` (market.yaml or the legacy
        forward_test block). Invalid config or a wrong timezone returns
        False — management-only, never fail-open. This is the Phase-1.4/2
        entry-cutoff gate (exclusive): at ``entry_cutoff`` entries stop
        while the runner stays alive to manage and hard-flat.
        """
        try:
            from src.market_clock import market_clock_from_config
            return market_clock_from_config(load_config()).can_enter(now_ist)
        except Exception:
            return False

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

        Runs every `guard_interval` seconds. Owns the full lifecycle of an
        open position: ladder scale-outs AND the hard stop/target, so a
        runner can never fall out of management between the slow scans.
        The ORIGINAL risk (entry -> initial stop) is frozen on first sight
        (`pos.initial_stop`) because T1 moves the live stop to breakeven.
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

                # The guard's stop/target live on the ORIGINAL entry fill.
                # After a T1/T2 partial an exit fill shares the same symbol
                # and PaperBroker stamps it stop_loss=0.0/target=0.0 — so we
                # must (a) match the entry side and (b) require a TRUTHY stop,
                # else the runner gets "target-hit at 0.0" on the next tick.
                entry_side = ("BUY" if pos.side == "LONG" else "SELL")
                order = next((o for o in list(self.broker.orders.values())
                              if o.symbol == sym and o.status == "FILLED"
                              and getattr(o, "side", "") == entry_side
                              and getattr(o, "stop_loss", 0)), None)
                if order is None:
                    # Fallback for SimpleNamespace fixtures without `side`.
                    order = next((o for o in reversed(list(
                        self.broker.orders.values()))
                        if o.symbol == sym and o.status == "FILLED"
                        and getattr(o, "stop_loss", 0)), None)
                if order is None: continue
                sl = float(order.stop_loss)

                # Freeze the ORIGINAL risk (entry -> initial stop) the first
                # time we see this position. After T1 the active stop moves to
                # breakeven, so recomputing risk from the live stop would be
                # zero and the runner would silently fall out of management
                # (no T2, no stop, no target). R is always vs the initial stop.
                orig_stop = getattr(pos, "initial_stop", None)
                if orig_stop is None:
                    orig_stop = sl
                    try:
                        setattr(pos, "initial_stop", sl)
                    except Exception:
                        pass
                risk = abs(pos.avg_price - orig_stop)
                if risk <= 0: continue
                rr = ((price - pos.avg_price) / risk if pos.side == "LONG"
                      else (pos.avg_price - price) / risk)

                # Ladder T1: 50% at +0.8R, rest to breakeven (risk-free).
                # One-unit rule: if T1 cannot close any quantity (int(1*0.5)==0)
                # the ladder state is left untouched — the position stays
                # fully open and eligible to retry on the next tick.
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
                        try:
                            from src.risk_manager import update_pnl as _rm_p
                            _rm_p(float(rec.get("pnl") or 0.0))
                        except Exception:
                            pass

                # Ladder Phase 2: requires T1, eligible at +1.5R.
                # Shadow unless ladder.enabled_phase2. One-unit T2 uses the
                # same rule: no quantity -> leave hit-flag unset.
                if getattr(pos, "ladder_t1_hit", False) \
                        and rr >= t2_at_r and not getattr(
                            pos, "ladder_t2_hit", False):
                    if phase2:
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
                                from src.risk_manager import update_pnl as _rm_p2
                                _rm_p2(float(rec.get("pnl") or 0.0))
                            except Exception:
                                pass
                            try:
                                atr_v = _atr()
                                if atr_v:
                                    trail = (price - atr_v * trail_mult
                                             if pos.side == "LONG"
                                             else price + atr_v * trail_mult)
                                    order.stop_loss = float(trail)
                            except Exception:
                                pass
                        # else: no quantity -> do NOT set ladder_t2_hit
                    else:
                        self._log_event("shadow-exit", {
                            "symbol": sym, "side": pos.side,
                            "entry": pos.avg_price, "price": price,
                            "rr": round(rr, 2),
                            "reason": "ladder-T2", "would_be": "partial@T2-1.5R",
                        })
                # Mark-to-market push: a red book halts the runner even
                # before a full stop is hit (realized + unrealized check).
                try:
                    from src.risk_manager import _check_halt as _gc2
                    total = 0.0
                    for _p in self.broker.positions.values():
                        total += float(getattr(_p, "pnl", 0.0) or 0.0)
                    _gc2(unrealized_total=total)
                except Exception:
                    pass

                # Shadow invalidation (log-only; never closes)
                try:
                    if rr >= t1_at_r * 0.5:
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

                # Hard stop/target — always checked, ladder state irrelevant.
                # The active target lives on the original FILLED order; the
                # active stop is either the original sl or (post-T1)
                # breakeven/trailing after ladder actions.
                tgt_val = getattr(order, "target", 0) or None
                stop_hit = ((pos.side == "LONG" and price <= sl) or
                            (pos.side == "SHORT" and price >= sl))
                tgt_hit = False
                if tgt_val is not None:
                    tgt_hit = ((pos.side == "LONG" and price >= float(tgt_val)) or
                               (pos.side == "SHORT" and price <= float(tgt_val)))
                if stop_hit:
                    rec = self.broker.close_position(sym, price, "stop-hit")
                    if rec:
                        self._log_event("close", rec)
                        try:
                            from src.risk_manager import (
                                close_position as _rm_close,
                                update_pnl as _rm_pnl,
                            )
                            _rm_pnl(float(rec.get("pnl") or 0.0))
                            _rm_close(sym)
                        except Exception:
                            pass
                elif tgt_hit:
                    rec = self.broker.close_position(sym, price, "target-hit")
                    if rec:
                        self._log_event("close", rec)
                        try:
                            from src.risk_manager import (
                                close_position as _rm_close,
                                update_pnl as _rm_pnl,
                            )
                            _rm_pnl(float(rec.get("pnl") or 0.0))
                            _rm_close(sym)
                        except Exception:
                            pass
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
                    try:
                        from src.risk_manager import (
                            close_position as _rm_close,
                            update_pnl as _rm_pnl,
                            _check_halt as _rm_check,
                        )
                        pnl = float(rec.get("pnl") or 0.0)
                        _rm_pnl(pnl)
                        _rm_close(sym)
                    except Exception:
                        pass
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
        # Idempotency (2026-10-09: execute_option_plan runs on EVERY pass —
        # startup + each live re-scan — and each pass opened a FRESH spread
        # without checking, booking the same loss 8x). One spread id per
        # session, across the main book AND every shadow book. The set is
        # keyed by provider so a MegaBull-held spread and a local shadow
        # never collide.
        if not hasattr(self, "_opt_spreads_placed"):
            self._opt_spreads_placed = set()
        max_spreads = int((load_config().get("options", {}) or {}).get(
            "max_spreads_per_session", 4))
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
                provider = str(getattr(
                    opt_broker, "provider_name", "options_paper"))
                longs = [l for l in legs if l.side == "BUY"]
                shorts = [l for l in legs if l.side == "SELL"]
                sid_key = (provider, pick.strategy, pick.expiry,
                           float(longs[0].strike) if longs else 0.0,
                           float(shorts[0].strike) if shorts else 0.0)
                if sid_key in self._opt_spreads_placed:
                    logger.info(
                        "execute_option_plan: SKIP %s %s (spread already "
                        "held this session).", cand.get("symbol"),
                        pick.strategy)
                    continue
                if len(self._opt_spreads_placed) >= max_spreads:
                    logger.info(
                        "execute_option_plan: session cap (%d spreads) "
                        "reached — SKIP %s.", max_spreads,
                        cand.get("symbol"))
                    self._log_event("skip", {
                        "symbol": cand.get("symbol"), "asset": "options",
                        "strategy": pick.strategy,
                        "reason": "session-spread-cap"})
                    continue
                risk_amt = float(cand.get("risk_amount") or 0.0)
                lot_sz = int(load_config().get("options", {}).get(
                    "lot_size", 50))
                lots = 1
                if pick.net_debit > 0 and risk_amt > 0:
                    lots = max(1, int(risk_amt // (pick.net_debit * lot_sz)))
                order = opt_broker.open_spread(pick, lots=lots,
                                               reason="options-plan")
                if order.status == "FILLED":
                    self._opt_spreads_placed.add(sid_key)
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
                                # Same spread shape as the main book, so the
                                # next execute_option_plan pass skips it too.
                                self._opt_spreads_placed.add(
                                    ("options_paper", pick.strategy,
                                     pick.expiry,
                                     float(longs[0].strike) if longs else 0.0,
                                     float(shorts[0].strike)
                                     if shorts else 0.0))
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

        # Rebuild risk state from the refreshed broker book (restart-resilient).
        try:
            from src.risk_manager import rebuild_from_ledger
            today = None
            try:
                from datetime import datetime, timezone, timedelta as _td
                IST = timezone(_td(hours=5, minutes=30))
                today = datetime.now(IST).strftime("%Y-%m-%d")
            except Exception:
                pass
            rebuild_from_ledger(self.broker.positions,
                                getattr(self.broker, "closed_trades", []),
                                today=today)
        except Exception:
            pass
        # Hard block when any risk halt is already active (daily loss, max
        # positions backstopped by RiskState, not just config).
        try:
            from src.risk_manager import get_risk_state
            if get_risk_state().halt_active:
                logger.critical("execute_plan: trading halted — %s.",
                                get_risk_state().halt_reason)
                return []
        except Exception:
            pass

        # Broker-uncertainty guard: if the remote book returned a pending/
        # out-of-sync marker, do not trade until it reconciles.
        try:
            raw = getattr(self.broker, "positions", {})
            if any(getattr(p, "status", "") == "PENDING" for p in raw.values()):
                logger.warning("execute_plan: broker has PENDING positions — "
                               "blocking new entries until refresh confirms.")
                return []
        except Exception:
            pass
        # Sector exposure guard (Phase 7).
        try:
            from src.risk_manager import (
                would_breach_sector as _would_breach_sector,  # noqa: E402
                get_risk_state as _grs2,  # noqa
            )
            # Use RiskState positions if available; otherwise mirror broker book.
            try:
                existing = dict(getattr(_grs2(), "positions", {}) or {})
            except Exception:
                existing = {}
            if not existing:
                # Mirror broker book when the RiskState is empty (fresh start).
                for _sym, _pos in (getattr(self.broker, "positions", {}) or {}).items():
                    try:
                        qty = int(getattr(_pos, "quantity", 0) or 0)
                        avg = float(getattr(_pos, "avg_price", 0.0) or 0.0)
                    except Exception:
                        continue
                    existing[_sym] = {"size": qty, "entry": avg}
            _sector_new = [c for c in plan.get("candidates", []) or []
                           if would_breach_sector(
                               c.get("symbol", ""), int(c.get("position_size") or 0),
                               float(c.get("entry_price") or 0), existing=existing)]
            if _sector_new:
                self._log_event("skip", {
                    "reason": "sector-exposure",
                    "symbols": [c.get("symbol") for c in _sector_new],
                })
        except Exception:
            _sector_new = []
        # Available-margin guard (local + remote books both expose get_balance).
        try:
            bal = self.broker.get_balance()
            if float(bal.get("available", 1)) <= 0:
                logger.warning("execute_plan: available margin exhausted — "
                               "blocking new entries.")
                return []
        except Exception:
            pass

        placed: List[Dict[str, Any]] = []
        for cand in plan.get("candidates", []) or []:
            # Per-symbol sector gate: skip only the banking-heavy candidate,
            # not the whole plan.
            try:
                from src.risk_manager import would_breach_sector as _would_breach1
                from src.risk_manager import get_risk_state as _grs0
                try:
                    existing0 = dict(getattr(_grs0(), "positions", {}) or {})
                except Exception:
                    existing0 = {}
                if not existing0:
                    for _sym2, _pos2 in (getattr(self.broker, "positions", {}) or {}).items():
                        try:
                            existing0[_sym2] = {
                                "size": int(getattr(_pos2, "quantity", 0) or 0),
                                "entry": float(getattr(_pos2, "avg_price", 0.0) or 0.0),
                            }
                        except Exception:
                            continue
                if _would_breach1(cand.get("symbol", ""),
                                  int(cand.get("position_size") or 0),
                                  float(cand.get("entry_price") or 0),
                                  existing=existing0):
                    self._log_event("skip", {
                        "symbol": cand.get("symbol"), "action": cand.get("action"),
                        "reason": "sector-exposure"})
                    continue
            except Exception:
                pass
            # Per-candidate risk check: daily loss / halt, margin, fees.
            try:
                from src.risk_manager import get_risk_state as _grs
                if _grs().halt_active:
                    break
                # Fees/slippage estimation: commission + slippage on notional
                cfg2 = cfg
                comm = float(cfg2.get("risk_management", {}).get("backtesting",
                             {}).get("commission_pct", 0) or
                             cfg2.get("backtesting", {}).get("commission_pct", 0) or 0) / 100
                slip = float(cfg2.get("backtesting", {}).get("slippage_pct", 0) or
                             cfg2.get("backtesting", {}).get("slippage_pct", 0) or 0) / 100
                notional = float(cand.get("entry_price") or 0) * int(
                    cand.get("position_size") or 0)
                est_cost = round(notional * (comm + slip), 2)
                try:
                    if float(self.broker.get_balance().get("available", 1e12)) < est_cost:
                        self._log_event("skip", {
                            "symbol": signal.symbol if 'signal' in locals() else cand.get("symbol"),
                            "reason": "margin-after-est-cost"})
                        continue
                except Exception:
                    pass
            except Exception:
                pass

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

            if getattr(order, "status", "") in ("PENDING", "PLACED", "ACCEPTED"):
                logger.warning("execute_plan: %s pending (%s) — not booked "
                               "until confirmed filled.",
                               signal.symbol, order.status)
                self._log_event("skip", {
                    "symbol": signal.symbol, "action": signal.action,
                    "rank": cand.get("rank"),
                    "reason": f"pending: {order.status}"})
                continue
            if order.status != "FILLED":
                # Rejections must not consume risk or be treated as fills.
                logger.warning("execute_plan: %s rejected: %s",
                               signal.symbol, order.error)
                self._log_event("skip", {
                    "symbol": signal.symbol, "action": signal.action,
                    "rank": cand.get("rank"), "setup": cand.get("setup_name"),
                    "reason": f"rejected: {order.error}"})
                continue

            # Confirmed fill: register exposure so halt checks see it immediately.
            try:
                from src.risk_manager import add_position as _add
                _add(signal.symbol, signal.position_size, signal.entry_price,
                     cand.get("risk_amount", 0.0) or 0.0)
            except Exception:
                pass

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

    def _live_rescan(self, slot: str) -> List[Dict[str, Any]]:
        """Rebuild the plan from TODAY's live bars and trade anything new.

        This is the market-hours discovery the user asked for: instead of
        only trading yesterday's premarket file, the session re-runs the
        full screener (+ option legs) on live data at scheduled slots and
        immediately executes fresh candidates. ALWAYS sends a Telegram
        verdict — "found + traded" or "scanned, nothing met the bar" — so
        a quiet market is never mistaken for a dead bot. Never raises.
        """
        try:
            from scripts.premarket import build_plan
            cfg = load_config()
            universe = cfg.get("universe", {}).get("premarket", "nifty100")
            top_n = cfg.get("screener", {}).get("max_positions_to_open", 3)
            fresh = build_plan(universe, "1d", top_n)
            cands = fresh.get("candidates", []) or []
            try:
                from src.plan import save_plan as _save_plan
                from src.plan import today_ist_str
                _save_plan(dict(fresh, date=today_ist_str()))
            except Exception:
                pass
        except Exception as exc:
            logger.warning("Live rescan (%s) failed: %s", slot, exc)
            try:
                from src.alerts import send_telegram
                send_telegram(
                    f"🔍 *Live Market Scan ({slot} IST)*\n\n"
                    f"Scan failed ({exc}). Still managing open positions.")
            except Exception:
                pass
            return []
        if not cands:
            logger.info("Live rescan (%s): scanned, nothing met the bar.", slot)
            try:
                from src.alerts import send_telegram
                send_telegram(
                    f"🔍 *Live Market Scan ({slot} IST)*\n\n"
                    f"Scanned the live market — no new setup met the bar. "
                    f"Open positions: `{len(self.broker.positions)}`. "
                    f"Still watching stops, targets & the NSE options chain.")
            except Exception:
                pass
            return []
        # Trade the fresh candidates (equity + options) right now.
        placed_eq = self.execute_plan({"candidates": cands})
        placed_opt: List[Dict[str, Any]] = []
        try:
            opt_broker = getattr(self, "opt_broker", None)
            if opt_broker is not None:
                placed_opt = self.execute_option_plan(
                    {"candidates": cands}, opt_broker=opt_broker)
        except Exception as exc:
            logger.warning("Live rescan (%s) options leg failed: %s", slot, exc)
        try:
            from src.alerts import send_telegram
            lines = [f"🔍 *Live Market Scan ({slot} IST) — found "
                     f"{len(cands)} setup(s), traded now:*"]
            for c in cands[:5]:
                lines.append(
                    f"• {c.get('symbol')} {c.get('action')} "
                    f"@{c.get('entry_price')} conf {c.get('confidence')} "
                    f"rank {c.get('rank')}")
                opt = c.get("option_legs") or {}
                if opt.get("strategy") and opt.get("strategy") != "none":
                    legs = " + ".join(
                        f"{l.get('side')} {l.get('strike')}{l.get('kind')}"
                        for l in (opt.get("legs") or []))
                    lines.append(f"  + {opt.get('strategy')} {legs} "
                                 f"debit {opt.get('net_debit')}")
            lines.append(f"Equity orders: {len(placed_eq)} | "
                         f"Options spreads: {len(placed_opt)}")
            send_telegram("\n".join(lines))
        except Exception:
            pass
        return cands

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
        market_open_alerted = False
        live_scan_times = ["09:40", "11:00", "13:00"]

        while True:
            try:
                from datetime import datetime, timezone as _tz, timedelta as _td
                ist_now = datetime.now(_tz(_td(hours=5, minutes=30)))
                now_str = ist_now.strftime("%H:%M")

                if not plan_done and self._session_open():
                    self.execute_plan(plan)
                    plan_done = True
                    if not market_open_alerted:
                        try:
                            from src.alerts import send_telegram
                            send_telegram("🔔 *Market Open (09:15 IST)*\n\nNSE session has started! TRIO is now active, orders dispatched, and position tracking is live.")
                            market_open_alerted = True
                        except Exception as _mo_exc:
                            logger.warning("Market open alert failed: %s", _mo_exc)

                # Scheduled LIVE market re-scans (09:40 / 11:00 / 13:00 IST):
                # rebuild the plan from today's live bars (not yesterday's
                # file), trade anything new, and ALWAYS alert the outcome —
                # silence is never mistaken for a dead bot.
                for slot in list(live_scan_times):
                    if now_str >= slot and self._session_open():
                        live_scan_times.remove(slot)
                        new_cands = self._live_rescan(slot)
                        break

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
