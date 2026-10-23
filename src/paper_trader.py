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
import math
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.utils import (get_logger, load_config, utc_now, remote_paper_required,
                       local_option_shadow_allowed)
from src.options_intraday import (
    COMBINED,
    OPTIONS_INTRADAY,
    combined_mode_enabled,
    options_intraday_enabled,
    options_intraday_config,
    resolve_trading_mode,
    strict_options_enabled,
    validate_option_candidate,
    validate_options_mode_environment,
)
from src.data_fetcher import fetch_market_data
from src.indicators import compute_indicators
from src.sentiment import analyze_sentiment
from src.signal_engine import generate_signal, TradeSignal
from src.risk_manager import apply_risk_management, get_risk_state
from src.broker.paper import PaperBroker
from src.demand_supply import (
    candles_from_frame,
    confirm_demand_supply,
    demand_supply_config,
    demand_supply_enabled,
    filter_completed_frame,
)

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
        trading_mode: Optional[str] = None,
    ):
        cfg = load_config()

        self.symbols = symbols or cfg.get("symbols", [])
        self.timeframe = timeframe or cfg.get("timeframes", {}).get("default", "15m")
        self.test_mode = test_mode  # smoke/unit runs write a SEPARATE log file
        # Offline/unit runs must not inherit the repository's remote-paper
        # session selection from config.  Keep an explicitly requested mode
        # available for strict options-runtime tests, but default ordinary
        # PaperTrader fixtures to the local production path.
        test_isolation = bool(test_mode or os.environ.get("TRIO_TEST_MODE") == "1")
        explicit_mode = trading_mode or os.environ.get("TRIO_TRADING_MODE")
        mode_for_resolution = (
            trading_mode
            if trading_mode is not None
            else ("production" if test_isolation and not explicit_mode else None)
        )
        self.trading_mode = resolve_trading_mode(cfg, mode_for_resolution)
        self.options_only = self.trading_mode == OPTIONS_INTRADAY
        self.combined_mode = self.trading_mode == COMBINED
        self.strict_options_mode = self.options_only or self.combined_mode

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
        configured_provider = str(
            (cfg.get("broker", {}) or {}).get("provider", "paper")
        ).strip().lower()
        if (test_isolation and not explicit_mode
                and not os.environ.get("TRIO_BROKER_PROVIDER")
                and configured_provider in {"paper", "megabull"}):
            raw = provider = "paper"
        if raw not in allowed:
            raise ValueError(
                f"Unknown broker provider '{raw}' — allowed: {sorted(allowed)}. "
                f"Refusing to trade silently on a typoed simulator.")
        if remote_paper_required(cfg) and not test_isolation:
            from src.remote_paper import validate_remote_config
            validate_remote_config(cfg, provider=provider)
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
            mode = str((cfg.get("general", {}) or {}).get(
                "mode", cfg.get("mode", ""))).lower()
            if mode in ("live", "real") \
                    and not str(os.environ.get("TRIO_LIVE", "")).strip():
                if not bool((cfg.get("forward_test", {}) or {}).get("live_confirmed")):
                    raise RuntimeError(
                        "Config 'mode: live' requires TRIO_LIVE=1 or "
                        "forward_test.live_confirmed: true to proceed.")
            self.broker = MegaBullBroker(
                api_key=api_key,
                initial_capital=capital,
                state_path=(Path(cfg.get("general", {}).get("output_dir", "output"))
                            / "runtime" / "megabull.json")
                if remote_paper_required(cfg) else None,
            )
            logger.info("Broker provider: MegaBull (remote paper fills).")
        else:
            self.broker = PaperBroker(initial_capital=capital)
        self.broker_name = (
            getattr(self.broker, "provider_name", "paper"))
        self.signals_log: List[Dict[str, Any]] = []
        self._eod_squared = False  # one EOD square-off per session
        self._last_plan_order_ids: List[str] = []
        self._last_option_order_ids: List[str] = []
        self._options_risk_positions: Dict[str, Dict[str, Any]] = {}
        self._options_unresolved_exits: Dict[str, str] = {}
        self._options_runtime = None

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
            "PaperTrader initialized: %d symbols, timeframe=%s, capital=%.2f, mode=%s",
            len(self.symbols), self.timeframe, capital, self.trading_mode,
        )

    def _option_registry_path(self) -> Path:
        """Durable per-session option idempotency registry."""
        cfg = load_config()
        out = Path(cfg.get("general", {}).get("output_dir", "output"))
        return out / "runtime" / "options_registry.json"

    def _load_option_registry(self, session_date: str) -> set:
        if os.environ.get("TRIO_TEST_MODE") == "1":
            return set()
        try:
            path = self._option_registry_path()
            if not path.exists():
                return set()
            data = json.loads(path.read_text(encoding="utf-8"))
            return set(data.get(session_date, []) or [])
        except Exception as exc:
            logger.warning("Options registry unreadable: %s", exc)
            if remote_paper_required():
                raise RuntimeError("Cannot reconcile options registry") from exc
            return set()

    def _save_option_registry(self, session_date: str) -> None:
        if os.environ.get("TRIO_TEST_MODE") == "1":
            return
        try:
            path = self._option_registry_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
            data[session_date] = sorted(str(x) for x in
                                        getattr(self, "_opt_registry_keys", set()))
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:
            logger.warning("Options registry write failed: %s", exc)
            if remote_paper_required():
                raise RuntimeError("Cannot persist options intent") from exc

    @staticmethod
    def _option_registry_key(provider: str, sid_key: tuple) -> str:
        return "|".join([str(provider)] + [str(x) for x in sid_key[1:]])

    def ensure_option_broker(self, cfg: Optional[Dict[str, Any]] = None) -> Any:
        """Attach the configured options adapter exactly once.

        Strict options modes are deliberately remote-paper-only.  The legacy
        overlay may still construct a local options paper broker in ordinary
        production mode, but never in ``options_intraday`` or ``combined``.
        """
        from src.options_intraday import load_options_intraday_config
        cfg = load_options_intraday_config(cfg)
        existing = getattr(self, "opt_broker", None)
        if existing is not None:
            return existing
        if self.strict_options_mode:
            ok, reason = validate_options_mode_environment(cfg)
            if not ok:
                raise RuntimeError(reason)
            from src.remote_paper import make_options_broker
            self.opt_broker = make_options_broker(self, cfg)
            if getattr(self.opt_broker, "provider_name", "") != "megabull_options":
                raise RuntimeError("strict options mode requires MegaBullOptionsBroker")
            return self.opt_broker
        return None

    def _get_options_runtime(self, cfg: Optional[Dict[str, Any]] = None) -> Any:
        """Return the strict options-intraday lifecycle controller.

        The legacy options overlay remains available for the normal production
        mode.  The explicit options-intraday mode has a separate runtime so a
        local shadow, multi-leg spread, or stock/equity execution path cannot
        be reached accidentally.
        """
        if not getattr(self, "strict_options_mode",
                       getattr(self, "options_only", False)):
            raise RuntimeError("options-intraday runtime requested outside its mode")
        runtime = getattr(self, "_options_runtime", None)
        if runtime is None:
            from src.options_intraday_runtime import OptionsIntradayRuntime
            runtime = OptionsIntradayRuntime(
                self, cfg or load_config(),
                trading_mode=getattr(self, "trading_mode", "options_intraday"))
            self._options_runtime = runtime
        return runtime

    def _strict_options_risk_positions(self) -> Dict[str, Dict[str, Any]]:
        """Expose strict option risk to the shared portfolio reconciler."""
        runtime = getattr(self, "_options_runtime", None)
        if runtime is None or not self.strict_options_mode:
            return dict(getattr(self, "_options_risk_positions", {}) or {})
        exporter = getattr(runtime, "risk_positions", None)
        return dict(exporter() or {}) if callable(exporter) else {}

    def strict_options_risk_positions(self) -> Dict[str, Dict[str, Any]]:
        return self._strict_options_risk_positions()

    def _strict_options_closed_trades(self) -> List[Dict[str, Any]]:
        runtime = getattr(self, "_options_runtime", None)
        exporter = getattr(runtime, "strict_options_closed_trades", None)
        return list(exporter() or []) if callable(exporter) else []

    def _strict_options_blocked(self) -> bool:
        """Whether unresolved strict options state must block new entries.

        Existing equity positions may still be managed and flattened, but a
        combined session must not open another equity trade while the shared
        MegaBull account has an unresolved option entry/exit or an unknown
        remote option holding.
        """
        if not getattr(self, "strict_options_mode", False):
            return False
        runtime = getattr(self, "_options_runtime", None)
        if runtime is None:
            return False
        if bool(getattr(runtime, "_broker_blocked", False)):
            return True
        if bool(getattr(self, "_options_unresolved_exits", {})):
            return True
        state = getattr(runtime, "_state", {}) or {}
        for intent in (state.get("intents", {}) or {}).values():
            if isinstance(intent, dict) and intent.get("status") in {"pending", "uncertain"}:
                return True
        return False

    def _filter_equity_plan_by_demand_supply(
            self, plan: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
        """Apply an intraday demand/supply confirmation before equity entry.

        The combined premarket plan is intentionally still built from the
        existing daily equity screener.  When the feature is enabled, this
        boundary adds the missing live intraday confirmation immediately before
        submission, without changing the existing daily ranking or stop/target
        calculations.
        """
        settings = demand_supply_config(cfg, context="equity")
        timeframe = str(settings.get("equity_timeframe", "15m"))
        if not demand_supply_enabled(cfg, context="equity", timeframe=timeframe):
            return plan
        candidates = list(plan.get("candidates", []) or [])
        accepted = []
        for candidate in candidates:
            symbol = str(candidate.get("symbol", "")).strip()
            try:
                market_data = fetch_market_data(symbol, timeframe)
                frame, diagnostic = filter_completed_frame(
                    market_data.ohlcv, timeframe)
                if not diagnostic.get("valid"):
                    raise ValueError(
                        f"no completed {timeframe} candles")
                direction = ("bullish" if str(candidate.get("action", ""))
                             .upper() == "BUY" else "bearish")
                confirmation = confirm_demand_supply(
                    candles_from_frame(frame, symbol=symbol),
                    direction,
                    cfg=cfg,
                    context="equity",
                )
                candidate["demand_supply"] = confirmation.to_dict()
                if confirmation.confirmed:
                    accepted.append(candidate)
                else:
                    self._log_event("skip", {
                        "symbol": symbol,
                        "asset": "equity",
                        "reason": "demand_supply_confirmation_failed",
                        "details": confirmation.to_dict(),
                    })
                    logger.info("Demand/supply blocked equity %s: %s",
                                symbol, confirmation.reason)
            except Exception as exc:
                candidate["demand_supply"] = {
                    "confirmed": False,
                    "reason": f"demand/supply data unavailable: {exc}",
                }
                self._log_event("skip", {
                    "symbol": symbol,
                    "asset": "equity",
                    "reason": "demand_supply_data_unavailable",
                })
                logger.warning("Demand/supply data blocked equity %s: %s",
                               symbol, exc)
        plan["candidates"] = accepted
        plan["demand_supply"] = {
            "enabled": True,
            "timeframe": timeframe,
            "accepted": len(accepted),
            "rejected": len(candidates) - len(accepted),
        }
        return plan

    def _options_risk_preflight(self, opt_broker: Any,
                                candidate: Dict[str, Any],
                                pick: Any, lots: int,
                                cfg: Dict[str, Any]) -> Tuple[bool, str]:
        """Apply production risk state before a remote options submission."""
        from src.risk_manager import get_risk_state
        state = get_risk_state()
        if state.halt_active:
            return False, "risk-halt"
        rm_cfg = cfg.get("risk_management", {}) or {}
        max_open = int(rm_cfg.get("max_open_positions", 5) or 5)
        if len(getattr(opt_broker, "positions", {}) or {}) >= max_open:
            return False, "max-open-options"
        max_consecutive = int(rm_cfg.get("max_consecutive_losses", 0) or 0)
        if max_consecutive > 0 and state.consecutive_losses >= max_consecutive:
            return False, "consecutive-loss-limit"
        capital = float(rm_cfg.get("capital", 0) or 0)
        max_daily_pct = float(rm_cfg.get("max_daily_loss_pct", 0) or 0)
        if capital > 0 and max_daily_pct > 0 and state.daily_pnl <= -capital * max_daily_pct / 100:
            return False, "daily-loss-limit"
        sid = getattr(pick, "expiry", "") + ":" + str(
            getattr(pick, "legs", [{}])[0].strike if getattr(pick, "legs", None) else "")
        if any(key.endswith(sid) for key in self._options_risk_positions):
            return False, "duplicate-option-entry"
        if getattr(pick, "maxLoss", 0.0) <= 0:
            return False, "invalid-option-risk"
        if lots <= 0:
            return False, "invalid-lot-size"
        return True, ""

    def _option_brokers(self) -> List[Any]:
        """Return the primary options book and any explicitly allowed shadows."""
        brokers = []
        primary = getattr(self, "opt_broker", None)
        if primary is not None:
            brokers.append(primary)
        if not self.options_only:
            brokers.extend(item[0] if isinstance(item, tuple) else item
                           for item in (getattr(self, "_opt_shadows", []) or []))
        return [broker for broker in brokers if broker is not None]

    def options_have_positions(self) -> bool:
        return any(bool(getattr(broker, "positions", {}))
                   for broker in self._option_brokers())

    def _register_option_fill(self, order: Any, pick: Any, lots: int) -> None:
        """Register a confirmed option position in the production risk state."""
        sid = str(getattr(order, "symbol", "") or "")
        if not sid:
            return
        risk_amount = float(getattr(pick, "maxLoss", 0.0) or 0.0) * lots
        info = {"size": lots, "entry": float(getattr(pick, "net_debit", 0.0) or 0.0),
                "risk_amount": risk_amount, "asset": "options"}
        self._options_risk_positions[sid] = info
        try:
            from src.risk_manager import add_position
            add_position(sid, lots, info["entry"], risk_amount)
        except Exception as exc:
            logger.warning("Options risk registration failed for %s: %s", sid, exc)

    def _forget_option_risk(self, symbol: str) -> None:
        self._options_risk_positions.pop(str(symbol), None)

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
        try:
            from src.execution_lifecycle import reconcile_broker, submit_candidate
            recon = reconcile_broker(self)
            if recon.get("uncertain"):
                logger.warning("scan_once: broker uncertain — entries blocked, "
                               "managing existing positions only.")
                self._guard_open_positions()
                self._manage_open_positions()
                return []
            proposed: Dict[str, Any] = dict(getattr(self, "_proposed", {}) or {})
        except Exception:
            from src.execution_lifecycle import submit_candidate  # type: ignore
            proposed = {}
        for cand in candidates:
            signal = cand.signal
            signals.append(signal)
            self.signals_log.append(cand.to_dict())
            payload = {
                "symbol": signal.symbol, "action": signal.action,
                "entry_price": signal.entry_price, "stop_loss": signal.stop_loss,
                "target": signal.target, "position_size": signal.position_size,
                "confidence": signal.confidence, "rank": cand.rank,
                "setup_name": cand.setup_name,
                "reasoning": list(signal.reasoning or []),
                "risk_amount": getattr(signal, "risk_amount", 0.0),
            }
            try:
                res = submit_candidate(self, payload, reason="scan",
                                       proposed=proposed, cfg=cfg)
            except Exception as exc:
                logger.error("Error placing order for %s: %s",
                             signal.symbol, exc)
                continue
            if res.get("status") != "FILLED":
                self.signals_log[-1]["skipped"] = res.get("reason", "skip")

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
        self._eod_squared = not bool(self.broker.positions)
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

    def _market_still_running(self, now_ist=None) -> bool:
        """True while the session loop must keep ticking.

        Deliberately NOT ``_session_open()`` (can_enter). Entries stop at
        ``entry_cutoff`` (15:00) but the hard-flat square-off is at 15:10 and
        the market closes at 15:30. A loop that exits when can_enter goes
        False therefore stopped managing ten minutes BEFORE the forced exit,
        leaving positions open and un-squared for the tail of the session.

        Keep ticking until the market is actually closed — and never exit
        while still holding stock.
        """
        try:
            from src.market_clock import market_clock_from_config
            clock = market_clock_from_config(load_config())
            if not clock.is_closed(now_ist):
                return True
            # Market closed: only stop once the book is flat.
            return bool(self.broker.positions) or self.options_have_positions() \
                or bool(getattr(self, "_options_unresolved_exits", {}))
        except Exception:
            # Fail-safe: an unreadable clock must not strand live positions.
            return True

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
                        from src.execution_lifecycle import record_confirmed_close as _rcc
                        try:
                            _rcc(self, sym, rec)
                        except Exception:
                            self._log_event("close", rec)
                elif sl and price >= sl and pos.side == "SHORT":
                    rec = self.broker.close_position(sym, price, "stop-hit")
                    if rec:
                        from src.execution_lifecycle import record_confirmed_close as _rcc
                        try:
                            _rcc(self, sym, rec)
                        except Exception:
                            self._log_event("close", rec)
                elif tgt and price >= tgt and pos.side == "LONG":
                    rec = self.broker.close_position(sym, price, "target-hit")
                    if rec:
                        from src.execution_lifecycle import record_confirmed_close as _rcc
                        try:
                            _rcc(self, sym, rec)
                        except Exception:
                            self._log_event("close", rec)
                elif tgt and price <= tgt and pos.side == "SHORT":
                    rec = self.broker.close_position(sym, price, "target-hit")
                    if rec:
                        from src.execution_lifecycle import record_confirmed_close as _rcc
                        try:
                            _rcc(self, sym, rec)
                        except Exception:
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
        # Broker-agnostic: the manager DECIDES for every provider; each
        # broker only EXECUTES close_position(). Remote (MegaBull) positions
        # are managed on their mirrored stops/targets — no silent strategy
        # fork (see execution_lifecycle.reconcile_broker first).
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
                tgt = float(order.target or 0.0)

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
                # Preserve the original entry quantity across partial exits.
                # Rebuilding this from pos.quantity would make T2 use 30% of
                # the remainder instead of 30% of the original position.
                original_qty = getattr(pos, "original_quantity", None)
                if not original_qty:
                    original_qty = int(pos.quantity)
                    try:
                        setattr(pos, "original_quantity", original_qty)
                    except Exception:
                        pass

                # Shared policy integration: T1 decision via exit_policy module.
                from src.exit_policy import LadderState, decide_exit
                state = LadderState(
                    side=pos.side,
                    entry=pos.avg_price,
                    initial_stop=float(getattr(pos, "initial_stop", sl)),
                    stop=sl,
                    target=tgt,
                    quantity=pos.quantity,
                    original_quantity=original_qty,
                    t1_hit=getattr(pos, "ladder_t1_hit", False),
                    t2_hit=getattr(pos, "ladder_t2_hit", False),
                )
                atr = _atr()
                actions = decide_exit(state, price=price, atr=atr)
                
                # Filter for T1 partials from shared policy
                t1_actions = [a for a in actions if a.kind == "partial" and "T1" in a.reason]
                
                if rr >= t1_at_r and not pos.halved and t1_actions:
                    # Execute T1 partial from shared policy
                    action = t1_actions[0]
                    rec = self.broker.close_position(
                        sym, price, action.reason,
                        # Brokers accept a fraction of the CURRENT position;
                        # the policy quantity is exact and is based on the
                        # original entry size.
                        fraction=(action.quantity / pos.quantity
                                  if pos.quantity else 0.0))
                    if rec:
                        from src.execution_lifecycle import record_confirmed_close
                        record_confirmed_close(self, sym, rec)
                        exited.append(rec)
                        # Persist T1 success to position
                        setattr(pos, "ladder_t1_hit", True)
                        pos.halved = True
                        order.stop_loss = pos.avg_price
                        # Partial: realized pnl only; position stays open.
                        pos.booked_pnl = float(getattr(pos, "booked_pnl", 0.0) or 0.0) + float(rec.get("pnl") or 0.0)
                elif rr >= t1_at_r and not pos.halved:
                    # Legacy T1 path (fallback if decide_exit didn't fire,
                    # e.g. phase2 config differences)
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
                        # Partial: realized P&L is already recorded by the
                        # shared close lifecycle; do not count it twice.
                        pos.booked_pnl = float(getattr(pos, "booked_pnl", 0.0) or 0.0) + float(rec.get("pnl") or 0.0)

                # 4. Execute T1 partials from shared policy (already done above)
                # -----------------------------------------------------------------
                # 5. Ladder Phase 2 via shared exit policy
                # -----------------------------------------------------------------
                if getattr(pos, "ladder_t1_hit", False):
                    # Build LadderState for shared policy
                    state = LadderState(
                        side=pos.side,
                        entry=pos.avg_price,
                        initial_stop=float(getattr(pos, "initial_stop", sl)),
                        stop=sl,
                        target=tgt,
                        quantity=pos.quantity,
                        original_quantity=original_qty,
                        t1_hit=True,
                        t2_hit=getattr(pos, "ladder_t2_hit", False),
                    )
                    atr = _atr()
                    phase2 = bool(ladder_cfg.get("enabled_phase2", False))
                    actions = decide_exit(state, price=price, atr=atr,
                                          hard_flat=self._eod_squared, cfg=cfg)
                    
                    # Filter for T2 partials
                    t2_actions = [a for a in actions if a.kind == "partial" and "T2" in a.reason]
                    
                    if t2_actions and rr >= t2_at_r:
                        # Execute T2 partial from shared policy
                        action = t2_actions[0]
                        rec = self.broker.close_position(
                            sym, price, action.reason,
                            fraction=(action.quantity / pos.quantity
                                      if pos.quantity else 0.0))
                        if rec:
                            from src.execution_lifecycle import record_confirmed_close
                            record_confirmed_close(self, sym, rec)
                            exited.append(rec)
                            # Persist T2 success to position
                            setattr(pos, "ladder_t2_hit", True)
                            # Arm trailing stop if ATR available
                            if atr and action.new_stop:
                                if pos.side == "LONG":
                                    order.stop_loss = max(order.stop_loss or 0, action.new_stop)
                                else:
                                    order.stop_loss = min(order.stop_loss or 0, action.new_stop)
                            # Partial: realized P&L is already recorded by
                            # the shared close lifecycle; do not count twice.
                            pos.booked_pnl = float(getattr(pos, "booked_pnl", 0.0) or 0.0) + float(rec.get("pnl") or 0.0)
                    elif not t2_actions and rr >= t2_at_r and phase2 is False:
                        # phase2 off -> T2 becomes shadow log
                        self._log_event("shadow-exit", {
                            "symbol": sym, "side": pos.side,
                            "entry": pos.avg_price, "price": price,
                            "rr": round(rr, 2),
                            "reason": "ladder-T2", "would_be": "partial@T2-1.5R",
                        })
                    # If phase2 is True but no t2_actions (e.g. quantity too small),
                    # the legacy logic below handles it as a fallback
                # _legacy_t2_management placeholder has been removed; logic inline above
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
                        try:
                            from src.execution_lifecycle import record_confirmed_close
                            record_confirmed_close(self, sym, rec)
                            exited.append(rec)
                        except Exception:
                            self._log_event("close", rec)
                elif tgt_hit:
                    rec = self.broker.close_position(sym, price, "target-hit")
                    if rec:
                        try:
                            from src.execution_lifecycle import record_confirmed_close
                            record_confirmed_close(self, sym, rec)
                            exited.append(rec)
                        except Exception:
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
                    try:
                        from src.execution_lifecycle import record_confirmed_close as _rcc2
                        _rcc2(self, sym, rec)
                    except Exception:
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
        strict_mode = getattr(self, "strict_options_mode",
                              getattr(self, "options_only", False))
        combined_mode = getattr(self, "combined_mode", False)
        if strict_mode:
            # Strict options modes are owned by the dedicated lifecycle
            # lifecycle runtime.  Do not convert its one-leg candidates into
            # the legacy OptionTradeCandidate overlay: that path permits
            # spreads and has a local-shadow fallback in normal production.
            if plan.get("executed"):
                logger.info("execute_option_plan: options plan already marked executed; skipping")
                return []
            cfg = load_config()
            broker = opt_broker or self.ensure_option_broker(cfg)
            runtime = self._get_options_runtime(cfg)
            strict_plan = (plan if str(plan.get("trading_mode", "")).lower()
                           == "options_intraday" else
                           (plan.get("options_intraday_plan")
                            if combined_mode else plan))
            if not isinstance(strict_plan, dict):
                raise RuntimeError("combined plan has no strict options sub-plan")
            placed = runtime.execute_plan(strict_plan, opt_broker=broker)
            self.opt_broker = broker
            self._last_option_order_ids = [
                str(item.get("order_id")) for item in placed
                if isinstance(item, dict) and item.get("order_id")
            ]
            return placed
        if plan.get("executed"):
            logger.info("execute_option_plan: plan already marked executed; skipping")
            return []
        cfg = load_config()
        options_mode = bool(self.options_only)
        if options_mode:
            ok, reason = validate_options_mode_environment(cfg)
            if not ok:
                logger.error("execute_option_plan blocked: %s", reason)
                return []
        required = remote_paper_required(cfg) and not (
            os.environ.get("TRIO_TEST_MODE") == "1"
        )
        if (required or options_mode) and not self._session_open():
            return []
        if opt_broker is None:
            if options_mode:
                opt_broker = self.ensure_option_broker(cfg)
            elif required or getattr(self.broker, "provider_name", "") == "megabull":
                from src.remote_paper import make_options_broker
                opt_broker = make_options_broker(self, cfg)
                self.opt_broker = opt_broker
        if required or options_mode:
            if getattr(opt_broker, "provider_name", "") != "megabull_options":
                raise RuntimeError("Remote-only options require MegaBullOptionsBroker")
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
        from src.plan import today_ist_str
        session_date = str(plan.get("date") or today_ist_str())
        if not hasattr(self, "_opt_spreads_placed"):
            self._opt_spreads_placed = set()
        if not hasattr(self, "_opt_registry_keys"):
            self._opt_registry_keys = self._load_option_registry(session_date)
        self._option_registry_session_date = session_date
        self._last_option_order_ids = []
        max_spreads = int((load_config().get("options", {}) or {}).get(
            "max_spreads_per_session", 4))
        placed: List[Dict[str, Any]] = []
        option_candidates = plan.get("option_candidates")
        if option_candidates is None:
            option_candidates = plan.get("candidates", []) or []
        for cand in option_candidates or []:
            opt = cand.get("option_legs") or {}
            if not opt or opt.get("strategy") in (None, "", "none"):
                continue
            try:
                if options_mode:
                    valid, reason = validate_option_candidate(
                        cand, plan=plan, cfg=cfg)
                    if not valid:
                        self._log_event("skip", {
                            "symbol": cand.get("symbol"),
                            "asset": "options",
                            "reason": reason,
                        })
                        logger.warning("Options-only candidate rejected: %s", reason)
                        continue
                from src.options_selector import OptionTradeCandidate, OptionLeg
                legs = [OptionLeg(**{k: l.get(k, v) for k, v in
                                      OptionLeg().__dict__.items()})
                        for l in (opt.get("legs") or [])]
                pick = OptionTradeCandidate(
                    strategy=opt.get("strategy", ""),
                    underlying=opt.get("underlying", "NIFTY"),
                    exchange=opt.get("exchange", "NSE"),
                    lot_size=int(opt.get("lot_size") or 0),
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
                registry_key = self._option_registry_key(provider, sid_key)
                shadow_sid_key = ("options_paper",) + sid_key[1:]
                shadow_registry_key = self._option_registry_key(
                    "options_paper", sid_key)
                if sid_key in self._opt_spreads_placed:
                    logger.info(
                        "execute_option_plan: SKIP %s %s (spread already "
                        "held this session).", cand.get("symbol"),
                        pick.strategy)
                    continue
                if registry_key in self._opt_registry_keys:
                    logger.info("execute_option_plan: SKIP %s %s (durable "
                                "registry already contains spread).",
                                cand.get("symbol"), pick.strategy)
                    continue
                if ("megabull" in provider.lower() and
                        (shadow_sid_key in self._opt_spreads_placed or
                         shadow_registry_key in self._opt_registry_keys)):
                    logger.info("execute_option_plan: SKIP %s %s (local "
                                "shadow already held).", cand.get("symbol"),
                                pick.strategy)
                    continue
                if max(len(self._opt_spreads_placed), len(self._opt_registry_keys)) >= max_spreads:
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
                risk_unit = float(pick.maxLoss or pick.net_debit * lot_sz or 0.0)
                if risk_amt > 0 and risk_unit > 0:
                    lots = int(risk_amt // risk_unit)
                if options_mode and lots < 1:
                    self._log_event("skip", {
                        "symbol": cand.get("symbol"),
                        "asset": "options",
                        "reason": "risk-budget-below-one-lot",
                    })
                    continue
                if required or options_mode:
                    from src.execution_lifecycle import (
                        reconcile_broker, validate_available_balance)
                    if reconcile_broker(self).get("uncertain"):
                        break
                    risk_ok, risk_reason = self._options_risk_preflight(
                        opt_broker, cand, pick, lots, cfg)
                    if not risk_ok:
                        self._log_event("skip", {
                            "symbol": cand.get("symbol"),
                            "asset": "options",
                            "reason": risk_reason,
                        })
                        break
                    # All broker adapters implement the BaseBroker
                    # get_balance() signature without a ``strict`` keyword.
                    try:
                        if bool(getattr(self.broker, "_auth_failed", False)):
                            raise RuntimeError("broker balance authentication failed")
                        balance = self.broker.get_balance()
                        if getattr(self.broker, "_balance_valid", True) is False:
                            raise RuntimeError("broker balance response not authoritative")
                        available, margin_reason = validate_available_balance(
                            balance)
                    except Exception:
                        # An unavailable or malformed remote balance is not
                        # permission to trade. Do not log the response body.
                        self._log_event("skip", {
                            "asset": "options",
                            "reason": "margin-check-failed"})
                        continue
                    if margin_reason:
                        self._log_event("skip", {
                            "asset": "options", "reason": margin_reason})
                        continue
                    if available < pick.net_debit * lots * lot_sz:
                        self._log_event("skip", {
                            "asset": "options",
                            "reason": "margin-exhausted"})
                        continue
                    # Persist the intent BEFORE sending legs. An interrupted
                    # submit must never be automatically retried as a new spread.
                    self._opt_registry_keys.add(registry_key)
                    self._save_option_registry(session_date)
                order = opt_broker.open_spread(pick, lots=lots,
                                               reason="options-plan")
                if order.status == "FILLED":
                    if options_mode:
                        self._register_option_fill(order, pick, lots)
                    self._opt_spreads_placed.add(sid_key)
                    self._opt_registry_keys.add(registry_key)
                    self._last_option_order_ids.append(str(order.order_id))
                    self._save_option_registry(session_date)
                if order.status != "FILLED":
                    self._log_event("skip", {
                        "symbol": cand.get("symbol"), "asset": "options",
                        "strategy": pick.strategy,
                        "order_id": order.order_id, "status": order.status,
                        "reason": f"{order.status}: {order.error}"})
                    try:
                        from src.alerts import send_options_telegram
                        send_options_telegram(
                            f"MegaBull paper option {order.status}: {cand.get('symbol')} "
                            f"{pick.strategy}\n{order.error}\nNo local shadow trade opened.")
                    except Exception as exc:
                        logger.warning("Options rejection alert failed: %s", exc)
                    # MegaBull silently rejects option legs (endpoint
                    # refused lots=1/units=50/75 on 2026-10-08). When the
                    # remote broker is selected but a leg can't fill, keep
                    # the LOCAL paper sim as the shadow book so the
                    # overlay's P&L/marking stays testable and the equity
                    # session is never blocked.
                    if (not options_mode and local_option_shadow_allowed(cfg)
                            and "megabull" in str(
                            getattr(opt_broker, "provider_name", "")).lower()):
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
                                self._opt_registry_keys.add(shadow_registry_key)
                                self._last_option_order_ids.append(
                                    str(s_order.order_id))
                                self._save_option_registry(session_date)
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
        if self.options_only:
            # Options-only mode may manage an existing equity book, but it
            # must never submit a cash-market order from a stock signal.
            for cand in plan.get("candidates", []) or []:
                self._log_event("skip", {
                    "symbol": cand.get("symbol"),
                    "asset": "equity",
                    "reason": "options-only-mode",
                })
            logger.info("execute_plan: options-only mode — equity entries blocked.")
            return []
        if self._strict_options_blocked():
            logger.critical("execute_plan: strict options state unresolved — "
                            "blocking new equity entries while managing exits.")
            self._log_event("skip", {
                "asset": "equity",
                "reason": "strict-options-state-unresolved",
            })
            return []
        if plan.get("executed"):
            logger.info("execute_plan: plan already marked executed; skipping")
            return []
        self._last_plan_order_ids = []
        cfg = load_config()
        plan = self._filter_equity_plan_by_demand_supply(plan, cfg)
        if not plan.get("candidates"):
            logger.info("execute_plan: no equity candidates after demand/supply filter")
            return []
        from src.execution_lifecycle import reconcile_broker, submit_candidate
        recon = reconcile_broker(self)
        if recon.get("uncertain"):
            logger.warning("execute_plan: broker uncertain — blocking new "
                           "entries until refresh confirms.")
            return []
        try:
            from src.risk_manager import get_risk_state
            if get_risk_state().halt_active:
                logger.critical("execute_plan: trading halted — %s.",
                                get_risk_state().halt_reason)
                return []
        except Exception:
            pass

        # Transaction-like proposed portfolio: every confirmed fill in this
        # pass updates it, so the NEXT candidate sees the new exposure.
        proposed: Dict[str, Any] = dict(getattr(self, "_proposed", {}) or {})
        placed: List[Dict[str, Any]] = []
        for cand in plan.get("candidates", []) or []:
            payload = {
                "symbol": cand.get("symbol", ""), "action": cand.get("action", "HOLD"),
                "entry_price": cand.get("entry_price"),
                "stop_loss": cand.get("stop_loss"), "target": cand.get("target"),
                "position_size": int(cand.get("position_size") or 0),
                "confidence": int(cand.get("confidence") or 0),
                "reasoning": list(cand.get("reasoning") or []),
                "rank": cand.get("rank"), "setup_name": cand.get("setup_name"),
                "risk_amount": cand.get("risk_amount", 0.0) or 0.0,
            }
            try:
                res = submit_candidate(self, payload, reason="premarket-plan",
                                       proposed=proposed, cfg=cfg)
            except Exception as exc:
                logger.error("execute_plan: submit error for %s: %s",
                             cand.get("symbol"), exc)
                continue
            if res.get("status") != "FILLED":
                continue
            placed.append(res["record"])
            if res["record"].get("order_id"):
                self._last_plan_order_ids.append(str(
                    res["record"]["order_id"]))

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
            fresh = build_plan(universe, "1d", top_n,
                               trading_mode=self.trading_mode)
            cands = fresh.get("candidates", []) or []
            strict_plan = (fresh.get("options_intraday_plan")
                           if self.combined_mode else
                           (fresh if self.options_only else None))
            option_source = strict_plan if isinstance(strict_plan, dict) else fresh
            option_cands = option_source.get("option_candidates", []) or []
            try:
                from src.plan import save_plan as _save_plan
                from src.plan import today_ist_str
                _save_plan(dict(fresh, date=today_ist_str()))
            except Exception:
                pass
        except Exception as exc:
            logger.warning("Live rescan (%s) failed: %s", slot, exc)
            for _send in (
                lambda m: __import__("src.alerts", fromlist=["send_telegram"]).send_telegram(m),
                lambda m: __import__("src.alerts", fromlist=["send_options_telegram"]).send_options_telegram(m),
            ):
                try:
                    _send(f"🔍 *Live Market Scan ({slot} IST)*\n\n"
                          f"Scan failed ({exc}). Still managing open positions.")
                except Exception:
                    pass
            return []
        if not cands and not option_cands:
            logger.info("Live rescan (%s): scanned, nothing met the bar.", slot)
            for _kind, _sender in (
                ("equity", lambda m: __import__("src.alerts", fromlist=["send_telegram"]).send_telegram(m)),
                ("options", lambda m: __import__("src.alerts", fromlist=["send_options_telegram"]).send_options_telegram(m)),
            ):
                try:
                    if _kind == "equity":
                        _sender(
                            f"🔍 *Live Market Scan — Equity ({slot} IST)*\n\n"
                            f"No new setup met the bar. "
                            f"Open equity positions: `{len(self.broker.positions)}`.")
                    else:
                        _sender(
                            f"🔍 *Live Market Scan — Options (8877508167, {slot} IST)*\n\n"
                            f"No new NIFTY spread met the bar. "
                            f"Still watching the NSE options chain.")
                except Exception:
                    pass
            return []
        # Trade the fresh candidates through their asset-specific lifecycle.
        placed_eq = [] if self.options_only else self.execute_plan(
            {"candidates": cands})
        placed_opt: List[Dict[str, Any]] = []
        try:
            opt_broker = getattr(self, "opt_broker", None)
            if self.strict_options_mode and opt_broker is None:
                opt_broker = self.ensure_option_broker(cfg)
            if self.strict_options_mode and opt_broker is not None:
                options_plan = strict_plan
                placed_opt = self.execute_option_plan(
                    options_plan, opt_broker=opt_broker)
            elif opt_broker is not None:
                placed_opt = self.execute_option_plan(
                    {"candidates": cands,
                     "option_candidates": option_cands},
                    opt_broker=opt_broker)
        except Exception as exc:
            logger.warning("Live rescan (%s) options leg failed: %s", slot, exc)
        # Split deliver: equity verdict -> old bot, options verdict -> new bot (8877508167)
        try:
            from src.alerts import send_telegram as _send_eq
            from src.alerts import send_options_telegram as _send_opt
            # Equity verdict
            eq_lines = [f"🔍 *Live Market Scan — Equity ({slot} IST) — "
                        f"{'blocked by options-only mode' if self.options_only else f'found {len(cands)} setup(s), traded now'}:*"]
            for c in cands[:5]:
                eq_lines.append(
                    f"• {c.get('symbol')} {c.get('action')} "
                    f"@{c.get('entry_price')} conf {c.get('confidence')} "
                    f"rank {c.get('rank')}")
            eq_lines.append(f"Equity orders: {len(placed_eq)}")
            _send_eq("\n".join(eq_lines))
            # Options verdict (separate channel)
            has_opt = bool(option_cands)
            if has_opt:
                opt_lines = [f"🔍 *Live Market Scan — Options (8877508167, {slot} IST) — "
                             f"found {len(option_cands)} opportunity(s), traded now:*"]
                for c in option_cands[:5]:
                    opt = c.get("option_legs") or {}
                    if not opt and c.get("option_legs") is None:
                        # Strict planner candidates already contain the
                        # one-leg structure under option_legs.
                        opt = {}
                    if opt.get("strategy") and opt.get("strategy") != "none":
                        legs = " + ".join(
                            f"{l.get('side')} {l.get('strike')}{l.get('kind')}"
                            for l in (opt.get("legs") or []))
                        opt_lines.append(
                            f"• {c.get('symbol')} {opt.get('strategy')} {legs} "
                            f"debit {opt.get('net_debit')}")
                opt_lines.append(f"Options spreads: {len(placed_opt)}")
                _send_opt("\n".join(opt_lines))
            else:
                _send_opt(
                    f"🔍 *Live Market Scan — Options (8877508167, {slot} IST)*\n\n"
                    f"No new NIFTY spread met the bar — still watching the chain.")
        except Exception:
            pass
        return cands

    def _run_options_only_session(self, interval_seconds: int,
                                  max_seconds: Optional[int],
                                  management_only: bool) -> None:
        """Serialize fresh entry scans independently of the management cadence.

        There is deliberately no plan input or fixed-slot catchup schedule.
        Each scan finishes before the next can start, and failed attempts wait
        the same interval before retrying.  The strict runtime owns all entry,
        risk, exit and finalization decisions.
        """
        from datetime import datetime, timedelta, timezone

        runtime = None
        try:
            cfg = load_config()
            runtime = self._get_options_runtime(cfg)
            runtime.management_interval_seconds = max(5, interval_seconds)
            self.ensure_option_broker(cfg)
            mode_cfg = options_intraday_config(cfg)
            try:
                scan_interval = float(mode_cfg.get("scan_interval_seconds", 300))
                if not math.isfinite(scan_interval) or scan_interval <= 0:
                    raise ValueError("scan interval must be positive and finite")
            except (TypeError, ValueError, OverflowError):
                logger.warning("Invalid options scan interval; using 300 seconds.")
                scan_interval = 300.0

            started = time.monotonic()
            next_scan_at = None
            while True:
                tick_started = time.monotonic()
                ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
                try:
                    runtime.tick(now=ist_now)
                except Exception as exc:
                    logger.critical("options_intraday tick unresolved: %s", exc)

                # Management comes first; recheck the clock before attempting
                # an entry scan in case exit/reconciliation work took time.
                ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
                can_enter = self._session_open(now_ist=ist_now)
                if (not management_only and can_enter and
                        (next_scan_at is None or time.monotonic() >= next_scan_at)):
                    scan_started = time.monotonic()
                    try:
                        runtime.scan_and_execute(now=ist_now)
                    except Exception as exc:
                        logger.error("Options live scan failed; still managing exits: %s",
                                     exc)
                    finally:
                        next_scan_at = scan_started + scan_interval
                        if next_scan_at <= time.monotonic():
                            # A scan that overran its entire interval must not
                            # trigger a burst of overdue catchup scans.
                            next_scan_at = time.monotonic() + scan_interval

                ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
                try:
                    self._log_event("scan", {
                        "session_open": can_enter,
                        "positions": len(runtime._state.get("positions", {})),
                    })
                    from src.alerts import handle_all_joins
                    handle_all_joins()
                except Exception as exc:
                    logger.warning("Options session tick reporting failed: %s", exc)

                if not self._market_still_running(now_ist=ist_now):
                    logger.info("Market closed — final options square-off.")
                    break
                if max_seconds and time.monotonic() - started >= max_seconds:
                    logger.info("run_session: max_seconds reached.")
                    break
                time.sleep(max(0, max(5, interval_seconds) -
                               (time.monotonic() - tick_started)))
        finally:
            if runtime is not None:
                try:
                    runtime.finalize()
                except Exception as exc:
                    logger.critical("options_intraday finalization unresolved: %s", exc)
                    self._options_unresolved_exits = {"strict_options": str(exc)}
            self._write_trade_log_csv()

    def run_session(self, plan: Optional[Dict[str, Any]] = None,
                    interval_seconds: int = 60,
                    max_seconds: Optional[int] = None,
                    tick_hook: Any = None,
                    management_only: bool = False) -> None:
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

        Explicit options-only mode ignores ``plan`` and scans fresh live data
        on its own interval. ``management_only`` disables those entry scans
        while retaining every runtime management tick and finalization.
        """
        if self.options_only:
            self._run_options_only_session(
                interval_seconds, max_seconds, management_only)
            return

        options_runtime = None
        equity_enabled = not self.options_only
        if self.strict_options_mode:
            try:
                options_runtime = self._get_options_runtime(load_config())
                self.ensure_option_broker(load_config())
            except Exception as exc:
                logger.critical("options_intraday runtime setup failed: %s", exc)
                return
        if self.combined_mode and isinstance(plan, dict):
            nested = plan.get("options_intraday_plan")
            from datetime import datetime, timedelta, timezone
            today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date().isoformat()
            if (isinstance(nested, dict) and
                    (nested.get("execution_ready") is False or
                     str(nested.get("date", "")) != today)):
                try:
                    from src.options_intraday_plan import build_intraday_plan
                    refreshed = build_intraday_plan(load_config())
                    refreshed["date"] = today
                    plan["options_intraday_plan"] = refreshed
                except Exception as exc:
                    logger.critical("combined options plan refresh failed: %s", exc)
        if equity_enabled and not self._session_open():
            logger.info("Session is CLOSED — plan execution delayed "
                        "until the next open (managing only).")
            # Still rebuild stop/target for any positions held, so the guard
            # can manage exits on the next direct tick.
            self.rehydrate_orders(plan or {})

        start = time.time()
        equity_plan_done = plan is None or not equity_enabled
        options_plan_done = plan is None or not self.strict_options_mode
        market_open_alerted = False
        live_scan_times = ["09:40", "10:15", "11:00", "12:00", "13:00", "14:00"]

        while True:
            try:
                from datetime import datetime, timezone as _tz, timedelta as _td
                ist_now = datetime.now(_tz(_td(hours=5, minutes=30)))
                now_str = ist_now.strftime("%H:%M")

                if equity_enabled:
                    from src.execution_lifecycle import reconcile_broker
                    ready = (not reconcile_broker(self).get("uncertain") and
                             not self._strict_options_blocked())
                else:
                    ready = True
                if (not equity_plan_done and equity_enabled and
                        self._session_open() and ready):
                    self.execute_plan(plan)
                    equity_plan_done = True
                if (not options_plan_done and self.strict_options_mode and
                        self._session_open()):
                    try:
                        self.execute_option_plan(
                            plan, opt_broker=getattr(self, "opt_broker", None))
                    except Exception as option_exc:
                        logger.critical(
                            "strict options plan execution unresolved: %s",
                            option_exc)
                    # The runtime persists intents before remote submission;
                    # never resubmit the same opening plan on every tick.
                    options_plan_done = True
                if ((equity_plan_done or not equity_enabled) and
                        (options_plan_done or not self.strict_options_mode) and
                        not market_open_alerted):
                    if not market_open_alerted:
                        _open_msg = "🔔 *Market Open (09:15 IST)*\n\nNSE session has started! TRIO is now active, orders dispatched, and position tracking is live."
                        for _sender in (
                            lambda m: __import__("src.alerts", fromlist=["send_telegram"]).send_telegram(m),
                            lambda m: __import__("src.alerts", fromlist=["send_options_telegram"]).send_options_telegram(m),
                        ):
                            try:
                                _sender(_open_msg)
                            except Exception as _mo_exc:
                                logger.warning("Market open alert failed: %s", _mo_exc)
                        market_open_alerted = True

                # Scheduled LIVE market re-scans (09:40 / 11:00 / 13:00 IST):
                # rebuild the plan from today's live bars (not yesterday's
                # file), trade anything new, and ALWAYS alert the outcome —
                # silence is never mistaken for a dead bot.
                for slot in list(live_scan_times):
                    if now_str >= slot and self._session_open():
                        live_scan_times.remove(slot)
                        new_cands = self._live_rescan(slot)
                        break

                # Manage existing positions (stops/targets/partials).  The
                # options tick hook runs before hard-flat so a remote paper
                # exit is attempted at the authoritative 15:10 boundary.
                if equity_enabled:
                    self._guard_open_positions()
                    self._manage_open_positions()
                if self.strict_options_mode:
                    try:
                        options_runtime.tick(now=ist_now)
                    except Exception as runtime_exc:
                        logger.critical(
                            "options_intraday tick unresolved: %s", runtime_exc)
                else:
                    self._guard_open_positions()
                    self._manage_open_positions()
                if tick_hook is not None and not self.strict_options_mode:
                    try:
                        tick_hook()
                    except Exception as hook_exc:
                        logger.warning("run_session tick_hook failed: %s",
                                       hook_exc)
                # EOD: square off once the window closes.
                if equity_enabled:
                    self._maybe_eod_square_off()
                option_count = (len((options_runtime._state.get("positions", {})
                                     if options_runtime is not None else {}))
                                if self.strict_options_mode else 0)
                self._log_event("scan", {
                    "session_open": self._session_open(),
                    "positions": len(self.broker.positions) + option_count})
                try:
                    from src.alerts import handle_all_joins
                    handle_all_joins()
                except Exception:
                    pass
            except Exception as exc:
                logger.error("run_session tick failed: %s", exc)
            # Stop only when the MARKET is closed, not when entries stop.
            # (_session_open/can_enter flips False at 15:00 but hard-flat is
            # 15:10 — breaking there skipped the forced square-off.)
            if not self._market_still_running():
                logger.info("Market closed — final square-off.")
                if self.strict_options_mode and options_runtime is not None:
                    try:
                        options_runtime.finalize(now=ist_now)
                    except Exception as runtime_exc:
                        logger.critical(
                            "options_intraday finalization unresolved: %s",
                            runtime_exc)
                        self._options_unresolved_exits = {
                            "strict_options": str(runtime_exc),
                        }
                if equity_enabled:
                    self._maybe_eod_square_off()
                break
            if max_seconds and (time.time() - start) >= max_seconds:
                logger.info("run_session: max_seconds reached.")
                if self.strict_options_mode and options_runtime is not None:
                    try:
                        options_runtime.finalize(now=ist_now)
                    except Exception as runtime_exc:
                        logger.critical(
                            "options_intraday max-time finalization unresolved: %s",
                            runtime_exc)
                        self._options_unresolved_exits = {
                            "strict_options": str(runtime_exc),
                        }
                if equity_enabled:
                    self._maybe_eod_square_off(now_ist=ist_now)
                break
            time.sleep(max(5, interval_seconds))
        self._write_trade_log_csv()
