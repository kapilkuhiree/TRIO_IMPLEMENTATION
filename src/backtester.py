"""
TRIO — Backtester
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

Runs the trading strategy on historical data and reports key metrics:
win rate, profit factor, max drawdown, Sharpe ratio, and total return.

DISCLAIMER: Educational purposes only. Not financial advice.
Past performance does not indicate future results.
"""

import logging
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from src.utils import get_logger, load_config
from src.data_fetcher import fetch_market_data
from src.indicators import compute_indicators
from src.signal_engine import generate_signal
from src.risk_manager import (
    calculate_stop_loss,
    calculate_take_profit,
    calculate_position_size,
    reset_risk_state,
)

logger = get_logger("backtester")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class BacktestTrade:
    """Record of a single backtest trade.
    Author: Kapil Kuhire
    """
    entry_idx: int = 0
    exit_idx: int = 0
    action: str = ""           # BUY or SELL
    entry_price: float = 0.0
    exit_price: float = 0.0
    stop_loss: float = 0.0
    target: float = 0.0
    size: int = 0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    exit_reason: str = ""      # target_hit, stop_hit, signal_exit, end_of_data


@dataclass
class BacktestResult:
    """Aggregated backtest results.
    Author: Kapil Kuhire
    """
    symbol: str = ""
    timeframe: str = ""
    period: str = ""
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    total_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    avg_risk_reward: float = 0.0
    trades: List[BacktestTrade] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("trades", None)  # exclude individual trades from dict for brevity
        return d

    def summary_str(self) -> str:
        """Human-readable summary."""
        return (
            f"Backtest: {self.symbol} ({self.period})\n"
            f"  Trades:       {self.total_trades}\n"
            f"  Win Rate:     {self.win_rate:.1f}%\n"
            f"  Profit Factor:{self.profit_factor:.2f}\n"
            f"  Total Return: {self.total_return_pct:.2f}%\n"
            f"  Max Drawdown: {self.max_drawdown_pct:.2f}%\n"
            f"  Sharpe Ratio: {self.sharpe_ratio:.2f}\n"
        )


# ---------------------------------------------------------------------------
# Core backtest engine
# ---------------------------------------------------------------------------

