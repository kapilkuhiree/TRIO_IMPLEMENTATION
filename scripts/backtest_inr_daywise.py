"""INR day-wise comparison: ORB vs mean-reversion vs QQQ RSI(3) rules.

All three sleeves use the same fixed notional (default Rs 10,00,000) and the
same round-trip cost assumption (default 5 bps per side), so the rupee P&L is
directly comparable day by day.

The three sleeves
-----------------
1. ``orb``          : the repo's OrbStrategy on 5m NIFTY index bars.
2. ``mean_reversion``: the repo's MeanReversionStrategy on 5m NIFTY index bars.
3. ``qqq_nifty``    : the QQQ daily rules (RSI(3) < 22, EMA(100) rising over 5
                      sessions, close <= 10-day VWAP * 0.99, next-open entry,
                      exit next open once RSI(3) > 55) applied to NIFTY daily.

Sleeves 1 and 2 are simulated on the **index** (spot), not on options, because
a fixed rupee notional is only meaningful for a linear instrument.  The repo's
live ORB/MR books buy options, so option-premium P&L will differ; this script
answers "same rupee notional, same underlying, who wins day by day".

A fourth reference column, ``qqq_native``, keeps the actual QQQ strategy in USD
and converts each trade's P&L to INR at the entry/exit USDINR rate.  It is a
different underlying and is shown for completeness, not as a like-for-like
comparison.

Data
----
* 5m NIFTY: ``output/orb_retest_5m_NSEI_60d.pkl`` (60 sessions, yfinance cap).
* NIFTY daily: yfinance ``^NSEI`` from 2007 (for the long-run QQQ-on-NIFTY run).
* QQQ daily: ``output/qqq_daily.csv`` (from the QQQ research script).
* USDINR: yfinance ``USDINR=X`` daily.

Usage
-----
    python scripts/backtest_inr_daywise.py
    python scripts/backtest_inr_daywise.py --notional 10000000 --cost-bps 3
    python scripts/backtest_inr_daywise.py --json out.json --html report.html

DISCLAIMER: Educational purposes only. Not financial advice.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime as _dt
from datetime import time as dtime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import src.options_live_scan as scan  # noqa: E402
import scripts.backtest_orb_retest_v2 as repo_bt  # noqa: E402
from scripts.backtest_qqq_mean_reversion import (  # noqa: E402
    build_signals as qqq_build_signals,
    load_qqq,
    run_backtest as qqq_run_backtest,
)
from strategies.mean_reversion_strategy import MeanReversionStrategy  # noqa: E402
from strategies.orb_strategy import OrbStrategy  # noqa: E402

IST_OFFSET = pd.Timedelta(hours=5, minutes=30)
HARD_FLAT = dtime(15, 10)
DEFAULT_NOTIONAL = 1_000_000.0
DEFAULT_COST_BPS = 5.0
DEFAULT_DAYS = 60

FIVE_MIN_CACHE = ROOT / "output" / "orb_retest_5m_NSEI_{days}d.pkl"
DEFAULT_JSON = ROOT / "output" / "inr_three_way_summary.json"
DEFAULT_DAYWISE = ROOT / "output" / "inr_daywise_comparison.csv"
DEFAULT_HTML = ROOT / "output" / "inr_three_way_report.html"
QQQ_TRADES = ROOT / "output" / "qqq_mean_reversion_trades.csv"


# ---------------------------------------------------------------------------
# Index VWAP fallback (NSE index feeds carry Volume = 0)
# ---------------------------------------------------------------------------

def _vwap_with_fallback(candles: Sequence[Dict[str, Any]]) -> Optional[float]:
    """Equal-weighted typical price when the feed has no volume."""
    if not candles:
        return None
    dates: List[Optional[Any]] = []
    for row in candles:
        try:
            dates.append(_dt.fromisoformat(
                str(row.get("timestamp")).replace("Z", "+00:00")).date())
        except (TypeError, ValueError):
            dates.append(None)
    known = [value for value in dates if value is not None]
    if not known:
        return None
    last = max(known)
    pv = vol_sum = 0.0
    for row, day in zip(candles, dates):
        if day != last:
            continue
        try:
            high = float(row.get("high"))
            low = float(row.get("low"))
            close = float(row.get("close"))
            volume = float(row.get("volume") or 0.0)
        except (TypeError, ValueError):
            continue
        if close <= 0:
            continue
        typical = (high + low + close) / 3.0
        if volume <= 0:
            pv += typical
            vol_sum += 1.0
        else:
            pv += typical * volume
            vol_sum += volume
    return pv / vol_sum if vol_sum > 0 else None


scan._session_vwap = _vwap_with_fallback  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _flatten(frame: pd.DataFrame) -> pd.DataFrame:
    if isinstance(frame.columns, pd.MultiIndex):
        frame = frame.copy()
        frame.columns = [str(column[0]) for column in frame.columns]
    return frame


def load_5m(days: int = DEFAULT_DAYS) -> pd.DataFrame:
    cache = Path(str(FIVE_MIN_CACHE).format(days=days))
    if not cache.exists():
        raise FileNotFoundError(
            f"Missing {cache}. Run scripts/backtest_orb_retest_v2.py first to "
            f"cache {days} sessions of 5m NIFTY data.")
    frame = pd.read_pickle(cache)
    frame = _flatten(frame)
    frame.index = pd.to_datetime(frame.index)
    if frame.index.tz is None:
        frame.index = frame.index.tz_localize("UTC")
    frame.index = frame.index.tz_convert("Asia/Kolkata")
    frame = frame[(frame.index.time >= dtime(9, 15)) & (frame.index.time <= dtime(15, 30))]
    return frame.dropna(subset=["Open", "High", "Low", "Close"])


def load_nifty_daily(start: str = "2007-01-01", end: str = "2026-11-14") -> pd.DataFrame:
    import yfinance as yf

    raw = yf.download("^NSEI", start=start, end=end, interval="1d",
                      auto_adjust=False, progress=False, threads=False)
    raw = _flatten(raw)
    raw = raw.rename_axis("Date")
    bars = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
    bars.index = pd.to_datetime(bars.index).tz_localize(None)
    bars = bars.dropna(subset=["Open", "High", "Low", "Close"])
    # NIFTY index volume is zero/unreliable; Volume = 1 turns the rolling VWAP
    # into an equal-weighted typical-price average instead of 0/0.
    bars["Volume"] = 1.0
    return bars


def load_usdinr(start: str = "1999-01-01", end: str = "2026-11-14") -> pd.Series:
    import yfinance as yf

    raw = yf.download("USDINR=X", start=start, end=end, interval="1d",
                      auto_adjust=False, progress=False, threads=False)
    raw = _flatten(raw)
    close = raw["Close"] if "Close" in raw.columns else raw.iloc[:, 0]
    close.index = pd.to_datetime(close.index).tz_localize(None)
    close = close.dropna()
    return close.sort_index()


def to_candles(frame: pd.DataFrame, name: str = "NIFTY") -> List[Dict[str, Any]]:
    return [{
        "timestamp": ts.isoformat(),
        "open": float(row["Open"]),
        "high": float(row["High"]),
        "low": float(row["Low"]),
        "close": float(row["Close"]),
        "volume": float(row.get("Volume") or 0.0),
        "underlying_symbol": name,
        "exchange": "NSE",
        "instrument_type": "index",
    } for ts, row in frame.iterrows()]


# ---------------------------------------------------------------------------
# Signal collection (5m sleeves)
# ---------------------------------------------------------------------------

Signal = Tuple[str, float, float]  # option_type/side, stop, target


def collect_orb_signals(candles: Sequence[Dict[str, Any]],
                        lot: int = 65, step: int = 50) -> Dict[int, Signal]:
    strategy = OrbStrategy(lot_size=lot, orb_confirm_closes=2)
    specs = {"lot_size": lot, "strike_step": step, "expiry_preference": "weekly"}
    signals: Dict[int, Signal] = {}
    for index in range(3, len(candles)):
        try:
            result = strategy.evaluate_signal(candles, index, specs, "NIFTY")
        except Exception:
            continue
        if getattr(result, "allowed", False):
            kind = str(getattr(result, "option_type", "") or "").upper()
            if kind in {"CE", "PE"}:
                signals[index] = (
                    kind,
                    float(getattr(result, "stop_loss", 0.0) or 0.0),
                    float(getattr(result, "target", 0.0) or 0.0),
                )
    return signals


def collect_mr_signals(candles: Sequence[Dict[str, Any]]) -> Dict[int, Signal]:
    strategy = MeanReversionStrategy()
    signals: Dict[int, Signal] = {}
    for index in range(len(candles)):
        try:
            result = strategy.evaluate_signal(candles, index)
        except Exception:
            continue
        if getattr(result, "allowed", False):
            kind = str(getattr(result, "option_type", "") or "").upper()
            if kind in {"CE", "PE"}:
                signals[index] = (
                    kind,
                    float(getattr(result, "stop_loss", 0.0) or 0.0),
                    float(getattr(result, "target", 0.0) or 0.0),
                )
    return signals


# ---------------------------------------------------------------------------
# Spot execution
# ---------------------------------------------------------------------------

@dataclass
class SpotTrade:
    strategy: str
    day: str
    entry_ts: str
    exit_ts: str
    side: str
    entry_price: float
    exit_price: float
    qty: float
    return_pct: float
    pnl: float
    exit_reason: str


def simulate_spot(candles: Sequence[Dict[str, Any]], signals: Dict[int, Signal], *,
                  strategy: str, notional: float, cost_bps: float,
                  max_trades_per_day: int = 5,
                  earliest_min: int = 30,
                  entry_cutoff_min: int = 315) -> List[SpotTrade]:
    """One open position at a time, next-bar entry, stop/target/EOD exit.

    ``earliest_min`` / ``entry_cutoff_min`` are minutes after the 09:15 open and
    mirror the repo's live ORB gate (no entries before 09:45 or after 14:30).

    When a bar straddles both the stop and the target, the stop wins.  That is
    the pessimistic assumption and keeps the comparison honest.
    """
    cost = max(0.0, float(cost_bps)) / 10_000.0
    timestamps = [pd.Timestamp(bar["timestamp"]) for bar in candles]
    days = [stamp.date().isoformat() for stamp in timestamps]
    last_bar_of_day: Dict[str, int] = {}
    for index, day in enumerate(days):
        last_bar_of_day[day] = index

    def minute_of_day(stamp: pd.Timestamp) -> int:
        return stamp.hour * 60 + stamp.minute - (9 * 60 + 15)

    trades: List[SpotTrade] = []
    position: Optional[Dict[str, Any]] = None
    per_day: Dict[str, int] = defaultdict(int)

    def close_position(exit_index: int, price: float, reason: str) -> None:
        nonlocal position
        if position is None or price <= 0:
            return
        entry = position["entry_price"]
        if position["side"] == "long":
            gross_return = price / entry - 1.0
        else:
            gross_return = 1.0 - price / entry
        exit_notional = notional * (1.0 + gross_return)
        charges = notional * cost + abs(exit_notional) * cost
        pnl = notional * gross_return - charges
        trades.append(SpotTrade(
            strategy=strategy,
            day=position["day"],
            entry_ts=position["entry_ts"],
            exit_ts=timestamps[exit_index].isoformat(),
            side=position["side"],
            entry_price=round(entry, 2),
            exit_price=round(price, 2),
            qty=round(position["qty"], 4),
            return_pct=round((pnl / notional) * 100.0, 6),
            pnl=round(pnl, 2),
            exit_reason=reason,
        ))
        position = None

    for index, bar in enumerate(candles):
        day = days[index]
        stamp = timestamps[index]

        # 1. Fill yesterday's signal at this bar's open (same session only).
        if position is None and (index - 1) in signals and days[index - 1] == day:
            entry_minute = minute_of_day(stamp)
            if (per_day[day] < max_trades_per_day
                    and earliest_min <= entry_minute <= entry_cutoff_min):
                kind, stop, target = signals[index - 1]
                entry = float(bar["open"])
                # Do not chase a move that already ran through the target or
                # the stop between the signal close and the next open.
                if entry > 0:
                    if kind == "CE":
                        beyond = ((target > 0 and entry >= target)
                                  or (stop > 0 and entry <= stop))
                    else:
                        beyond = ((target > 0 and entry <= target)
                                  or (stop > 0 and entry >= stop))
                else:
                    beyond = True
                if not beyond:
                    position = {
                        "day": day,
                        "entry_ts": stamp.isoformat(),
                        "side": "long" if kind == "CE" else "short",
                        "entry_price": entry,
                        "qty": notional / entry,
                        "stop": stop if stop > 0 else None,
                        "target": target if target > 0 else None,
                    }
                    per_day[day] += 1

        # 2. Exits on this bar, including the entry bar itself.
        if position is not None:
            open_ = float(bar["open"])
            high = float(bar["high"])
            low = float(bar["low"])
            close = float(bar["close"])
            stop = position["stop"]
            target = position["target"]
            reason: Optional[str] = None
            price = close

            if position["side"] == "long":
                if stop is not None and low <= stop:
                    reason, price = "stop", min(open_, stop)
                elif target is not None and high >= target:
                    reason, price = "target", max(open_, target)
            else:
                if stop is not None and high >= stop:
                    reason, price = "stop", max(open_, stop)
                elif target is not None and low <= target:
                    reason, price = "target", min(open_, target)

            if reason is None and (stamp.time() >= HARD_FLAT
                                   or index == last_bar_of_day[day]):
                reason, price = "eod", close
            if reason is not None:
                close_position(index, price, reason)

    return trades


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def option_trade_objs(trades: Sequence[Dict[str, Any]],
                      notional: float) -> List[SimpleNamespace]:
    """Normalise repo option trades to the common metrics shape."""
    out: List[SimpleNamespace] = []
    for trade in trades:
        pnl = float(trade["pnl"])
        out.append(SimpleNamespace(
            pnl=pnl,
            return_pct=pnl / notional * 100.0 if notional else 0.0,
            exit_date=str(trade.get("exit_ts", ""))[:10],
            day=str(trade.get("day", "")),
            bars_held=0,
        ))
    return out


def run_option_sleeve(candles: Sequence[Dict[str, Any]],
                      signals: Dict[int, str], *, strategy: str,
                      meta: Dict[str, Any], earliest_min: int = 15,
                      cooldown: int = 0,
                      cooldown_rule: str = "any") -> List[Dict[str, Any]]:
    """Run the repo's real option execution (BS pricing, premium stops)."""
    by_date: Dict[str, List[int]] = defaultdict(list)
    for index, candle in enumerate(candles):
        by_date[candle["timestamp"][:10]].append(index)
    return repo_bt.run_variant(
        candles, by_date, signals, meta, strategy=strategy,
        earliest_min=earliest_min, cooldown=cooldown,
        cooldown_rule=cooldown_rule)


