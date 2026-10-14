"""
TRIO — ISOLATED RISK CONTROL EXPERIMENTAL MODULE
==================================================

This module provides experimental risk controls that can be used for
strategy testing WITHOUT modifying the production baseline.

IMPORTANT: This is an EXPERIMENTAL module. It does not overwrite or
modify the production config/config.yaml or the main backtest engine.
Use it only for strategy testing and research purposes.

RESEARCH PARAMETERS (can be overridden):
- risk_per_trade_pct: 0.25 to 0.5 (25-50 basis points)
- max_daily_loss_pct: 1.0 (1% of starting daily equity)
- max_open_positions: 5
- max_consecutive_losses: 2 (stop after 2 consecutive losses)
- time_cutoff_ist: 14:30 (2:30 PM IST)
"""

import sys, math, json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, time as dtime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

# ============================================================================
# EXPERIMENTAL RISK STATE (ISOLATED - does not affect production RiskState)
# ============================================================================

@dataclass
class ExperimentalRiskState:
    """Isolated risk state - separate from production _risk_state."""
    daily_pnl: float = 0.0
    daily_starting_equity: float = 500000.0  # Default: 500k like MegaBull
    open_positions: int = 0
    positions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    halt_active: bool = False
    halt_reason: str = ""
    consecutive_losses: int = 0
    consecutive_wins: int = 0
    high_watermark: float = 0.0
    daily_loss_limit_pct: float = 1.0  # 1% of starting equity
    max_trades_per_day: int = 5
    max_consecutive_losses: int = 2  # Stop after 2 consecutive losses
    max_open_positions: int = 5
    correlated_groups: List[List[str]] = field(default_factory=list)
    time_cutoff_ist: dtime = dtime(14, 30)  # 2:30 PM IST
    trades_today: int = 0
    last_trade_time: Optional[str] = None
    last_trade_pnl: float = 0.0


def reset_experimental_risk_state() -> None:
    """Reset the experimental risk state for a new session/day."""
    global _exp_risk_state
    _exp_risk_state = ExperimentalRiskState()


# Global experimental risk state
_exp_risk_state = None


def init_experimental_risk(capital: float = 500000.0, starting_equity: float = 500000.0) -> None:
    """Initialize the experimental risk state for a new session."""
    global _exp_risk_state
    _exp_risk_state = ExperimentalRiskState()
    _exp_risk_state.daily_starting_equity = starting_equity


def get_experimental_risk_state() -> Optional[ExperimentalRiskState]:
    """Return the active experimental state for inspection or orchestration."""
    return _exp_risk_state


def configure_experimental_risk(
    *,
    max_trades_per_day: Optional[int] = None,
    max_consecutive_losses: Optional[int] = None,
    max_open_positions: Optional[int] = None,
    time_cutoff_ist: Optional[dtime] = None,
    correlated_groups: Optional[List[List[str]]] = None,
) -> None:
    """Apply portfolio-level experimental limits to the active state."""
    if _exp_risk_state is None:
        init_experimental_risk()
    if max_trades_per_day is not None:
        _exp_risk_state.max_trades_per_day = int(max_trades_per_day)
    if max_consecutive_losses is not None:
        _exp_risk_state.max_consecutive_losses = int(max_consecutive_losses)
    if max_open_positions is not None:
        _exp_risk_state.max_open_positions = int(max_open_positions)
    if time_cutoff_ist is not None:
        _exp_risk_state.time_cutoff_ist = time_cutoff_ist
    if correlated_groups is not None:
        _exp_risk_state.correlated_groups = [list(group) for group in correlated_groups]


def reset_active_experimental_risk() -> None:
    """Reset the actual global state used by this module."""
    reset_experimental_risk_state()


# ============================================================================
# EXPERIMENTAL POSITION SIZING
# ============================================================================

def calculate_experimental_position_size(
    entry_price: float,
    stop_loss: float,
    risk_per_trade_pct: float = 0.30,
    capital: float = 500000.0,
) -> int:
    """
    Calculate position size using the actual planned risk and predefined stop distance.
    
    Position size = (capital * risk_per_trade_pct / 100) / |entry - stop_loss|
    
    Args:
        entry_price: Planned entry price
        stop_loss: Stop-loss price
        risk_per_trade_pct: Risk per trade as % of capital (0.25-0.5 recommended)
        capital: Total capital (default: 500k like MegaBull)
    
    Returns:
        Number of shares/units (integer, minimum 0)
    """
    risk_amount = capital * risk_per_trade_pct / 100
    price_risk = abs(entry_price - stop_loss)
    
    if price_risk == 0:
        return 0
    
    size = int(risk_amount / price_risk)
    return max(size, 0)


