"""
TRIO — Risk Manager

Handles position sizing, stop-loss / take-profit calculations,
trailing stops, daily loss limits, exposure caps, and the trading halt switch.

Every signal that passes through risk management gets concrete entry, stop,
target, and position size values.

DISCLAIMER: Educational purposes only. Not financial advice.
"""

import logging
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from src.utils import get_logger, load_config, utc_now

logger = get_logger("risk_manager")


# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------

@dataclass
class RiskState:
    """Tracks current risk state across the trading session.

    Halt policy (explicit per-reason):
      daily_loss      -> session-persistent (rest of session)
      consecutive_loss-> session-persistent (rest of session)
      broker_uncertain-> persists until reconciliation confirms the book
      max_positions   -> DYNAMIC: clears when count falls below the limit
      drawdown        -> session-persistent once breached
    """
    daily_pnl: float = 0.0
    open_positions: int = 0
    positions: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # symbol -> position info
    halt_active: bool = False
    halt_reason: str = ""
    halts: Dict[str, str] = field(default_factory=dict)  # reason -> message
    consecutive_losses: int = 0
    consecutive_wins: int = 0
    high_watermark: float = 0.0
    weekly_pnl: float = 0.0
    last_session_date: str = ""
    last_reconciled_at: str = ""
    partial_history: List[Dict[str, Any]] = field(default_factory=list)

    def reset_daily(self) -> None:
        """Reset daily counters (call at start of each trading day)."""
        self.daily_pnl = 0.0
        self.halt_active = False
        self.halt_reason = ""
        self.halts = {}
        self.consecutive_losses = 0
        self.consecutive_wins = 0
        self.high_watermark = 0.0


# Global risk state
_risk_state = RiskState()


def get_risk_state() -> RiskState:
    """Return the current risk state."""
    return _risk_state


def reset_risk_state() -> None:
    """Reset all risk state (for testing or new session)."""
    global _risk_state
    _risk_state = RiskState()


# ---------------------------------------------------------------------------
# Restart / ledger rebuild
# ---------------------------------------------------------------------------