def metrics(trades: Sequence[Any], notional: float) -> Dict[str, Any]:
    pnls = np.array([float(trade.pnl) for trade in trades], dtype=float)
    returns = np.array([float(trade.return_pct) for trade in trades], dtype=float)
    count = len(pnls)
    if count == 0:
        return {
            "trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "gross_profit": 0.0, "gross_loss": 0.0, "profit_factor": 0.0,
            "avg_trade_return_pct": 0.0, "net_pnl": 0.0, "max_drawdown": 0.0,
            "max_drawdown_pct": 0.0, "avg_bars_held": 0.0,
        }
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    gross_profit = float(wins.sum())
    gross_loss = float(-losses.sum())
    equity = np.concatenate(([notional], notional + np.cumsum(pnls)))
    peaks = np.maximum.accumulate(equity)
    drawdowns = equity - peaks
    max_drawdown = float(drawdowns.min())
    return {
        "trades": count,
        "wins": int(len(wins)),
        "losses": int(len(losses)),
        "win_rate": round(len(wins) / count * 100.0, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(max(gross_loss, 0.0), 2),
        "profit_factor": (round(gross_profit / gross_loss, 4)
                          if gross_loss else float("inf")),
        "avg_trade_return_pct": round(float(returns.mean()), 4),
        "net_pnl": round(float(pnls.sum()), 2),
        "max_drawdown": round(max_drawdown, 2),
        "max_drawdown_pct": round(abs(max_drawdown) / notional * 100.0, 2),
        "avg_bars_held": round(float(np.mean([
            getattr(trade, "bars_held", 1) for trade in trades])), 2),
    }


def trade_exit_day(trade: Any) -> str:
    return str(getattr(trade, "exit_date", "") or getattr(trade, "day", ""))[:10]


def daily_pnl(trades: Sequence[Any]) -> Dict[str, float]:
    """Realised P&L booked on the exit date."""
    out: Dict[str, float] = defaultdict(float)
    for trade in trades:
        day = trade_exit_day(trade)
        if day:
            out[day] += float(trade.pnl)
    return dict(out)


def in_window(trades: Sequence[Any], days: Sequence[str]) -> List[Any]:
    allowed = set(days)
    return [trade for trade in trades if trade_exit_day(trade) in allowed]


# ---------------------------------------------------------------------------
# QQQ sleeves
# ---------------------------------------------------------------------------

def run_qqq_on_nifty(bars: pd.DataFrame, *, notional: float,
                     cost_bps: float) -> Tuple[List[Any], pd.DataFrame]:
    signals = qqq_build_signals(bars)
    trades = qqq_run_backtest(bars, signals, exit_timing="next_open",
                              cost_bps=cost_bps, capital=notional)
    return trades, signals


def run_qqq_native_inr(*, notional: float, cost_bps: float,
                       trades_path: Path = QQQ_TRADES) -> Tuple[List[Dict[str, Any]],
                                                               Dict[str, float]]:
    """Convert the USD QQQ trade ledger to INR at entry/exit FX."""
    if not trades_path.exists():
        return [], {}
    frame = pd.read_csv(trades_path, parse_dates=["signal_date", "entry_date", "exit_date"])
    fx = load_usdinr()
    cost = max(0.0, float(cost_bps)) / 10_000.0

    def rate(day: pd.Timestamp) -> float:
        try:
            value = float(fx.asof(day))
        except (KeyError, TypeError, ValueError):
            value = float("nan")
        if not math.isfinite(value) or value <= 0:
            # USDINR history starts in 2003; backfill the earliest rate so the
            # pre-2003 QQQ trades are not silently dropped.
            value = float(fx.iloc[0])
        return value

    trades: List[Dict[str, Any]] = []
    for row in frame.itertuples():
        entry_fx = rate(row.entry_date)
        exit_fx = rate(row.exit_date)
        if not math.isfinite(entry_fx) or entry_fx <= 0:
            continue
        usd_notional = notional / entry_fx
        net_return = ((row.exit_price * (1.0 - cost))
                      / (row.entry_price * (1.0 + cost)) - 1.0)
        pnl_inr = usd_notional * net_return * exit_fx
        trades.append({
            "strategy": "qqq_native",
            "day": row.entry_date.date().isoformat(),
            "exit_date": row.exit_date.date().isoformat(),
            "return_pct": round(net_return * 100.0, 6),
            "pnl": round(pnl_inr, 2),
            "fx_entry": round(entry_fx, 4),
            "fx_exit": round(exit_fx, 4),
        })
    return trades, daily_pnl_dict(trades)


def daily_pnl_dict(trades: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    out: Dict[str, float] = defaultdict(float)
    for trade in trades:
        out[str(trade["exit_date"])[:10]] += float(trade["pnl"])
    return dict(out)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def json_safe(value: Any) -> Any:
    if isinstance(value, float) and (math.isinf(value) or math.isnan(value)):
        return None
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def _money(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}Rs {abs(value):,.0f}"


def format_summary(rows: Dict[str, Dict[str, Any]]) -> str:
    header = (f"{'Strategy':<22}{'Trades':>7}{'Win%':>8}{'PF':>7}"
              f"{'Avg%':>8}{'Net Rs':>15}{'MaxDD Rs':>15}{'DD%':>7}")
    lines = [header, "-" * len(header)]
    for name, stats in rows.items():
        pf = "inf" if stats["profit_factor"] == float("inf") else f"{stats['profit_factor']:.2f}"
        lines.append(
            f"{name:<22}{stats['trades']:>7}{stats['win_rate']:>7.1f}%"
            f"{pf:>7}{stats['avg_trade_return_pct']:>8.2f}"
            f"{stats['net_pnl']:>15,.0f}{stats['max_drawdown']:>15,.0f}"
            f"{stats['max_drawdown_pct']:>6.1f}%")
    return "\n".join(lines)


def build_daywise(*, days: Sequence[str], series: Dict[str, Dict[str, float]],
                  notional: float) -> pd.DataFrame:
    frame = pd.DataFrame(index=pd.Index(days, name="date"))
    for name, mapping in series.items():
        frame[name] = [mapping.get(day, 0.0) for day in days]
        frame[f"{name}_cum"] = notional + frame[name].cumsum()
    return frame


def format_daywise(frame: pd.DataFrame, names: Sequence[str]) -> str:
    labels = {"orb": "ORB", "mean_reversion": "MR",
              "qqq_nifty": "QQQ-NIFTY", "qqq_native": "QQQ-INR"}
    width = 14
    header = (f"{'Date':<12}" + "".join(
        f"{labels.get(name, name) + ' Rs':>{width}}" for name in names)
        + "".join(f"{labels.get(name, name) + ' cum':>{width}}" for name in names))
    lines = [header, "-" * len(header)]
    for day, row in frame.iterrows():
        cells = [f"{day:<12}"]
        for name in names:
            value = row[name]
            cells.append(f"{'0':>{width}}" if value == 0 else f"{value:>{width},.0f}")
        for name in names:
            cells.append(f"{row[f'{name}_cum']:>{width},.0f}")
        lines.append("".join(cells))
    return "\n".join(lines)


_SVG_COLORS = ["#2563eb", "#dc2626", "#059669", "#d97706"]


def _svg_equity(frame: pd.DataFrame, names: Sequence[str], notional: float) -> str:
    width, height, pad = 960, 320, 48
    values = [frame[f"{name}_cum"].to_numpy(dtype=float) for name in names]
    low = min(float(v.min()) for v in values)
    high = max(float(v.max()) for v in values)
    if high == low:
        high, low = high + 1.0, low - 1.0
    count = len(frame)
    plot_w, plot_h = width - 2 * pad, height - 2 * pad

    def x(index: int) -> float:
        return pad + (plot_w * index / max(count - 1, 1))

    def y(value: float) -> float:
        return pad + plot_h * (1.0 - (value - low) / (high - low))

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="Equity curves">',
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
        f'<text x="{pad}" y="24" font-family="system-ui,sans-serif" font-size="14" '
        f'fill="#111827">Cumulative equity, Rs {notional:,.0f} start</text>',
    ]
    for offset in range(5):
        value = low + (high - low) * offset / 4
        yy = y(value)
        parts.append(f'<line x1="{pad}" y1="{yy:.1f}" x2="{width - pad}" y2="{yy:.1f}" '
                     f'stroke="#e5e7eb" stroke-width="1"/>')
        parts.append(f'<text x="{pad - 8}" y="{yy + 4:.1f}" text-anchor="end" '
                     f'font-family="system-ui,sans-serif" font-size="10" fill="#6b7280">'
                     f'{value / 100000:.1f}L</text>')
    for name, series, color in zip(names, values, _SVG_COLORS):
        points = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(series))
        parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" '
                     f'points="{points}"/>')
        parts.append(f'<text x="{width - pad + 6}" y="{y(series[-1]) + 4:.1f}" '
                     f'font-family="system-ui,sans-serif" font-size="11" '
                     f'fill="{color}">{name}</text>')
    parts.append("</svg>")
    return "".join(parts)