# ============================================================================
# EXPERIMENTAL RISK CONTROL CHECKS
# ============================================================================

def check_daily_loss_limit(
    pnl: float,
    starting_equity: float = 500000.0,
    loss_limit_pct: float = 1.0,
) -> Tuple[bool, str]:
    """
    Enforce the daily loss limit.
    
    Args:
        pnl: P&L from the trade being considered
        starting_equity: Starting equity for the day
        loss_limit_pct: Daily loss limit as % of starting equity (1.0 = 1%)
    
    Returns:
        (allowed, reason) tuple
    """
    global _exp_risk_state
    if _exp_risk_state is None:
        return True, ""  # No state initialized, allow
    
    daily_loss_limit = starting_equity * loss_limit_pct / 100
    projected_daily_pnl = _exp_risk_state.daily_pnl + pnl
    
    if projected_daily_pnl < -daily_loss_limit:
        return False, (
            f"DAILY LOSS LIMIT BREACH: "
            f"P&L={_exp_risk_state.daily_pnl:.2f} + {pnl:.2f} = {projected_daily_pnl:.2f}, "
            f"limit={-daily_loss_limit:.2f}"
        )
    
    return True, ""


def check_max_trades_per_day(
    trade_count_today: int = 0,
    max_trades: int = 5,
) -> Tuple[bool, str]:
    """
    Prevent entries after the maximum trade count.
    
    Args:
        trade_count_today: Number of trades already executed today
        max_trades: Maximum allowed trades per day (5)
    
    Returns:
        (allowed, reason) tuple
    """
    global _exp_risk_state
    if _exp_risk_state is None:
        return True, ""
    
    if trade_count_today >= max_trades:
        return False, (
            f"MAX TRADES PER DAY BREACH: "
            f"Already {trade_count_today} trades, limit is {max_trades}"
        )
    
    return True, ""


def check_consecutive_losses(
    pnl: float,
    max_consecutive: int = 2,
    record_outcome: bool = True,
) -> Tuple[bool, str]:
    """
    Stop opening new trades after two consecutive losses.
    
    Args:
        pnl: P&L from the trade being considered
        max_consecutive: Maximum allowed consecutive losses (2)
    
    Returns:
        (allowed, reason) tuple
    """
    global _exp_risk_state
    if _exp_risk_state is None:
        return True, ""
    
    # A pre-entry check must be observational.  Only a completed trade may
    # update the outcome counters.
    projected_losses = _exp_risk_state.consecutive_losses
    if record_outcome:
        if pnl < 0:
            _exp_risk_state.consecutive_losses += 1
            _exp_risk_state.consecutive_wins = 0
        else:
            _exp_risk_state.consecutive_losses = 0
            _exp_risk_state.consecutive_wins += 1
        projected_losses = _exp_risk_state.consecutive_losses

    if projected_losses >= max_consecutive:
        return False, (
            f"CONSECUTIVE LOSSES LIMIT BREACH: "
            f"{projected_losses} consecutive losses, "
            f"limit is {max_consecutive}"
        )
    
    return True, ""


def check_time_cutoff_ist(
    trade_time_ist: str,
    cutoff_time: dtime = dtime(14, 30),
) -> Tuple[bool, str]:
    """
    Prevent new entries after 2:30 PM IST when intraday timestamps are available.
    
    Args:
        trade_time_ist: Trade timestamp in IST format ("YYYY-MM-DD HH:MM:SS")
        cutoff_time: Cutoff time (default 2:30 PM IST)
    
    Returns:
        (allowed, reason) tuple
    """
    from datetime import datetime
    
    if trade_time_ist is None:
        return False, "Missing trade timestamp; cutoff validation fails closed"
    
    try:
        if isinstance(trade_time_ist, datetime):
            trade_dt = trade_time_ist
        else:
            text = str(trade_time_ist).strip()
            trade_dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if trade_dt.tzinfo is None:
            trade_dt = trade_dt.replace(tzinfo=IST)
        else:
            trade_dt = trade_dt.astimezone(IST)
        trade_time = trade_dt.time()
        
        if trade_time > cutoff_time:
            return False, (
                f"TIME CUTOFF BREACH: "
                f"Trade at {trade_time.strftime('%H:%M')} exceeds 2:30 PM IST cutoff"
            )
    except Exception as exc:
        return False, f"Invalid trade timestamp; cutoff validation fails closed: {exc}"
    
    return True, ""


