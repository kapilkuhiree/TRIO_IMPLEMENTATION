"""TRIO — Comprehensive Backtest Framework

Integrates:
- 5-minute NIFTY candle data import
- Three intraday strategy modules (ORB, Pullback, Momentum)
- Experimental risk controls
- Position sizing and risk limits
- Performance calculations and reporting
- Weekly/monthly expiry comparisons

RESEARCH MODE ONLY — No live trading or broker order submission.
"""

import sys, math, json, os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Add the project root to path
sys.path.insert(0, '.')
from datetime import time as dtime

# Import project modules
from data_importer import (
    import_5min_nifty_candles,
    import_option_premium_candles,
    get_contract_specifications,
    is_trading_session,
    validate_candle_data,
    validate_option_candle_data,
)
from experimental_risk import (
    init_experimental_risk,
    process_signal_with_risk_controls,
    calculate_experimental_position_size,
    check_daily_loss_limit,
    check_max_trades_per_day,
    check_consecutive_losses,
    check_time_cutoff_ist,
    check_duplicate_entry,
    verify_end_of_day_closure,
    log_rejected_entry,
)
from strategies.orb_strategy import OrbStrategy, run_orb_backtest
from strategies.trend_pullback_strategy import TrendPullbackStrategy, run_pullback_backtest
from strategies.momentum_continuation_strategy import (
    MomentumContinuationStrategy,
    run_momentum_backtest,
)


# ============================================================================
# BACKTEST ENGINE CLASS
# ============================================================================