def run_backtest(
    symbol: str,
    timeframe: str = "1d",
    period: str = "365d",
    initial_capital: Optional[float] = None,
    commission_pct: Optional[float] = None,
    slippage_pct: Optional[float] = None,
    indicator_lookback: int = 200,
) -> BacktestResult:
    """
    Run a backtest for a single symbol.

    Strategy: uses the signal engine on rolling windows of historical data.
    Entries on BUY/SELL signals, exits on stop-loss, take-profit, or opposing signal.

    Args:
        symbol:             Ticker symbol.
        timeframe:          Candle timeframe.
        period:             How far back to fetch data (e.g., 365d).
        initial_capital:    Starting capital.
        commission_pct:     Commission per trade (%).
        slippage_pct:       Slippage per trade (%).
        indicator_lookback: Minimum bars needed before first signal.

    Returns:
        BacktestResult with metrics and trade list.
    """
    cfg = load_config()
    bt_cfg = cfg.get("backtesting", {})

    if initial_capital is None:
        initial_capital = bt_cfg.get("initial_capital", 100000)
    if commission_pct is None:
        commission_pct = bt_cfg.get("commission_pct", 0.03)
    if slippage_pct is None:
        slippage_pct = bt_cfg.get("slippage_pct", 0.05)

    trail_enabled = cfg.get("risk_management", {}).get("trailing_stop", {}).get("enabled", False)

    reset_risk_state()

    # Fetch data
    logger.info("Fetching backtest data for %s (%s, %s)...", symbol, timeframe, period)
    market_data = fetch_market_data(symbol, timeframe, period)
    df = market_data.ohlcv

    if len(df) < indicator_lookback + 10:
        raise ValueError(
            f"Not enough data for backtest: {len(df)} bars "
            f"(need at least {indicator_lookback + 10})"
        )

    logger.info("Running backtest on %d bars...", len(df))

    # State
    capital = initial_capital
    equity_curve: List[float] = [capital]
    trades: List[BacktestTrade] = []
    in_trade = False
    current_trade: Optional[BacktestTrade] = None

    # Walk forward
    for i in range(indicator_lookback, len(df)):
        window = df.iloc[max(0, i - indicator_lookback):i + 1].copy()
        close = float(df["Close"].iloc[i])
        high = float(df["High"].iloc[i])
        low = float(df["Low"].iloc[i])

        # Trailing stop, only when enabled in config. It is disabled by default
        # because the out-of-sample test showed trailing exits lost money
        # (test profit factor 0.72) versus the fixed 1.5R target.
        if in_trade and current_trade is not None and trail_enabled:
            atr_val = None
            try:
                win_for_atr = df.iloc[max(0, i - 50):i + 1].copy()
                atr_calc = win_for_atr["High"].sub(win_for_atr["Low"]).rolling(14).mean().iloc[-1]
                atr_val = float(atr_calc) if atr_calc == atr_calc else None
            except Exception:
                atr_val = None

            if current_trade.action == "BUY":
                # update trailing stop upwards when price rises above entry
                if atr_val and close > current_trade.entry_price:
                    from src.risk_manager import calculate_trailing_stop
                    new_sl = calculate_trailing_stop(close, current_trade.entry_price, atr_val, "BUY")
                    if new_sl is not None and new_sl > current_trade.stop_loss:
                        current_trade.stop_loss = new_sl
            elif current_trade.action == "SELL":
                if atr_val and close < current_trade.entry_price:
                    from src.risk_manager import calculate_trailing_stop
                    new_sl = calculate_trailing_stop(close, current_trade.entry_price, atr_val, "SELL")
                    if new_sl is not None and new_sl < current_trade.stop_loss:
                        current_trade.stop_loss = new_sl

        # Check if current trade hits SL or TP
        if in_trade and current_trade is not None:
            if current_trade.action == "BUY":
                if low <= current_trade.stop_loss:
                    # Stop hit
                    exit_price = current_trade.stop_loss * (1 - slippage_pct / 100)
                    current_trade.exit_price = exit_price
                    current_trade.exit_idx = i
                    current_trade.exit_reason = "stop_hit"
                    pnl = (exit_price - current_trade.entry_price) * current_trade.size
                    pnl -= abs(pnl) * commission_pct / 100
                    current_trade.pnl = round(pnl, 2)
                    current_trade.pnl_pct = round(pnl / (current_trade.entry_price * current_trade.size) * 100, 2) if current_trade.size > 0 else 0.0
                    capital += pnl
                    trades.append(current_trade)
                    in_trade = False
                    current_trade = None
                elif high >= current_trade.target:
                    # Target hit
                    exit_price = current_trade.target * (1 - slippage_pct / 100)
                    current_trade.exit_price = exit_price
                    current_trade.exit_idx = i
                    current_trade.exit_reason = "target_hit"
                    pnl = (exit_price - current_trade.entry_price) * current_trade.size
                    pnl -= abs(pnl) * commission_pct / 100
                    current_trade.pnl = round(pnl, 2)
                    current_trade.pnl_pct = round(pnl / (current_trade.entry_price * current_trade.size) * 100, 2) if current_trade.size > 0 else 0.0
                    capital += pnl
                    trades.append(current_trade)
                    in_trade = False
                    current_trade = None

            elif current_trade.action == "SELL":
                if high >= current_trade.stop_loss:
                    exit_price = current_trade.stop_loss * (1 + slippage_pct / 100)
                    current_trade.exit_price = exit_price
                    current_trade.exit_idx = i
                    current_trade.exit_reason = "stop_hit"
                    pnl = (current_trade.entry_price - exit_price) * current_trade.size
                    pnl -= abs(pnl) * commission_pct / 100
                    current_trade.pnl = round(pnl, 2)
                    current_trade.pnl_pct = round(pnl / (current_trade.entry_price * current_trade.size) * 100, 2) if current_trade.size > 0 else 0.0
                    capital += pnl
                    trades.append(current_trade)
                    in_trade = False
                    current_trade = None
                elif low <= current_trade.target:
                    exit_price = current_trade.target * (1 + slippage_pct / 100)
                    current_trade.exit_price = exit_price
                    current_trade.exit_idx = i
                    current_trade.exit_reason = "target_hit"
                    pnl = (current_trade.entry_price - exit_price) * current_trade.size
                    pnl -= abs(pnl) * commission_pct / 100
                    current_trade.pnl = round(pnl, 2)
                    current_trade.pnl_pct = round(pnl / (current_trade.entry_price * current_trade.size) * 100, 2) if current_trade.size > 0 else 0.0
                    capital += pnl
                    trades.append(current_trade)
                    in_trade = False
                    current_trade = None

        equity_curve.append(capital)

        # Generate signal on rolling window (skip if already in trade)
        if not in_trade and i % 1 == 0:  # every bar (can be adjusted)
            try:
                readings = compute_indicators(window, symbol, timeframe)
                signal = generate_signal(
                    symbol=symbol,
                    latest_price=close,
                    technical_readings=readings,
                )

                if signal.action in ("BUY", "SELL"):
                    # Get ATR for stop-loss
                    atr_ind = None
                    for key, ind in readings.indicators.items():
                        if key.startswith("atr_"):
                            atr_ind = ind.value
                            break

                    # Use the swing level for stop placement so the backtest
                    # matches the live path (and the validated strategy).
                    swing = readings.swing_low if signal.action == "BUY" else readings.swing_high

                    sl = calculate_stop_loss(close, atr_ind, signal.action, swing_level=swing)
                    tp = calculate_take_profit(close, sl, signal.action)
                    size = calculate_position_size(close, sl, capital)

                    if size > 0:
                        entry_price = close * (1 + slippage_pct / 100) if signal.action == "BUY" else close * (1 - slippage_pct / 100)
                        current_trade = BacktestTrade(
                            entry_idx=i,
                            action=signal.action,
                            entry_price=round(entry_price, 2),
                            stop_loss=sl,
                            target=tp,
                            size=size,
                        )
                        in_trade = True
            except Exception as exc:
                continue  # skip bars where indicator computation fails

    # Close any open trade at end
    if in_trade and current_trade is not None:
        exit_price = float(df["Close"].iloc[-1])
        if current_trade.action == "BUY":
            pnl = (exit_price - current_trade.entry_price) * current_trade.size
        else:
            pnl = (current_trade.entry_price - exit_price) * current_trade.size
        pnl -= abs(pnl) * commission_pct / 100
        current_trade.exit_price = exit_price
        current_trade.exit_idx = len(df) - 1
        current_trade.exit_reason = "end_of_data"
        current_trade.pnl = round(pnl, 2)
        current_trade.pnl_pct = round(pnl / (current_trade.entry_price * current_trade.size) * 100, 2) if current_trade.size > 0 else 0.0
        capital += pnl
        trades.append(current_trade)

    # --- Compute metrics ---
    result = _compute_metrics(symbol, timeframe, period, initial_capital, capital, equity_curve, trades)

    logger.info("Backtest complete for %s: %s", symbol, result.summary_str())

    return result