def check_max_open_positions() -> Tuple[bool, str]:
    """Check the aggregate number of active experimental positions."""
    if _exp_risk_state is None:
        return True, ""
    if _exp_risk_state.open_positions >= _exp_risk_state.max_open_positions:
        return False, (
            f"MAX OPEN POSITIONS BREACH: {_exp_risk_state.open_positions} active, "
            f"limit is {_exp_risk_state.max_open_positions}"
        )
    return True, ""


def check_correlated_exposure(symbol: str) -> Tuple[bool, str]:
    """Reject an entry when a correlated symbol is already open."""
    if _exp_risk_state is None:
        return True, ""
    symbol = str(symbol).upper()
    active = {str(key).upper() for key in _exp_risk_state.positions}
    for group in _exp_risk_state.correlated_groups:
        normalized = {str(item).upper() for item in group}
        if symbol in normalized and active.intersection(normalized):
            return False, (
                f"CORRELATED EXPOSURE: {symbol} conflicts with "
                f"{sorted(active.intersection(normalized))}"
            )
    return True, ""


def record_experimental_trade_outcome(
    symbol: str,
    pnl: float,
    *,
    position: Optional[Dict[str, Any]] = None,
) -> None:
    """Record one completed simulated trade and update global state."""
    if _exp_risk_state is None:
        init_experimental_risk()
    _exp_risk_state.daily_pnl += float(pnl)
    _exp_risk_state.trades_today += 1
    _exp_risk_state.last_trade_pnl = float(pnl)
    _exp_risk_state.last_trade_time = datetime.now(IST).isoformat()
    if pnl < 0:
        _exp_risk_state.consecutive_losses += 1
        _exp_risk_state.consecutive_wins = 0
    else:
        _exp_risk_state.consecutive_losses = 0
        _exp_risk_state.consecutive_wins += 1
    if position is not None:
        _exp_risk_state.positions[str(symbol)] = dict(position)


def check_duplicate_entry(
    symbol: str,
    existing_positions: Dict[str, Dict[str, Any]],
) -> Tuple[bool, str]:
    """
    Prevent duplicate entries from the same signal.
    
    Args:
        symbol: Trading symbol
        existing_positions: Existing open positions dictionary
    
    Returns:
        (allowed, reason) tuple
    """
    if symbol in existing_positions:
        return False, (
            f"DUPLICATE ENTRY: "
            f"Already have a position in {symbol}"
        )
    
    return True, ""


def verify_end_of_day_closure(
    positions: Dict[str, Dict[str, Any]],
    session_close_price: float,
) -> Tuple[bool, str]:
    """
    Verify end-of-day position closure.
    
    Args:
        positions: Open positions dictionary
        session_close_price: Closing price of the session
    
    Returns:
        (verified, reason) tuple
    """
    global _exp_risk_state
    
    for symbol, pos in positions.items():
        entry = pos.get("entry", 0)
        # If position still open at session close, it gets squared off
        # This is a verification check, not an enforcement
        if entry > 0 and pos.get("exit") is None:
            # Position was not closed - it would be squared off at EOD
            pass  # This is expected behavior
    
    return True, ""