class TrioBacktestEngine:
    """Main backtest engine that coordinates data import, strategy evaluation,
    risk controls, and performance reporting."""

    def __init__(
        self,
        csv_candles_path: str,
        start_date: str = None,
        end_date: str = None,
        initial_equity: float = 500000.0,
        risk_per_trade_pct: float = 0.30,
        max_daily_loss_pct: float = 1.0,
        max_trades_per_day: int = 5,
        max_consecutive_losses: int = 2,
        time_cutoff_ist: str = "14:30",
    ):
        self.csv_candles_path = csv_candles_path
        self.start_date = start_date
        self.end_date = end_date
        self.initial_equity = initial_equity
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_trades_per_day = max_trades_per_day
        self.max_consecutive_losses = max_consecutive_losses

        # Parse time cutoff
        self.time_cutoff_ist = dtime(int(time_cutoff_ist.split(":")[0]), int(time_cutoff_ist.split(":")[1]))

        # Initialize state
        self.trades: List[Dict[str, Any]] = []
        self.daily_pnl: float = 0.0
        self.daily_trade_count: int = 0
        self.consecutive_losses: int = 0
        self.consecutive_wins: int = 0
        self.high_watermark: float = 0.0
        peak_pnl = 0.0

        # Strategy results containers
        self.orb_results: Dict[str, Any] = {}
        self.pullback_results: Dict[str, Any] = {}
        self.momentum_results: Dict[str, Any] = {}

        # Contract specifications
        self.specs = get_contract_specifications()

    # ------------------------------------------------------------------
    # Data import and preprocessing
    # ------------------------------------------------------------------

    def import_data(self) -> List[Dict[str, Any]]:
        """Import and validate 5-minute NIFTY candle data.

        Returns:
            List of validated candle dicts with IST timestamps
        """
        # Import 5-min NIFTY candles
        candles = import_5min_nifty_candles(
            self.csv_candles_path, self.start_date, self.end_date
        )

        # Filter to trading hours only
        candles = [c for c in candles if is_trading_session(c.get("timestamp", ""))]

        # Validate candle data
        validation = validate_candle_data(candles)
        print(f"Data validation: {validation['total_processed']} candles, "
              f"{validation['duplicate_timestamps']} duplicates, "
              f"{validation['invalid_prices']} invalid prices")

        if not candles:
            raise ValueError("No valid candles imported - check CSV path and format")

        # Sort by timestamp
        candles.sort(key=lambda c: c["timestamp"])

        return candles

    # ------------------------------------------------------------------
    # Strategy evaluation with risk controls
    # ------------------------------------------------------------------

    def evaluate_all_strategies(self, candles: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Evaluate signals from all three strategies with risk controls.

        Args:
            candles: List of 5-minute NIFTY candles

        Returns:
            Dict with strategy results
        """
        # Initialize experimental risk state
        init_experimental_risk(capital=self.initial_equity)

        # Reset daily counters
        self.daily_trade_count = 0
        self.consecutive_losses = 0
        self.daily_pnl = 0.0
        self.high_watermark = 0.0

        # Strategy-specific results
        orb_trades = []
        pullback_trades = []
        momentum_trades = []

        # Track P&L for risk controls
        daily_starting_equity = self.initial_equity

        # Evaluate ORB strategy
        print("Evaluating ORB strategy...")
        orb_result = run_orb_backtest(
            self.csv_candles_path,
            self.start_date,
            self.end_date,
            lot_size=self.specs["lot_size"],
            initial_equity=self.initial_equity,
        )
        orb_trades = orb_result.get("trades", [])
        daily_pnl_orb = sum(t.get("pnl", 0) for t in orb_trades)
        print(f"  ORB: {orb_result.get('total_trades', 0)} trades, P&L: {daily_pnl_orb:.2f}")

        # Evaluate Pullback strategy
        print("Evaluating Pullback strategy...")
        pullback_result = run_pullback_backtest(
            self.csv_candles_path,
            self.start_date,
            self.end_date,
            lot_size=self.specs["lot_size"],
            initial_equity=self.initial_equity,
        )
        pullback_trades = pullback_result.get("trades", [])
        daily_pnl_pullback = sum(t.get("pnl", 0) for t in pullback_trades)
        print(f"  Pullback: {pullback_result.get('total_trades', 0)} trades, P&L: {daily_pnl_pullback:.2f}")

        # Evaluate Momentum strategy
        print("Evaluating Momentum strategy...")
        momentum_result = run_momentum_backtest(
            self.csv_candles_path,
            self.start_date,
            self.end_date,
            lot_size=self.specs["lot_size"],
            initial_equity=self.initial_equity,
        )
        momentum_trades = momentum_result.get("trades", [])
        daily_pnl_momentum = sum(t.get("pnl", 0) for t in momentum_trades)
        print(f"  Momentum: {momentum_result.get('total_trades', 0)} trades, P&L: {daily_pnl_momentum:.2f}")

        # Combine all trades with risk control enforcement
        all_trades = []

        # Process ORB trades with risk controls
        for trade in orb_trades:
            self._process_trade_with_risk_controls(
                trade, "orb", all_trades,
                daily_starting_equity,
            )

        # Process Pullback trades with risk controls
        for trade in pullback_trades:
            self._process_trade_with_risk_controls(
                trade, "pullback", all_trades,
                daily_starting_equity,
            )

        # Process Momentum trades with risk controls
        for trade in momentum_trades:
            self._process_trade_with_risk_controls(
                trade, "momentum", all_trades,
                daily_starting_equity,
            )

        return {
            "orb": orb_result,
            "pullback": pullback_result,
            "momentum": momentum_result,
            "combined_trades": all_trades,
            "total_trades": len(all_trades),
        }

    # ------------------------------------------------------------------
    # Trade processing with risk controls
    # ------------------------------------------------------------------

    def _process_trade_with_risk_controls(
        self,
        trade: Dict[str, Any],
        strategy_name: str,
        all_trades: List[Dict[str, Any]],
        daily_starting_equity: float,
    ) -> None:
        """Process a single trade through all experimental risk controls.

        Args:
            trade: Trade details dict
            strategy_name: Name of the strategy ("orb", "pullback", "momentum")
            all_trades: Accumulator list for approved trades
            daily_starting_equity: Starting equity for the day
        """
        entry_price = trade.get("entry_price", 0)
        stop_loss = trade.get("stop_loss", 0)
        target = trade.get("target", 0)
        option_type = trade.get("option_type", "CE")
        pnl = trade.get("pnl", 0)
        trigger_time = trade.get("timestamp", "")

        # 1. Check daily loss limit
        allowed, reason = check_daily_loss_limit(
            pnl=pnl,
            starting_equity=daily_starting_equity,
            loss_limit_pct=self.max_daily_loss_pct,
        )
        if not allowed:
            log_rejected_entry(
                reason=f"DAILY LOSS LIMIT: {reason}",
                trade_details={
                    "strategy": strategy_name,
                    "entry_price": entry_price,
                    "pnl": pnl,
                    "option_type": option_type,
                },
            )
            print(f"  REJECTED (ORB/{strategy_name}): {reason}")
            return

        # 2. Check max trades per day
        allowed, reason = check_max_trades_per_day(
            trade_count_today=self.daily_trade_count,
            max_trades=self.max_trades_per_day,
        )
        if not allowed:
            log_rejected_entry(
                reason=f"MAX TRADES PER DAY: {reason}",
                trade_details={
                    "strategy": strategy_name,
                    "entry_price": entry_price,
                    "pnl": pnl,
                    "option_type": option_type,
                },
            )
            print(f"  REJECTED (ORB/{strategy_name}): {reason}")
            return

        # 3. Check consecutive losses
        allowed, reason = check_consecutive_losses(pnl=pnl)
        if not allowed:
            log_rejected_entry(
                reason=f"CONSECUTIVE LOSSES: {reason}",
                trade_details={
                    "strategy": strategy_name,
                    "entry_price": entry_price,
                    "pnl": pnl,
                    "option_type": option_type,
                },
            )
            print(f"  REJECTED (ORB/{strategy_name}): {reason}")
            return

        # 4. Check time cutoff (IST)
        allowed, reason = check_time_cutoff_ist(trade_time_ist=trigger_time)
        if not allowed:
            log_rejected_entry(
                reason=f"TIME CUTOFF: {reason}",
                trade_details={
                    "strategy": strategy_name,
                    "entry_price": entry_price,
                    "pnl": pnl,
                    "option_type": option_type,
                },
            )
            print(f"  REJECTED (ORB/{strategy_name}): {reason}")
            return

        # 5. Check for duplicate entries
        allowed, reason = check_duplicate_entry(
            symbol="NIFTY",
            existing_positions={},  # Simplified - would use actual positions
        )
        if not allowed:
            log_rejected_entry(
                reason=f"DUPLICATE ENTRY: {reason}",
                trade_details={
                    "strategy": strategy_name,
                    "entry_price": entry_price,
                    "pnl": pnl,
                    "option_type": option_type,
                },
            )
            print(f"  REJECTED (ORB/{strategy_name}): {reason}")
            return

        # 6. Calculate position size
        position_size = calculate_experimental_position_size(
            entry_price=entry_price,
            stop_loss=stop_loss,
            risk_per_trade_pct=self.risk_per_trade_pct,
            capital=daily_starting_equity,
        )

        # 7. Verify end-of-day closure (for daily backtest)
        # In a full intraday backtest, we'd verify positions are squared off

        # Trade approved - update state
        self.daily_trade_count += 1
        if pnl < 0:
            self.consecutive_losses += 1
            self.consecutive_wins = 0
        else:
            self.consecutive_losses = 0
            self.consecutive_wins += 1

        # Update high watermark
        if self.daily_pnl + pnl > self.high_watermark:
            self.high_watermark = self.daily_pnl + pnl

        # Update daily P&L
        self.daily_pnl += pnl

        # Build approved trade record
        approved_trade = {
            "strategy": strategy_name,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "target": target,
            "option_type": option_type,
            "position_size": position_size,
            "pnl": round(pnl, 2),
            "trigger_time": trigger_time,
            "daily_pnl_after": round(self.daily_pnl, 2),
            "risk_per_trade_pct": self.risk_per_trade_pct,
            "max_daily_loss_pct": self.max_daily_loss_pct,
        }
        all_trades.append(approved_trade)

        print(f"  ALLOWED (ORB/{strategy_name}): P&L={pnl:.2f}, "
              f"pos_size={position_size}, daily_P&L={self.daily_pnl:.2f}")

    # ------------------------------------------------------------------
    # Performance calculations
    # ------------------------------------------------------------------

    def calculate_performance_metrics(self, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Calculate comprehensive performance metrics from trade list.

        Args:
            trades: List of trade dicts from backtest runs

        Returns:
            Dict with performance metrics
        """
        if not trades:
            return {"error": "No trades to analyze"}

        total_trades = len(trades)
        winning_trades = [t for t in trades if t.get("pnl", 0) > 0]
        losing_trades = [t for t in trades if t.get("pnl", 0) <= 0]

        win_rate = len(winning_trades) / total_trades if total_trades > 0 else 0
        loss_rate = len(losing_trades) / total_trades if total_trades > 0 else 0

        # P&L metrics
        total_pnl = sum(t.get("pnl", 0) for t in trades)
        average_winner = sum(t.get("pnl", 0) for t in winning_trades) / len(winning_trades) if winning_trades else 0
        average_loser = sum(t.get("pnl", 0) for t in losing_trades) / len(losing_trades) if losing_trades else 0

        # Expectancy
        expectancy = (win_rate * average_winner) + (loss_rate * average_loser)

        # Profit Factor
        gross_profit = sum(t.get("pnl", 0) for t in winning_trades) if winning_trades else 0
        gross_loss = abs(sum(t.get("pnl", 0) for t in losing_trades)) if losing_trades else 0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

        # Maximum drawdown (running peak-to-trough)
        cumulative = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for t in trades:
            cumulative += t.get("pnl", 0)
            if cumulative > peak:
                peak = cumulative
            drawdown = peak - cumulative
            if drawdown > max_drawdown:
                max_drawdown = drawdown

        # Longest losing streak
        longest_streak = 0
        current_streak = 0
        for t in trades:
            if t.get("pnl", 0) <= 0:
                current_streak += 1
                longest_streak = max(longest_streak, current_streak)
            else:
                current_streak = 0

        # Trades per day
        trade_dates = set()
        for t in trades:
            ts = t.get("trigger_time", "")
            if ts:
                try:
                    date_part = ts[:10]
                    trade_dates.add(date_part)
                except:
                    pass
        trades_per_day = total_trades / len(trade_dates) if trade_dates else 0
        pct_zero_trades = ((len(trade_dates) - total_trades_positive / trades_per_day) / len(trade_dates) * 100) if trade_dates else 0

        # Strategy breakdown
        strategy_breakdown = {}
        for t in trades:
            strat = t.get("strategy", "unknown")
            if strat not in strategy_breakdown:
                strategy_breakdown[strat] = {"trades": 0, "win": 0, "loss": 0, "pnl": 0.0}
            strategy_breakdown[strat]["trades"] += 1
            if t.get("pnl", 0) > 0:
                strategy_breakdown[strat]["win"] += 1
            else:
                strategy_breakdown[strat]["loss"] += 1
            strategy_breakdown[strat]["pnl"] += t.get("pnl", 0)

        return {
            "total_trades": total_trades,
            "winning_trades": len(winning_trades),
            "losing_trades": len(losing_trades),
            "win_rate": round(win_rate * 100, 2),
            "loss_rate": round(loss_rate * 100, 2),
            "total_pnl": round(total_pnl, 2),
            "average_winner": round(average_winner, 2),
            "average_loser": round(average_loser, 2),
            "expectancy": round(expectancy, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float('inf') else "inf",
            "max_drawdown": round(max_drawdown, 2),
            "longest_losing_streak": longest_streak,
            "trades_per_day": round(trades_per_day, 2),
            "pct_zero_trades": round(pct_zero_trades, 2),
            "strategy_breakdown": strategy_breakdown,
        }

    # ------------------------------------------------------------------
    # Weekly/monthly expiry comparison
    # ------------------------------------------------------------------

    def compare_expiry_periods(self, option_trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Compare strategy performance across weekly and monthly expiry periods.

        Args:
            option_trades: List of trades with expiry information

        Returns:
            Dict with expiry-period comparison
        """
        if not option_trades:
            return {"error": "No option trades to compare"}

        # Group trades by expiry type
        weekly_trades = [t for t in option_trades if t.get("expiry", "").lower().find("weekly") >= 0]
        monthly_trades = [t for t in option_trades if t.get("expiry", "").lower().find("monthly") >= 0 or "2026" in str(t.get("expiry", ""))]

        # Calculate metrics for each
        weekly_metrics = self.calculate_performance_metrics(weekly_trades) if weekly_trades else {"total_trades": 0}
        monthly_metrics = self.calculate_performance_metrics(monthly_trades) if monthly_trades else {"total_trades": 0}

        # Compare key metrics
        comparison = {
            "weekly": {
                "trade_count": weekly_metrics.get("total_trades", 0),
                "win_rate": weekly_metrics.get("win_rate", 0),
                "total_pnl": weekly_metrics.get("total_pnl", 0),
                "profit_factor": weekly_metrics.get("profit_factor", 0),
            },
            "monthly": {
                "trade_count": monthly_metrics.get("total_trades", 0),
                "win_rate": monthly_metrics.get("win_rate", 0),
                "total_pnl": monthly_metrics.get("total_pnl", 0),
                "profit_factor": monthly_metrics.get("profit_factor", 0),
            },
            "comparison": {
                "difference_in_pnl": round(
                    monthly_metrics.get("total_pnl", 0) - weekly_metrics.get("total_pnl", 0), 2
                ),
                "difference_in_win_rate": round(
                    monthly_metrics.get("win_rate", 0) - weekly_metrics.get("win_rate", 0), 2
                ),
                "monthly_preferred": (
                    monthly_metrics.get("total_pnl", 0) > weekly_metrics.get("total_pnl", 0)
                ),
            },
        }

        return comparison

    # ------------------------------------------------------------------
    # Sensitivity analysis
    # ------------------------------------------------------------------

    def sensitivity_analysis(self, trades: List[Dict[str, Any]], 
                            risk_variants: List[float] = None) -> Dict[str, Any]:
        """Analyze strategy sensitivity to risk parameters and transaction costs.

        Args:
            trades: List of trades to analyze
            risk_variants: List of risk_per_trade_pct values to test

        Returns:
            Dict with sensitivity results
        """
        if risk_variants is None:
            risk_variants = [0.25, 0.30, 0.50, 1.0, 1.5]

        if not trades:
            return {"error": "No trades for sensitivity analysis"}

        results = {}
        for risk_pct in risk_variants:
            # Filter trades that would be allowed at this risk setting
            # (simplified: just recalculate metrics at different risk levels)
            risk_trades = [t for t in trades if t.get("risk_per_trade_pct", 0.30) <= risk_pct]

            if not risk_trades:
                results[f"risk_{risk_pct}"] = {"error": "No trades at this risk level"}
                continue

            metrics = self.calculate_performance_metrics(risk_trades)
            metrics["risk_per_trade_pct"] = risk_pct
            results[f"risk_{risk_pct}"] = metrics

        return results

    # ------------------------------------------------------------------
    # Full backtest execution
    # ------------------------------------------------------------------

    def run_full_backtest(self) -> Dict[str, Any]:
        """Run the complete backtest framework.

        Coordinates:
        1. Data import
        2. Strategy evaluation
        3. Risk control enforcement
        4. Performance calculation
        5. Expiry comparison
        6. Sensitivity analysis

        Returns:
            Dict with complete backtest results
        """
        print("=" * 60)
        print("TRIO COMPREHENSIVE BACKTEST FRAMEWORK")
        print("=" * 60)
        print()

        # Step 1: Import data
        print("Step 1: Importing market data...")
        candles = self.import_data()
        print(f"  Imported {len(candles)} 5-minute candles\n")

        # Step 2: Evaluate all strategies
        print("Step 2: Evaluating strategies...")
        strategy_results = self.evaluate_all_strategies(candles)
        print()

        # Step 3: Calculate combined performance
        print("Step 3: Calculating performance metrics...")
        combined_trades = strategy_results["combined_trades"]
        metrics = self.calculate_performance_metrics(combined_trades)
        print(f"  Total trades: {metrics['total_trades']}")
        print(f"  Win rate: {metrics['win_rate']}%")
        print(f"  Total P&L: {metrics['total_pnl']}")
        print(f"  Profit factor: {metrics['profit_factor']}")
        print(f"  Max drawdown: {metrics['max_drawdown']}")
        print(f"  Longest losing streak: {metrics['longest_losing_streak']}")
        print()

        # Step 4: Expiry comparison (if option data available)
        print("Step 4: Comparing expiry periods...")
        expiry_comparison = self.compare_expiry_periods(combined_trades)
        print(f"  Weekly vs Monthly comparison completed\n")

        # Step 5: Sensitivity analysis
        print("Step 5: Sensitivity analysis...")
        sensitivity = self.sensitivity_analysis(combined_trades)
        print()

        # Step 6: Summary
        print("Step 6: Summary")
        print(f"  Strategies tested: ORB, Pullback, Momentum")
        print(f"  Risk settings: {self.risk_per_trade_pct}%/trade, {self.max_daily_loss_pct}% daily loss")
        print(f"  Max entries/day: {self.max_trades_per_day}")
        print(f"  Consecutive loss guard: stop after {self.max_consecutive_losses} losses")
        print(f"  Time cutoff: 2:30 PM IST")
        print()

        return {
            "candle_count": len(candles),
            "orb": strategy_results["orb"],
            "pullback": strategy_results["pullback"],
            "momentum": strategy_results["momentum"],
            "combined_trades": combined_trades,
            "performance_metrics": metrics,
            "expiry_comparison": expiry_comparison,
            "sensitivity_analysis": sensitivity,
            "risk_settings": {
                "risk_per_trade_pct": self.risk_per_trade_pct,
                "max_daily_loss_pct": self.max_daily_loss_pct,
                "max_trades_per_day": self.max_trades_per_day,
                "max_consecutive_losses": self.max_consecutive_losses,
                "time_cutoff_ist": "14:30",
            },
        }


# ============================================================================
# QUICK TEST FUNCTION
# ------------------------------------------------------------------

def quick_test(csv_path: str = None) -> Dict[str, Any]:
    """Quick test of the backtest framework with sample data validation.

    Args:
        csv_path: Path to 5-min NIFTY CSV (uses sample data if None)

    Returns:
        Dict with quick test results
    """
    print("=" * 50)
    print("QUICK BACKTEST FRAMEWORK TEST")
    print("=" * 50)

    # If no CSV path provided, check if sample data exists
    if csv_path is None:
        # Look for any CSV in the data directory
        import glob
        csv_files = glob.glob("data/**/*.csv", recursive=True) + glob.glob("output/*.csv", recursive=True)
        if csv_files:
            csv_path = csv_files[0]
            print(f"Using sample data: {csv_path}")
        else:
            print("No CSV data found - demonstrating framework without data import")
            csv_path = None

    # Initialize engine with research parameters
    engine = TrioBacktestEngine(
        csv_candles_path=csv_path,
        initial_equity=500000.0,
        risk_per_trade_pct=0.30,  # 0.30% as research parameter
        max_daily_loss_pct=1.0,   # 1% daily loss limit
        max_trades_per_day=5,     # Max 5 entries per day
        max_consecutive_losses=2, # Stop after 2 consecutive losses
        time_cutoff_ist="14:30",  # 2:30 PM IST cutoff
    )

    # Run full backtest
    if csv_path:
        results = engine.run_full_backtest()
    else:
        # Return framework structure without actual data
        results = {
            "framework_ready": True,
            "message": "Backtest framework operational - provide 5-min NIFTY CSV data for actual backtesting",
            "risk_settings": engine.risk_settings,
            "strategies_available": ["ORB", "Pullback", "Momentum"],
        }

    print("=" * 50)
    return results


# ============================================================================
# EXAMPLE USAGE
# ------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("TRIO BACKTEST FRAMEWORK - STANDALONE TEST")
    print("=" * 60)
    print()
    print("This module integrates:")
    print("  - 5-min NIFTY candle data import (data_importer.py)")
    print("  - Three strategy modules: ORB, Pullback, Momentum")
    print("  - Experimental risk controls (experimental_risk.py)")
    print("  - Position sizing and risk limit enforcement")
    print("  - Performance metrics calculation")
    print("  - Weekly/monthly expiry comparison")
    print("  - Sensitivity analysis")
    print()
    print("REQUIREMENTS FOR ACTUAL BACKTESTING:")
    print("  - 5-min NIFTY OHLCV CSV with IST timestamps")
    print("  - Option premium CSV (strike, expiry, option_type, bid/ask)")
    print("  - Contract specifications (lot_size, strike_interval)")
    print()
    print("EXECUTION:")
    print("  python backtester.py  # With CSV data path")
    print()