def _compute_metrics(
    symbol: str,
    timeframe: str,
    period: str,
    initial_capital: float,
    final_capital: float,
    equity_curve: List[float],
    trades: List[BacktestTrade],
) -> BacktestResult:
    """Compute backtest metrics from the trade list and equity curve."""
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]

    total = len(trades)
    win_count = len(wins)
    loss_count = len(losses)
    win_rate = (win_count / total * 100) if total > 0 else 0.0

    gross_profit = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0

    total_return_pct = ((final_capital - initial_capital) / initial_capital * 100)

    # Max drawdown
    eq = np.array(equity_curve)
    peak = np.maximum.accumulate(eq)
    drawdown = (peak - eq) / peak * 100
    max_drawdown = float(np.max(drawdown)) if len(drawdown) > 0 else 0.0

    # Sharpe ratio (annualized, assuming daily returns)
    if len(equity_curve) > 1:
        returns = np.diff(equity_curve) / equity_curve[:-1]
        if np.std(returns) > 0:
            sharpe = np.mean(returns) / np.std(returns) * np.sqrt(252)
        else:
            sharpe = 0.0
    else:
        sharpe = 0.0

    # Average risk-reward
    rrs = []
    for t in trades:
        risk = abs(t.entry_price - t.stop_loss)
        if risk > 0:
            rr = abs(t.pnl / (risk * t.size))
            rrs.append(rr)
    avg_rr = float(np.mean(rrs)) if rrs else 0.0

    return BacktestResult(
        symbol=symbol,
        timeframe=timeframe,
        period=period,
        total_trades=total,
        wins=win_count,
        losses=loss_count,
        win_rate=round(win_rate, 1),
        profit_factor=round(profit_factor, 2),
        total_return_pct=round(total_return_pct, 2),
        max_drawdown_pct=round(max_drawdown, 2),
        sharpe_ratio=round(float(sharpe), 2),
        avg_risk_reward=round(avg_rr, 2),
        trades=trades,
    )