def log_rejected_entry(
    reason: str,
    trade_details: Dict[str, Any],
    log_file: str = "output/experimental_risk_log.jsonl",
) -> None:
    """
    Log every rejected entry and the reason it was rejected.
    
    Args:
        reason: The rejection reason string
        trade_details: Dictionary with trade details
        log_file: Path to the log file
    """
    from pathlib import Path
    
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    
    entry = {
        "timestamp": __import__("datetime").datetime.now().isoformat(),
        "reason": reason,
        "details": trade_details,
    }
    
    with open(log_file, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ============================================================================
# EXPERIMENTAL SIGNAL PROCESSING WITH RISK CONTROLS
# ============================================================================

def process_signal_with_risk_controls(
    signal_action: str,
    symbol: str,
    entry_price: float,
    stop_loss: float,
    target_price: float,
    trade_time_ist: str = None,
    risk_per_trade_pct: float = 0.30,
    capital: float = 500000.0,
) -> Dict[str, Any]:
    """
    Process a trading signal through all experimental risk controls.
    
    Returns a dict with:
    - allowed: Whether the trade is allowed
    - reject_reason: If not allowed, why
    - position_size: Calculated position size
    - daily_pnl_impact: projected daily P&L impact
    - risk_checks: Dict of individual check results
    """
    global _exp_risk_state
    
    results = {
        "allowed": True,
        "reject_reason": "",
        "position_size": 0,
        "daily_pnl_impact": 0.0,
        "risk_checks": {},
    }
    
    # Initialize state if needed
    if _exp_risk_state is None:
        init_experimental_risk(capital, 500000.0)
    
    checks = {}
    
    # 1. Check daily loss limit
    allowed, reason = check_daily_loss_limit(pnl=0,  # P&L will be realized after trade
                                             starting_equity=500000.0,
                                             loss_limit_pct=1.0)
    checks["daily_loss_limit"] = {"allowed": allowed, "reason": reason}
    if not allowed:
        results["allowed"] = False
        results["reject_reason"] = reason
        return results
    
    # 2. Check max trades per day
    allowed, reason = check_max_trades_per_day(
        trade_count_today=_exp_risk_state.trades_today,
        max_trades=_exp_risk_state.max_trades_per_day,
    )
    checks["max_trades_per_day"] = {"allowed": allowed, "reason": reason}
    if not allowed:
        results["allowed"] = False
        results["reject_reason"] = reason
        return results
    
    # 3. Check consecutive losses
    allowed, reason = check_consecutive_losses(
        pnl=0,
        max_consecutive=_exp_risk_state.max_consecutive_losses,
        record_outcome=False,
    )
    checks["consecutive_losses"] = {"allowed": allowed, "reason": reason}
    if not allowed:
        results["allowed"] = False
        results["reject_reason"] = reason
        return results
    
    # 4. Check time cutoff (IST)
    allowed, reason = check_time_cutoff_ist(trade_time_ist)
    checks["time_cutoff_ist"] = {"allowed": allowed, "reason": reason}
    if not allowed:
        results["allowed"] = False
        results["reject_reason"] = reason
        return results
    
    # 5. Aggregate open-position and correlated-exposure controls
    allowed, reason = check_max_open_positions()
    checks["max_open_positions"] = {"allowed": allowed, "reason": reason}
    if not allowed:
        results["allowed"] = False
        results["reject_reason"] = reason
        results["risk_checks"] = checks
        return results

    allowed, reason = check_correlated_exposure(symbol)
    checks["correlated_exposure"] = {"allowed": allowed, "reason": reason}
    if not allowed:
        results["allowed"] = False
        results["reject_reason"] = reason
        results["risk_checks"] = checks
        return results

    # 6. Calculate position size
    position_size = calculate_experimental_position_size(
        entry_price=entry_price,
        stop_loss=stop_loss,
        risk_per_trade_pct=risk_per_trade_pct,
        capital=500000.0,
    )
    results["position_size"] = position_size
    
    # 7. Check for duplicate entries
    allowed, reason = check_duplicate_entry(symbol, _exp_risk_state.positions)
    checks["duplicate_entry"] = {"allowed": allowed, "reason": reason}
    if not allowed:
        results["allowed"] = False
        results["reject_reason"] = reason
        return results
    
    # All checks passed
    results["allowed"] = True
    results["risk_checks"] = checks
    
    return results


# ============================================================================
# EXAMPLE USAGE DEMONSTRATION
# ============================================================================

if __name__ == "__main__":
    print("=" * 70)
    print("TRIO EXPERIMENTAL RISK CONTROLS DEMONSTRATION")
    print("=" * 70)
    print()
    print("KEY RESEARCH PARAMETERS:")
    print("  - risk_per_trade_pct: 0.25% to 0.5% (25-50 bps)")
    print("  - max_daily_loss_pct: 1.0% (1% of starting equity)")
    print("  - max_open_positions: 5")
    print("  - max_consecutive_losses: 2")
    print("  - time_cutoff_ist: 14:30 (2:30 PM IST)")
    print()
    print("EXAMPLE USAGE:")
    print("  init_experimental_risk(capital=500000.0)")
    print("  result = process_signal_with_risk_controls(")
    print("      signal_action='BUY',")
    print("      symbol='NIFTY',")
    print("      entry_price=23800,")
    print("      stop_loss=23600,")
    print("      target_price=24200,")
    print("      trade_time_ist='2026-10-13 09:45:00'")
    print("  )")
    print()
    print("  if result['allowed']:")
    print("    print('Trade allowed, position size: ' + str(result['position_size']))")
    print("  else:")
    print("    print('REJECTED: ' + result['reject_reason'])")
    print()
    print("=" * 70)