def write_html(*, frame: pd.DataFrame, names: Sequence[str], stats: Dict[str, Dict[str, Any]],
               notional: float, cost_bps: float, window: Tuple[str, str],
               path: Path) -> None:
    rows = []
    for name in names:
        item = stats[name]
        pf = "inf" if item["profit_factor"] == float("inf") else f"{item['profit_factor']:.2f}"
        rows.append(
            f"<tr><td>{name}</td><td>{item['trades']}</td>"
            f"<td>{item['win_rate']:.1f}%</td><td>{pf}</td>"
            f"<td>{item['avg_trade_return_pct']:.2f}%</td>"
            f"<td>{item['net_pnl']:,.0f}</td><td>{item['max_drawdown']:,.0f}</td>"
            f"<td>{item['max_drawdown_pct']:.1f}%</td></tr>")
    day_rows = []
    for day, row in frame.iterrows():
        cells = "".join(
            "<td>—</td>" if row[name] == 0 else f"<td>{row[name]:,.0f}</td>"
            for name in names)
        cums = "".join(f"<td>{row[f'{name}_cum']:,.0f}</td>" for name in names)
        day_rows.append(f"<tr><td>{day}</td>{cells}{cums}</tr>")
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>INR day-wise: ORB vs mean-reversion vs QQQ</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;margin:32px;color:#111827}}
table{{border-collapse:collapse;margin:16px 0;font-size:13px}}
th,td{{border:1px solid #e5e7eb;padding:4px 8px;text-align:right}}
th{{background:#f9fafb;position:sticky;top:0}}
td:first-child,th:first-child{{text-align:left}}
.wrap{{overflow-x:auto}}
h1{{font-size:20px}}h2{{font-size:16px;margin-top:32px}}
.note{{color:#6b7280;font-size:13px;max-width:900px}}
</style></head><body>
<h1>INR day-wise comparison</h1>
<p class="note">Window {window[0]} → {window[1]}. ORB and mean-reversion use the repo's
real option execution (ATM BS 5 DTE 18% IV, −20%/+30% premium stops, Rs 7,000
risk per trade, full Indian option costs). QQQ sleeves use Rs {notional:,.0f}
notional per trade, so the exposure scale differs — read the % column as well as
rupees. qqq_nifty applies the QQQ daily rules to NIFTY; qqq_native is the real
QQQ strategy converted to INR at trade-date FX.</p>
{_svg_equity(frame, names, notional)}
<h2>Summary</h2>
<div class="wrap"><table>
<tr><th>Strategy</th><th>Trades</th><th>Win%</th><th>PF</th><th>Avg%</th>
<th>Net Rs</th><th>MaxDD Rs</th><th>DD%</th></tr>
{''.join(rows)}
</table></div>
<h2>Day by day</h2>
<div class="wrap"><table>
<tr><th>Date</th>{''.join(f'<th>{n} Rs</th>' for n in names)}
{''.join(f'<th>{n} cum</th>' for n in names)}</tr>
{''.join(day_rows)}
</table></div>
</body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--notional", type=float, default=DEFAULT_NOTIONAL)
    parser.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--daywise-csv", type=Path, default=DEFAULT_DAYWISE)
    parser.add_argument("--html", type=Path, default=DEFAULT_HTML)
    args = parser.parse_args(argv)

    frame_5m = load_5m(args.days)
    candles = to_candles(frame_5m)
    days = sorted({stamp.date().isoformat() for stamp in frame_5m.index})
    window = (days[0], days[-1])

    print(f"5m NIFTY: {len(candles)} bars, {len(days)} sessions, {window[0]} -> {window[1]}")
    print("Collecting ORB and mean-reversion signals ...")
    orb_signals = collect_orb_signals(candles)
    mr_signals = collect_mr_signals(candles)
    print(f"  ORB signals={len(orb_signals)}  mean-reversion signals={len(mr_signals)}")

    meta = repo_bt.SYMBOLS["^NSEI"]
    orb_kind = repo_bt.collect_orb_signals(candles, "NIFTY", meta["lot"], meta["step"])
    mr_kind = repo_bt.collect_mr_signals(candles)

    print("Simulating ORB and mean-reversion with the repo's option execution ...")
    orb_option = run_option_sleeve(
        candles, orb_kind, strategy="orb", meta=meta,
        earliest_min=15, cooldown=600, cooldown_rule="any")
    mr_option = run_option_sleeve(
        candles, mr_kind, strategy="mean_reversion", meta=meta, earliest_min=15)
    orb_trades = option_trade_objs(orb_option, args.notional)
    mr_trades = option_trade_objs(mr_option, args.notional)

    print("Simulating the same signals as linear spot at the fixed notional ...")
    orb_spot = simulate_spot(candles, orb_signals, strategy="orb",
                             notional=args.notional, cost_bps=args.cost_bps)
    mr_spot = simulate_spot(candles, mr_signals, strategy="mean_reversion",
                            notional=args.notional, cost_bps=args.cost_bps)

    print("Running QQQ rules on NIFTY daily ...")
    nifty_daily = load_nifty_daily()
    qqq_nifty_trades, _ = run_qqq_on_nifty(nifty_daily, notional=args.notional,
                                           cost_bps=args.cost_bps)

    print("Converting the QQQ trade ledger to INR ...")
    qqq_native_trades, qqq_native_daily = run_qqq_native_inr(
        notional=args.notional, cost_bps=args.cost_bps)

    series = {
        "orb": daily_pnl(orb_trades),
        "mean_reversion": daily_pnl(mr_trades),
        "qqq_nifty": daily_pnl(qqq_nifty_trades),
        "qqq_native": qqq_native_daily,
    }
    daywise = build_daywise(days=days, series=series, notional=args.notional)

    qqq_native_objs = [SimpleNamespace(**trade) for trade in qqq_native_trades]
    stats = {
        "orb": metrics(orb_trades, args.notional),
        "mean_reversion": metrics(mr_trades, args.notional),
        "qqq_nifty": metrics(in_window(qqq_nifty_trades, days), args.notional),
        "qqq_native": metrics(in_window(qqq_native_objs, days), args.notional),
    }
    full_stats = {
        "qqq_nifty_full": metrics(qqq_nifty_trades, args.notional),
        "qqq_native_full": metrics(qqq_native_objs, args.notional),
    }
    names = ["orb", "mean_reversion", "qqq_nifty", "qqq_native"]

    print("\n60-SESSION WINDOW — ORB/MR ON OPTIONS (repo sizing), QQQ AT Rs 10L")
    print("=" * 92)
    print(format_summary(stats))
    print("\nORB/MR: repo option execution (ATM BS 5 DTE 18% IV, -20%/+30% premium "
          "stops, Rs 7,000 risk/trade, real option costs).")
    print("QQQ   : Rs 10L notional per trade, spot. Different exposure scale, so "
          "read the % column, not just rupees.")
    print("qqq_native only contributes on the days its trades closed inside this "
          "window.")

    print("\nDAY-WISE REALISED P&L (Rs)")
    print("=" * 92)
    print(format_daywise(daywise, names))

    print("\nSAME SIGNALS AS LINEAR SPOT AT Rs 10L NOTIONAL (like-for-like check)")
    print("=" * 92)
    spot_stats = {
        "orb_spot": metrics(orb_spot, args.notional),
        "mean_reversion_spot": metrics(mr_spot, args.notional),
    }
    print(format_summary(spot_stats))
    print("Spot ORB loses because the strategy's own index stop/target is only a "
          "few points wide; the live book trades the option premium, not the index.")

    print("\nFULL-HISTORY CONTEXT (Rs notional)")
    print("=" * 92)
    print(format_summary(full_stats))
    print(f"\nqqq_nifty_full covers {nifty_daily.index[0].date()} -> "
          f"{nifty_daily.index[-1].date()} "
          f"({len(nifty_daily)} daily bars).")

    payload = {
        "config": {
            "days": args.days,
            "window": list(window),
            "notional_inr": args.notional,
            "cost_bps_per_side": args.cost_bps,
            "bars_5m": len(candles),
            "orb_signals": len(orb_signals),
            "mr_signals": len(mr_signals),
            "nifty_daily_bars": len(nifty_daily),
            "nifty_daily_range": [nifty_daily.index[0].date().isoformat(),
                                  nifty_daily.index[-1].date().isoformat()],
        },
        "stats_window": stats,
        "stats_spot_linear": spot_stats,
        "stats_full_history": full_stats,
        "daywise": daywise.reset_index().to_dict(orient="records"),
    }
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(json_safe(payload), indent=2, default=str),
                         encoding="utf-8")
    daywise.to_csv(args.daywise_csv)
    write_html(frame=daywise, names=names, stats=stats, notional=args.notional,
               cost_bps=args.cost_bps, window=window, path=args.html)

    print(f"\nSaved JSON    : {args.json}")
    print(f"Saved day-wise: {args.daywise_csv}")
    print(f"Saved HTML    : {args.html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