def rebuild_from_ledger(
    positions: Dict[str, Any],
    closed_trades: List[Dict[str, Any]],
    *,
    today: Optional[str] = None,
) -> RiskState:
    """Rebuild :data:`_risk_state` from broker snapshots + the JSONL ledger.

    Knows nothing about TRIO's own ``RiskState`` serialization — it derives
    everything from the broker's own truth: ``positions`` (open) and
    ``closed_trades`` / ``trade_log.jsonl`` (realized P&L). An empty ledger
    or a ledger that cannot be read is treated as "no realized P&L today".

    Call after restart, before any new entry is considered.

    Args:
        positions:     Current open positions (``{symbol: pos}``).
        closed_trades: Broker's own ``closed_trades`` list.
        today:         ISO date ``YYYY-MM-DD`` IST (default: today).
    """
    from datetime import date as _d
    if today is None:
        try:
            from src.utils import load_config as _lc  # noqa
            from datetime import datetime, timedelta, timezone as _tz
            IST = _tz(timedelta(hours=5, minutes=30))
            today = datetime.now(IST).strftime("%Y-%m-%d")
        except Exception:
            today = str(_d.today())

    reset_risk_state()
    # Open positions
    for sym, pos in (positions or {}).items():
        qty = getattr(pos, "quantity", None)
        if qty is None:
            qty = pos.get("quantity", 0) if isinstance(pos, dict) else 0
        avg = getattr(pos, "avg_price", None)
        if avg is None:
            avg = pos.get("avg_price", 0.0) if isinstance(pos, dict) else 0.0
        side = getattr(pos, "side", None)
        if side is None:
            side = pos.get("side", "") if isinstance(pos, dict) else ""
        _risk_state.positions[sym] = {
            "size": int(qty or 0), "entry": float(avg or 0.0),
            "side": str(side or ""), "risk_amount": 0.0,
        }
    _risk_state.open_positions = len(_risk_state.positions)
    _risk_state.last_session_date = today

    # Realized P&L for today. Dedup broker closed_trades vs the JSONL
    # ledger by stable key (order_id > trade_id > composite) so a restart
    # never double-counts P&L, and a hydrated broker record never hides a
    # ledger-only close (process died before the mirror was saved).
    from datetime import datetime as _DT, timedelta as _TDD, timezone as _TZZ
    _IST = _TZZ(_TDD(hours=5, minutes=30))

    def _key(r: Dict[str, Any]) -> str:
        for k in ("order_id", "trade_id", "close_id"):
            v = r.get(k)
            if v:
                return f"{k}:{v}"
        return ("cmp:%s|%s|%s|%s|%s" % (
            r.get("symbol"), r.get("side"), r.get("entry"),
            r.get("exit"), r.get("quantity")))

    def _ist_day(ts: str) -> str:
        try:
            dt = _DT.fromisoformat(str(ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_TZZ.utc)
            return dt.astimezone(_IST).strftime("%Y-%m-%d")
        except Exception:
            return ""

    seen: Dict[str, float] = {}
    ordered_pnls: List[float] = []
    # 1) broker records first
    for r in (closed_trades or []):
        try:
            ts = str(r.get("closed_at") or r.get("entry_time") or r.get("ts") or "")
            day = _ist_day(ts) if ts else today
            if day and day != today:
                continue
            k = _key(r if isinstance(r, dict) else {})
            if k in seen:
                continue
            v = float(r.get("pnl") or 0.0)
            seen[k] = v
            ordered_pnls.append(v)
        except Exception:
            continue
    # 2) JSONL ledger (UTC ts -> IST day; test-mode rows ignored; never double-sum)
    try:
        from pathlib import Path as _P
        from src.utils import load_config as _lc2
        cfg = _lc2()
        log_path = _P(cfg.get("logging", {}).get("trade_log",
                        "output/trade_log.jsonl"))
        if log_path.exists():
            import json as _j
            for line in log_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = _j.loads(line)
                except Exception:
                    continue
                if rec.get("event") != "close" or rec.get("mode") == "test":
                    continue
                if _ist_day(str(rec.get("ts") or "")) != today:
                    continue
                k = _key(rec)
                if k in seen:
                    continue
                v = float(rec.get("pnl") or 0.0)
                seen[k] = v
                ordered_pnls.append(v)
    except Exception:
        pass

    _risk_state.daily_pnl = round(sum(seen.values()), 2)
    _risk_state.high_watermark = max(_risk_state.daily_pnl, 0.0)
    # Consecutive losses / wins from the time-ordered close sequence.
    _risk_state.consecutive_losses = 0
    _risk_state.consecutive_wins = 0
    for v in ordered_pnls:
        if v < 0:
            _risk_state.consecutive_losses += 1
            _risk_state.consecutive_wins = 0
        elif v > 0:
            _risk_state.consecutive_wins += 1
            _risk_state.consecutive_losses = 0
    _check_halt()
    return _risk_state


def on_broker_retry_failed(symbol: str) -> bool:
    """A remote place/close returned UNCERTAIN (network hit, never confirmed).

    Returns True when the caller should treat the book as inconclusive and
    block new entries until ``refresh_positions`` confirms the mirror.
    """
    return True


def update_pnl(pnl: float) -> None:
    """Update the daily PnL (+ consecutive-loss/win + drawdown watermark)."""
    _risk_state.daily_pnl += pnl
    # watermark for drawdown (peak of today's realized equity)
    if _risk_state.daily_pnl > _risk_state.high_watermark:
        _risk_state.high_watermark = _risk_state.daily_pnl
    if pnl < 0:
        _risk_state.consecutive_losses += 1
        _risk_state.consecutive_wins = 0
    elif pnl > 0:
        _risk_state.consecutive_wins += 1
        _risk_state.consecutive_losses = 0
    _check_halt()


def add_position(symbol: str, size: int, entry: float, risk_amount: float) -> None:
    """Register an open position (also resets the consecutive-loss run)."""
    _risk_state.positions[symbol] = {
        "size": size, "entry": entry, "risk_amount": risk_amount,
    }
    _risk_state.open_positions = len(_risk_state.positions)
    _check_halt()


def close_position(symbol: str) -> None:
    """Close (remove) a position — does not re-trigger halt checks here."""
    _risk_state.positions.pop(symbol, None)
    _risk_state.open_positions = len(_risk_state.positions)


# ---------------------------------------------------------------------------
# Halt checks
# ---------------------------------------------------------------------------

def sector_exposure(positions: Dict[str, Any],
                      capital: Optional[float] = None) -> Dict[str, float]:
    """Sector exposure as % of capital (per Phase 7).

    Args:
        positions: {symbol -> {size, entry, ...}} or broker positions.
        capital:   Own capital, defaults to config.
    """
    from src.sector_map import sector_for
    if capital is None:
        cfg = load_config()
        capital = float((cfg.get("risk_management", {}) or {}).get("capital", 100000) or 100000)
    cap = float(capital) or 100000.0
    out: Dict[str, float] = {}
    totals: Dict[str, float] = {}
    for sym, info in (positions or {}).items():
        sz = float((info or {}).get("size", info.get("quantity", 0)) if isinstance(info, dict) else 0)
        entry = float((info or {}).get("entry", info.get("avg_price", 0)) if isinstance(info, dict) else 0)
        try:
            notional = sz * entry
        except Exception:
            notional = 0.0
        sec = sector_for(sym)
        totals[sec] = totals.get(sec, 0.0) + notional
    for sec, notional in totals.items():
        out[sec] = round(notional / cap * 100, 2)
    return out


def would_breach_sector(symbol: str, size: int, entry: float,
                        existing: Optional[Dict[str, Any]] = None,
                        limit_pct: Optional[float] = None) -> bool:
    """True if adding one more position would breach the per-sector cap."""
    from src.sector_map import sector_for
    cfg = load_config()
    rm = (cfg.get("risk_management", {}) or {})
    lim = limit_pct if limit_pct is not None else float(rm.get("max_exposure_per_sector_pct", 40) or 40)
    cap = float(rm.get("capital", 100000) or 100000)
    merged = dict(existing or {})
    merged = dict(merged)
    # Don't override an existing entry for the same symbol; count the new
    # not as add, not as replacement, for the breach preview.
    sec = sector_for(symbol)
    proposed = dict(merged)
    proposed[symbol] = {"size": int(size or 0), "entry": float(entry or 0.0)}
    exp = sector_exposure(proposed, capital=cap)
    return float(exp.get(sec, 0.0)) > lim


def _check_halt(unrealized_total: Optional[float] = None) -> None:
    """Check if trading should be halted (realized + optional unrealized)."""
    cfg = load_config()
    rm_cfg = cfg.get("risk_management", {})

    try:
        capital = float(rm_cfg.get("capital", 100000) or 100000)
    except Exception:
        capital = 100000.0

    def _set(reason: str, msg: str, level: str = "critical") -> None:
        _risk_state.halts[reason] = msg
        _risk_state.halt_active = True
        _risk_state.halt_reason = msg
        if level == "critical":
            logger.critical("TRADING HALTED: %s", msg)
        else:
            logger.warning("TRADING HALTED: %s", msg)

    # 1) Daily realized loss
    max_daily_loss_pct = float(rm_cfg.get("max_daily_loss_pct", 5.0) or 0.0)
    max_daily_loss = capital * max_daily_loss_pct / 100
    if max_daily_loss > 0 and _risk_state.daily_pnl <= -max_daily_loss:
        _set("daily_loss",
             f"Daily loss limit breached: PnL={_risk_state.daily_pnl:.2f} "
             f"(limit={-max_daily_loss:.2f})")

    # 2) Daily realized + unrealized (mark-to-market drawdown within the day)
    if unrealized_total is not None:
        eff = _risk_state.daily_pnl + float(unrealized_total)
        if max_daily_loss > 0 and eff <= -max_daily_loss:
            _set("daily_loss",
                 f"Daily loss (incl. unrealized) breached: {eff:.2f}")

    # 3) Max open positions — DYNAMIC: clears when count falls below limit.
    max_positions = int(rm_cfg.get("max_open_positions", 5) or 5)
    if _risk_state.open_positions >= max_positions:
        _set("max_positions",
             f"Max open positions reached: {_risk_state.open_positions}/{max_positions}",
             level="warning")
    else:
        _risk_state.halts.pop("max_positions", None)
        # Recompute halt_active from the remaining (session-persistent) reasons.
        if not any(k in _risk_state.halts for k in
                   ("daily_loss", "consecutive_loss", "broker_uncertain", "drawdown")):
            _risk_state.halt_active = False
            _risk_state.halt_reason = ""

    # 4) Consecutive losing trades
    max_consec = int(rm_cfg.get("max_consecutive_losses", 0) or 0)
    if max_consec > 0 and _risk_state.consecutive_losses >= max_consec:
        _set("consecutive_loss",
             f"Consecutive losses limit: {_risk_state.consecutive_losses}/{max_consec}")

    # 5) Drawdown off the high watermark (realized)
    max_dd_pct = float(rm_cfg.get("max_drawdown_pct", 0.0) or 0.0)
    if max_dd_pct > 0 and _risk_state.high_watermark > 0:
        dd = (_risk_state.high_watermark - _risk_state.daily_pnl) / max(
            _risk_state.high_watermark, 1.0) * 100
        if dd >= max_dd_pct:
            _set("drawdown",
                 f"Max drawdown breached: {dd:.1f}% (limit {max_dd_pct}%)")


def set_broker_uncertain(msg: str) -> None:
    """Mark the remote book UNKNOWN — blocks new entries until reconciled."""
    _risk_state.halts["broker_uncertain"] = msg
    _risk_state.halt_active = True
    _risk_state.halt_reason = msg
    logger.critical("TRADING HALTED (broker uncertain): %s", msg)


def clear_broker_uncertain() -> None:
    """Reconciliation confirmed the book — lift only the uncertain halt."""
    _risk_state.halts.pop("broker_uncertain", None)
    try:
        from src.utils import utc_now
        _risk_state.last_reconciled_at = str(utc_now())
    except Exception:
        pass
    if not _risk_state.halts:
        _risk_state.halt_active = False
        _risk_state.halt_reason = ""


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------

def calculate_position_size(
    entry_price: float,
    stop_loss: float,
    capital: Optional[float] = None,
    risk_per_trade_pct: Optional[float] = None,
) -> int:
    """
    Calculate position size based on fixed percentage risk.

    Position size = (capital * risk_pct) / |entry - stop_loss|

    Args:
        entry_price:       Planned entry price.
        stop_loss:         Stop-loss price.
        capital:           Total capital (from config if None).
        risk_per_trade_pct: % of capital to risk (from config if None).

    Returns:
        Number of shares/units (integer).
    """
    cfg = load_config()
    rm_cfg = cfg.get("risk_management", {})

    if capital is None:
        capital = rm_cfg.get("capital", 100000)
    if risk_per_trade_pct is None:
        risk_per_trade_pct = rm_cfg.get("risk_per_trade_pct", 1.5)

    risk_amount = capital * risk_per_trade_pct / 100
    price_risk = abs(entry_price - stop_loss)

    if price_risk == 0:
        logger.warning("Stop-loss equals entry price, cannot calculate position size")
        return 0

    size = int(risk_amount / price_risk)

    # Exposure cap is a NOTIONAL fraction of capital (fraction of account
    # value tied up in one position), not a share count. On a 23k index
    # with 1.5% risk, a 20% notional cap allows floor(20000/23000)=0
    # shares — every index signal sizes to zero. The cap now scales:
    #   - equity-like prices (< 20000): keep 20% notional cap (realistic
    #     for CNC delivery where full notional leaves the account)
    #   - index-like prices (>= 20000): treat the cap as 5x notional,
    #     approximating MIS/intraday leverage where only margin is blocked.
    # Without this, index backtests can never take a single trade.
    max_exposure_pct = rm_cfg.get("max_exposure_per_symbol_pct", 20)
    leverage = rm_cfg.get("index_leverage", 5) if entry_price >= 20000 else 1
    max_exposure = capital * max_exposure_pct / 100 * leverage
    max_size_by_exposure = int(max_exposure / entry_price) if entry_price > 0 else 0

    size = min(size, max_size_by_exposure)

    return max(size, 0)


# ---------------------------------------------------------------------------
# Stop-loss & Take-profit
# ---------------------------------------------------------------------------

def calculate_stop_loss(
    entry_price: float,
    atr_value: Optional[float] = None,
    action: str = "BUY",
    config_override: Optional[Dict[str, Any]] = None,
    swing_level: Optional[float] = None,
) -> float:
    """
    Calculate stop-loss price.

    Strategy note
    -------------
    The default method is "swing": the stop sits just below the most recent
    swing low (for longs) or above the recent swing high (for shorts). This was
    chosen because an out-of-sample test across 15 NSE large caps over 5 years
    showed a swing stop produced a materially better profit factor than a
    fixed ATR multiple. Fixed-ATR variants that looked good in-sample fell
    below a 1.0 profit factor on unseen data (see scripts/validation_oos.json).

    Args:
        entry_price:     Entry price.
        atr_value:       ATR value (used when method=atr, or as fallback).
        action:          BUY or SELL.
        config_override: Override config.
        swing_level:     Recent swing low (BUY) / swing high (SELL). Required
                         when method=swing; falls back to ATR if None.

    Returns:
        Stop-loss price.
    """
    cfg = load_config()
    rm_cfg = config_override or cfg.get("risk_management", {})
    sl_cfg = rm_cfg.get("stop_loss", {})

    method = sl_cfg.get("method", "swing")
    min_dist_pct = sl_cfg.get("min_stop_distance_pct", 1.0)

    distance = None

    if method == "swing" and swing_level is not None:
        buffer_pct = sl_cfg.get("swing_buffer_pct", 0.25) / 100
        if action == "BUY":
            distance = (entry_price - swing_level) + swing_level * buffer_pct
        else:
            distance = (swing_level - entry_price) + swing_level * buffer_pct

    if distance is None or distance <= 0:
        if atr_value is not None and atr_value > 0:
            distance = atr_value * sl_cfg.get("atr_multiplier", 3.5)
        else:
            distance = entry_price * sl_cfg.get("percentage", 3.0) / 100

    # Never let the stop sit so close that noise alone triggers it.
    floor = entry_price * min_dist_pct / 100
    if distance < floor:
        distance = floor

    if action == "BUY":
        return round(entry_price - distance, 2)
    else:  # SELL
        return round(entry_price + distance, 2)


def calculate_take_profit(
    entry_price: float,
    stop_loss: float,
    action: str = "BUY",
    min_rr: Optional[float] = None,
) -> float:
    """
    Calculate take-profit price based on minimum risk-reward ratio.

    Args:
        entry_price: Entry price.
        stop_loss:   Stop-loss price.
        action:      BUY or SELL.
        min_rr:      Minimum risk-reward ratio (from config if None).

    Returns:
        Take-profit target price.
    """
    cfg = load_config()
    rm_cfg = cfg.get("risk_management", {})

    if min_rr is None:
        min_rr = rm_cfg.get("take_profit", {}).get("min_risk_reward", 2.0)

    risk = abs(entry_price - stop_loss)

    if action == "BUY":
        return round(entry_price + risk * min_rr, 2)
    else:
        return round(entry_price - risk * min_rr, 2)


def calculate_breakeven_stop(
    entry_price: float,
    current_price: float,
    action: str = "BUY",
) -> Optional[float]:
    """Calculate breakeven stop if price is sufficiently in profit."""
    # Breakeven after +1R profit.
    if action == "BUY":
        if current_price >= entry_price:
            return round(entry_price, 2)
    else:  # SELL
        if current_price <= entry_price:
            return round(entry_price, 2)
    return None


def calculate_trailing_stop(
    current_price: float,
    entry_price: float,
    atr_value: Optional[float] = None,
    action: str = "BUY",
    config_override: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    """
    Calculate trailing stop if enabled in config.

    Returns None if trailing stop is disabled.
    """
    cfg = load_config()
    rm_cfg = config_override or cfg.get("risk_management", {})
    ts_cfg = rm_cfg.get("trailing_stop", {})

    if not ts_cfg.get("enabled", False):
        return None

    method = ts_cfg.get("method", "atr")

    if method == "atr" and atr_value is not None:
        multiplier = ts_cfg.get("atr_multiplier", 1.5)
        distance = atr_value * multiplier
    else:
        pct = ts_cfg.get("percentage", 1.0)
        distance = current_price * pct / 100

    if action == "BUY":
        return round(current_price - distance, 2)
    else:  # SELL
        return round(current_price + distance, 2)


# ---------------------------------------------------------------------------
# Apply risk management to a signal
# ---------------------------------------------------------------------------

def apply_risk_management(
    signal: Any,
    atr_value: Optional[float] = None,
    swing_level: Optional[float] = None,
) -> Any:
    """
    Apply risk management to a TradeSignal: calculate stop-loss, target,
    position size, risk amount, and check limits.

    Mutates and returns the signal object.

    Args:
        signal:      TradeSignal from signal_engine.
        atr_value:   ATR value from indicators.
        swing_level: Recent swing low/high from indicators. Preferred for the
                     "swing" stop method validated out-of-sample.

    Returns:
        The same TradeSignal with risk fields populated.
    """
    cfg = load_config()
    rm_cfg = cfg.get("risk_management", {})

    # Check halt
    if _risk_state.halt_active:
        signal.action = "HOLD"
        signal.reasoning.insert(0, f"HALTED: {_risk_state.halt_reason}")
        signal.confidence = 0
        signal.risk_check = {
            "halt_active": True,
            "halt_reason": _risk_state.halt_reason,
        }
        logger.warning("Signal blocked by trading halt: %s", _risk_state.halt_reason)
        return signal

    if signal.action == "HOLD" or signal.entry_price is None:
        signal.risk_check = {
            "daily_loss_remaining": _daily_loss_remaining(),
            "open_positions": _risk_state.open_positions,
            "max_positions": rm_cfg.get("max_open_positions", 5),
            "halt_active": False,
        }
        return signal

    entry = signal.entry_price
    action = signal.action

    # Stop-loss
    sl = calculate_stop_loss(entry, atr_value, action, swing_level=swing_level)
    signal.stop_loss = sl

    # Take-profit (ladder targets). Derive T1/T2/T3 from a single stop so
    # the Active Manager can scale out: T1=0.8R (50% + breakeven), T2=1.5R
    # (30% + trail), T3=2.5R runner — see ladder_targets(). T2/T3 are
    # armed but the manager shadow-logs them until a sweep validates.
    tp = calculate_take_profit(entry, sl, action)
    signal.target = tp
    # Ladder levels derived from configured t*_at_r; full ladder always
    # stored so forward paper + sweeps can measure T2/T3.
    ladder_cfg = rm_cfg.get("ladder", {}) if isinstance(rm_cfg.get("ladder"), dict) else {}
    t1_r = ladder_cfg.get("t1_at_r", rm_cfg.get("partial_at_r", 0.8))
    t2_r = ladder_cfg.get("t2_at_r", 1.5)
    t3_r = ladder_cfg.get("t3_at_r", 2.5)
    ladder = ladder_targets(entry, sl, action, t1_r=t1_r, t2_r=t2_r, t3_r=t3_r)
    signal.target_t1 = ladder["T1"]
    signal.target_t2 = ladder["T2"]
    signal.target_t3 = ladder["T3"]
    signal.ladder = ladder
    # Legacy single partial target (kept for existing tests/digest)
    signal.target_t1_legacy = calculate_take_profit(entry, sl, action, min_rr=t1_r)

    # Risk-reward ratio
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    rr = round(reward / risk, 1) if risk > 0 else 0
    signal.risk_reward_ratio = f"1:{rr}"

    # Position size
    size = calculate_position_size(entry, sl)
    signal.position_size = size

    # Risk amount
    signal.risk_amount = round(size * risk, 2)

    # Trailing stop
    ts_cfg = rm_cfg.get("trailing_stop", {})
    signal.trailing_stop = ts_cfg.get("enabled", False)

    # Risk check summary
    signal.risk_check = {
        "daily_loss_remaining": _daily_loss_remaining(),
        "open_positions": _risk_state.open_positions,
        "max_positions": rm_cfg.get("max_open_positions", 5),
        "halt_active": _risk_state.halt_active,
    }

    logger.info(
        "Risk applied %s %s: entry=%.2f SL=%.2f TP=%.2f size=%d risk=%.2f R:R=%s",
        action, signal.symbol, entry, sl, tp, size, signal.risk_amount, signal.risk_reward_ratio,
    )

    return signal


def ladder_targets(entry_price: float, stop_loss: float, action: str,
                 t1_r: float = 0.8, t2_r: float = 1.5, t3_r: float = 2.5
                 ) -> Dict[str, float]:
    """Derive intraday ladder levels T1/T2/T3 from a single stop.

    Your idea: T1 (0.8R) entry→breakeven, T2 (1.5R)→T1, T3 (2.5R).
    Scaled exits reduce \"356 mins to flat at EOD\" on a 1.5R miss.
    """
    risk = abs(entry_price - stop_loss)
    if action == "BUY":
        return {"T1": round(entry_price + risk * t1_r, 2),
                "T2": round(entry_price + risk * t2_r, 2),
                "T3": round(entry_price + risk * t3_r, 2)}
    return {"T1": round(entry_price - risk * t1_r, 2),
            "T2": round(entry_price - risk * t2_r, 2),
            "T3": round(entry_price - risk * t3_r, 2)}


def _daily_loss_remaining() -> float:
    """How much daily loss budget remains."""
    cfg = load_config()
    rm_cfg = cfg.get("risk_management", {})
    capital = rm_cfg.get("capital", 100000)
    max_pct = rm_cfg.get("max_daily_loss_pct", 5.0)
    max_loss = capital * max_pct / 100
    return round(max_loss + _risk_state.daily_pnl, 2)  # pnl is negative when losing
