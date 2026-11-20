"""Apply the QQQ RSI(3) + EMA(100) + 10-day VWAP rules to the whole NFO universe.

The QQQ edge was validated on a single US ETF.  This script asks the obvious
follow-up: does the same daily mean-reversion rule work on every option
underlying the repo can actually trade (218 NSE F&O stocks plus NIFTY,
BANKNIFTY and FINNIFTY)?

Rules are identical to ``scripts/backtest_qqq_mean_reversion.py``:

* RSI(3) closes below 22,
* EMA(100) is above its value 5 trading days ago,
* close is at least 1% below the 10-day rolling VWAP,
* buy the next session's open,
* sell the next open once RSI(3) closes above 55,
* one position at a time per symbol, fixed Rs 10,00,000 notional per trade.

Every symbol is an independent sleeve.  The aggregate view is the sum of those
sleeves (capital = Rs 10L x symbols that traded), so it is a per-candidate
screen, not a capital-constrained portfolio.  Reports are per candidate and
per month, as required for wide backtests.

Volume note: NSE index feeds carry Volume = 0, so for those three symbols the
rolling VWAP falls back to an equal-weighted typical-price average.  Stocks use
real volume.

    python scripts/backtest_qqq_rules_wide.py
    python scripts/backtest_qqq_rules_wide.py --limit 40
    python scripts/backtest_qqq_rules_wide.py --indices-only
    python scripts/backtest_qqq_rules_wide.py --refresh-data --workers 8

DISCLAIMER: Educational purposes only. Not financial advice.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest_qqq_mean_reversion import (  # noqa: E402
    Trade,
    build_signals,
    run_backtest,
    summarize,
)

DATA_DIR = ROOT / "output" / "qqq_wide_daily"
DEFAULT_JSON = ROOT / "output" / "qqq_rules_wide.json"
DEFAULT_CANDIDATES = ROOT / "output" / "qqq_rules_wide_candidates.csv"
DEFAULT_MONTHS = ROOT / "output" / "qqq_rules_wide_months.csv"
DEFAULT_DAYWISE = ROOT / "output" / "qqq_rules_wide_daywise.csv"
DEFAULT_HTML = ROOT / "output" / "qqq_rules_wide.html"

DEFAULT_NOTIONAL = 1_000_000.0
DEFAULT_COST_BPS = 5.0
DEFAULT_START = "2000-01-01"
DEFAULT_END = "2026-11-14"


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------

def load_universe(indices_only: bool = False) -> Dict[str, Dict[str, Any]]:
    from scripts.backtest_active_wide import nfo_universe

    universe = nfo_universe()
    if indices_only:
        universe = {name: meta for name, meta in universe.items()
                    if meta["kind"] == "index"}
    return universe


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _cache_path(name: str) -> Path:
    return DATA_DIR / f"{name.replace('&', 'AND').replace('/', '_')}.csv"


def _extract(raw: pd.DataFrame, ticker: str) -> Optional[pd.DataFrame]:
    if raw is None or raw.empty:
        return None
    if isinstance(raw.columns, pd.MultiIndex):
        if ticker not in set(raw.columns.get_level_values(0)):
            return None
        frame = raw[ticker].copy()
    else:
        frame = raw.copy()
    if "Close" not in frame.columns:
        return None
    if "Adj Close" not in frame.columns:
        frame["Adj Close"] = frame["Close"]
    for column in ("Open", "High", "Low", "Close", "Volume"):
        if column not in frame.columns:
            return None
    frame = frame.dropna(subset=["Open", "High", "Low", "Close"])
    frame.index = pd.to_datetime(frame.index).tz_localize(None)
    frame = frame[[ "Open", "High", "Low", "Close", "Adj Close", "Volume"]]
    return frame


def download_daily(universe: Dict[str, Dict[str, Any]], *, start: str, end: str,
                   refresh: bool = False, chunk_size: int = 40) -> Dict[str, str]:
    import yfinance as yf

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    paths: Dict[str, str] = {}
    pending: List[Tuple[str, str]] = []
    for name, meta in universe.items():
        path = _cache_path(name)
        if path.exists() and not refresh:
            paths[name] = str(path)
        else:
            pending.append((name, meta["ticker"]))

    print(f"Daily cache: {len(paths)} present, {len(pending)} to download")
    for offset in range(0, len(pending), chunk_size):
        chunk = pending[offset:offset + chunk_size]
        tickers = [ticker for _, ticker in chunk]
        print(f"  downloading {offset + 1}-{offset + len(chunk)} / {len(pending)}")
        try:
            raw = yf.download(tickers, start=start, end=end, interval="1d",
                              auto_adjust=False, progress=False, threads=True,
                              group_by="ticker")
        except Exception as exc:  # noqa: BLE001 - network is best-effort
            print(f"    batch failed: {exc}")
            continue
        for name, ticker in chunk:
            frame = _extract(raw, ticker)
            if frame is None or frame.empty:
                print(f"    no data: {name} ({ticker})")
                continue
            path = _cache_path(name)
            frame.to_csv(path, index_label="Date")
            paths[name] = str(path)
    return paths


def load_bars(path: str, *, is_index: bool = False) -> pd.DataFrame:
    frame = pd.read_csv(path, parse_dates=["Date"], index_col="Date")
    frame = frame.dropna(subset=["Open", "High", "Low", "Close"])
    frame.index = pd.to_datetime(frame.index).tz_localize(None)
    if not is_index:
        # Yahoo leaves zero-volume placeholder bars (O=H=L=C, often a stale
        # corporate-action print) that create fake +500% trades.
        frame = frame[frame["Volume"].fillna(0.0) > 0]
    return frame


def split_segments(bars: pd.DataFrame,
                   threshold: float = 0.5) -> List[pd.DataFrame]:
    """Split at implausible one-day gaps so no trade spans a data splice."""
    if len(bars) < 2:
        return [bars]
    returns = bars["Close"].pct_change().abs().to_numpy()
    cuts = [0]
    for index in range(1, len(bars)):
        value = returns[index]
        if np.isfinite(value) and value > threshold:
            cuts.append(index)
    cuts.append(len(bars))
    return [bars.iloc[cuts[k]:cuts[k + 1]] for k in range(len(cuts) - 1)]


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------

def run_symbol(name: str, bars: pd.DataFrame, *, notional: float,
               cost_bps: float, is_index: bool = False) -> Dict[str, Any]:
    bars = bars.copy()
    if is_index or float(bars["Volume"].fillna(0.0).sum()) <= 0:
        bars["Volume"] = 1.0  # volume-less index VWAP fallback
    else:
        # Use total-return bars so dividends/bonuses do not print as fake
        # one-day jumps inside a trade.
        factor = (bars["Adj Close"] / bars["Close"]).replace(
            [np.inf, -np.inf], np.nan).ffill().fillna(1.0)
        for column in ("Open", "High", "Low", "Close"):
            bars[column] = bars[column] * factor

    trades: List[Trade] = []
    for segment in split_segments(bars):
        if len(segment) < 120:
            continue
        signals = build_signals(segment)
        trades.extend(run_backtest(segment, signals, exit_timing="next_open",
                                   cost_bps=cost_bps, capital=notional))
    stats = summarize(trades, capital=notional)
    return {
        "name": name,
        "bars": int(len(bars)),
        "first": bars.index[0].date().isoformat(),
        "last": bars.index[-1].date().isoformat(),
        "stats": stats,
        "trades": trades,
    }


def window_results(results: Dict[str, Dict[str, Any]], *, since: str,
                   until: str, notional: float) -> Dict[str, Dict[str, Any]]:
    """Keep only trades closed inside [since, until] and restate the stats.

    Signals are still computed on the full history, so EMA(100)/RSI warm-up is
    identical to the full-sample run; only the realised trades are clipped.
    """
    clipped: Dict[str, Dict[str, Any]] = {}
    for name, result in results.items():
        trades = [trade for trade in result["trades"]
                  if since <= str(trade.exit_date)[:10] <= until]
        if not trades:
            continue
        clipped[name] = {
            **result,
            "trades": trades,
            "stats": summarize(trades, capital=notional),
        }
    return clipped


def month_key(trade: Trade) -> str:
    return str(getattr(trade, "exit_date", ""))[:7]


def month_breakdown(all_trades: Sequence[Trade]) -> pd.DataFrame:
    grouped: Dict[str, List[Trade]] = defaultdict(list)
    for trade in all_trades:
        grouped[month_key(trade)].append(trade)
    rows = []
    for month, trades in sorted(grouped.items()):
        pnls = [trade.pnl for trade in trades]
        wins = sum(1 for value in pnls if value > 0)
        rows.append({
            "month": month,
            "trades": len(trades),
            "win_rate": round(wins / len(trades) * 100.0, 2),
            "net_pnl": round(sum(pnls), 2),
        })
    return pd.DataFrame(rows)


def combined_daywise(results: Dict[str, Dict[str, Any]]) -> pd.DataFrame:
    per_day: Dict[str, float] = defaultdict(float)
    for result in results.values():
        for trade in result["trades"]:
            per_day[str(trade.exit_date)[:10]] += float(trade.pnl)
    if not per_day:
        return pd.DataFrame(columns=["date", "pnl", "cum_pnl"])
    days = sorted(per_day)
    frame = pd.DataFrame({"date": days,
                          "pnl": [per_day[day] for day in days]})
    frame["cum_pnl"] = frame["pnl"].cumsum()
    return frame


def aggregate_stats(results: Dict[str, Dict[str, Any]], *,
                    notional: float) -> Dict[str, Any]:
    all_trades: List[Trade] = []
    for result in results.values():
        all_trades.extend(result["trades"])
    if not all_trades:
        return {"symbols": 0, "trades": 0, "net_pnl": 0.0,
                "win_rate": 0.0, "profit_factor": 0.0, "capital": 0.0,
                "return_pct": 0.0, "max_drawdown": 0.0, "max_drawdown_pct": 0.0}
    pnls = np.array([trade.pnl for trade in all_trades], dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    gross_profit = float(wins.sum())
    gross_loss = float(-losses.sum())
    capital = notional * len(results)
    equity = np.concatenate(([capital], capital + np.cumsum(pnls)))
    peaks = np.maximum.accumulate(equity)
    drawdowns = equity - peaks
    max_dd = float(drawdowns.min())
    return {
        "symbols": len(results),
        "trades": len(all_trades),
        "net_pnl": round(float(pnls.sum()), 2),
        "win_rate": round(len(wins) / len(all_trades) * 100.0, 2),
        "profit_factor": (round(gross_profit / gross_loss, 4)
                          if gross_loss else float("inf")),
        "capital": round(capital, 2),
        "return_pct": round(float(pnls.sum()) / capital * 100.0, 2),
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(abs(max_dd) / capital * 100.0, 2),
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _pf(value: float) -> str:
    return "inf" if value == float("inf") else f"{value:.2f}"


def format_candidates(rows: List[Dict[str, Any]], limit: int = 40) -> str:
    header = (f"{'Symbol':<14}{'Bars':>7}{'Trades':>7}{'Win%':>7}{'PF':>7}"
              f"{'Avg%':>8}{'Net Rs':>14}{'MaxDD Rs':>13}{'DD%':>7}")
    lines = [header, "-" * len(header)]
    for row in rows[:limit]:
        stats = row["stats"]
        lines.append(
            f"{row['name']:<14}{row['bars']:>7}{stats['trades']:>7}"
            f"{stats['win_rate']:>6.1f}%{_pf(stats['profit_factor']):>7}"
            f"{stats['avg_trade_return_pct']:>8.2f}{stats['net_pnl']:>14,.0f}"
            f"{stats['max_drawdown']:>13,.0f}{stats['max_drawdown_pct_of_capital']:>6.1f}%")
    return "\n".join(lines)


def format_months(frame: pd.DataFrame) -> str:
    header = f"{'Month':<10}{'Trades':>8}{'Win%':>8}{'Net Rs':>15}"
    lines = [header, "-" * len(header)]
    for row in frame.itertuples():
        lines.append(f"{row.month:<10}{row.trades:>8}{row.win_rate:>7.1f}%"
                     f"{row.net_pnl:>15,.0f}")
    return "\n".join(lines)


def _svg(daywise: pd.DataFrame) -> str:
    width, height, pad = 960, 320, 48
    if daywise.empty:
        return "<p>No trades.</p>"
    values = daywise["cum_pnl"].to_numpy(dtype=float)
    low, high = float(values.min()), float(values.max())
    if high == low:
        high, low = high + 1.0, low - 1.0
    count = len(values)
    plot_w, plot_h = width - 2 * pad, height - 2 * pad

    def x(index: int) -> float:
        return pad + plot_w * index / max(count - 1, 1)

    def y(value: float) -> float:
        return pad + plot_h * (1.0 - (value - low) / (high - low))

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}">',
             f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
             f'<text x="{pad}" y="24" font-family="system-ui,sans-serif" '
             f'font-size="14" fill="#111827">Combined cumulative P&amp;L (Rs)</text>']
    for offset in range(5):
        value = low + (high - low) * offset / 4
        yy = y(value)
        parts.append(f'<line x1="{pad}" y1="{yy:.1f}" x2="{width - pad}" '
                     f'y2="{yy:.1f}" stroke="#e5e7eb"/>')
        parts.append(f'<text x="{pad - 8}" y="{yy + 4:.1f}" text-anchor="end" '
                     f'font-family="system-ui,sans-serif" font-size="10" '
                     f'fill="#6b7280">{value / 100000:.0f}L</text>')
    points = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
    parts.append(f'<polyline fill="none" stroke="#2563eb" stroke-width="2" '
                 f'points="{points}"/>')
    parts.append("</svg>")
    return "".join(parts)


def write_html(*, rows: List[Dict[str, Any]], months: pd.DataFrame,
               aggregate: Dict[str, Any], daywise: pd.DataFrame,
               notional: float, cost_bps: float, start: str, end: str,
               path: Path) -> None:
    table = []
    for row in rows:
        stats = row["stats"]
        table.append(
            f"<tr><td>{row['name']}</td><td>{row['bars']}</td>"
            f"<td>{stats['trades']}</td><td>{stats['win_rate']:.1f}%</td>"
            f"<td>{_pf(stats['profit_factor'])}</td>"
            f"<td>{stats['avg_trade_return_pct']:.2f}%</td>"
            f"<td>{stats['net_pnl']:,.0f}</td>"
            f"<td>{stats['max_drawdown']:,.0f}</td>"
            f"<td>{stats['max_drawdown_pct_of_capital']:.1f}%</td></tr>")
    month_rows = "".join(
        f"<tr><td>{row.month}</td><td>{row.trades}</td>"
        f"<td>{row.win_rate:.1f}%</td><td>{row.net_pnl:,.0f}</td></tr>"
        for row in months.itertuples())
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>QQQ rules across the NFO universe</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;margin:32px;color:#111827}}
table{{border-collapse:collapse;margin:16px 0;font-size:13px}}
th,td{{border:1px solid #e5e7eb;padding:4px 8px;text-align:right}}
th{{background:#f9fafb}}td:first-child,th:first-child{{text-align:left}}
.wrap{{overflow-x:auto}}h1{{font-size:20px}}h2{{font-size:16px;margin-top:28px}}
.note{{color:#6b7280;font-size:13px;max-width:960px}}
</style></head><body>
<h1>QQQ rules across the NFO option universe</h1>
<p class="note">RSI(3)&lt;22, EMA(100) rising over 5 sessions, close ≤ 10-day VWAP × 0.99,
next-open entry, exit next open on RSI(3)&gt;55. Rs {notional:,.0f} notional per
symbol, {cost_bps:.1f} bps per side. {start} → {end}. Each symbol is an independent
sleeve, so the aggregate is a per-candidate screen, not a capital-constrained book.</p>
<p><strong>Aggregate:</strong> {aggregate['symbols']} symbols,
{aggregate['trades']} trades, win {aggregate['win_rate']:.1f}%,
PF {_pf(aggregate['profit_factor'])}, net Rs {aggregate['net_pnl']:,.0f},
return {aggregate['return_pct']:.1f}% on Rs {aggregate['capital']:,.0f},
max DD Rs {aggregate['max_drawdown']:,.0f} ({aggregate['max_drawdown_pct']:.1f}%).</p>
{_svg(daywise)}
<h2>Per candidate</h2>
<div class="wrap"><table>
<tr><th>Symbol</th><th>Bars</th><th>Trades</th><th>Win%</th><th>PF</th>
<th>Avg%</th><th>Net Rs</th><th>MaxDD Rs</th><th>DD%</th></tr>
{''.join(table)}
</table></div>
<h2>Month by month</h2>
<div class="wrap"><table>
<tr><th>Month</th><th>Trades</th><th>Win%</th><th>Net Rs</th></tr>
{month_rows}
</table></div>
</body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--notional", type=float, default=DEFAULT_NOTIONAL)
    parser.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    parser.add_argument("--limit", type=int, default=0,
                        help="only the first N symbols (0 = all)")
    parser.add_argument("--indices-only", action="store_true")
    parser.add_argument("--since", default=None,
                        help="only count trades exiting on/after YYYY-MM-DD")
    parser.add_argument("--until", default=None,
                        help="only count trades exiting on/before YYYY-MM-DD")
    parser.add_argument("--min-trades", type=int, default=0,
                        help="hide candidates with fewer than N trades in the window")
    parser.add_argument("--refresh-data", action="store_true")
    parser.add_argument("--workers", type=int, default=1,
                        help="kept for CLI parity; downloads use yfinance threads")
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--candidates-csv", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--months-csv", type=Path, default=DEFAULT_MONTHS)
    parser.add_argument("--daywise-csv", type=Path, default=DEFAULT_DAYWISE)
    parser.add_argument("--html", type=Path, default=DEFAULT_HTML)
    args = parser.parse_args(argv)

    universe = load_universe(indices_only=args.indices_only)
    if args.limit:
        universe = dict(list(universe.items())[:args.limit])
    print(f"Universe: {len(universe)} option underlyings")

    paths = download_daily(universe, start=args.start, end=args.end,
                           refresh=args.refresh_data)
    if not paths:
        print("No daily data available.")
        return 1

    results: Dict[str, Dict[str, Any]] = {}
    skipped: List[str] = []
    for index, (name, meta) in enumerate(universe.items(), start=1):
        path = paths.get(name)
        if not path:
            skipped.append(name)
            continue
        is_index = meta.get("kind") == "index"
        try:
            bars = load_bars(path, is_index=is_index)
        except Exception as exc:  # noqa: BLE001
            print(f"  {name}: unreadable cache ({exc})")
            skipped.append(name)
            continue
        if len(bars) < 120:
            skipped.append(name)
            continue
        try:
            results[name] = run_symbol(name, bars, notional=args.notional,
                                       cost_bps=args.cost_bps, is_index=is_index)
        except Exception as exc:  # noqa: BLE001
            print(f"  {name}: backtest failed ({exc})")
            skipped.append(name)
        if index % 25 == 0:
            print(f"  processed {index}/{len(universe)}")

    if args.since or args.until:
        since = args.since or "1900-01-01"
        until = args.until or "2999-12-31"
        before = len(results)
        results = window_results(results, since=since, until=until,
                                 notional=args.notional)
        print(f"Window {since} -> {until}: {len(results)} of {before} symbols "
              f"closed a trade")

    if args.min_trades:
        results = {name: result for name, result in results.items()
                   if result["stats"]["trades"] >= args.min_trades}

    rows = sorted(results.values(), key=lambda row: row["stats"]["net_pnl"],
                  reverse=True)
    all_trades: List[Trade] = []
    for result in results.values():
        all_trades.extend(result["trades"])
    months = month_breakdown(all_trades)
    aggregate = aggregate_stats(results, notional=args.notional)
    daywise = combined_daywise(results)

    print(f"\nProcessed {len(results)} symbols, skipped {len(skipped)}")
    if skipped:
        print("Skipped:", ", ".join(skipped[:20]),
              "..." if len(skipped) > 20 else "")

    print("\nAGGREGATE (sum of independent Rs 10L sleeves)")
    print("=" * 96)
    print(f"Symbols {aggregate['symbols']} | trades {aggregate['trades']} | "
          f"win {aggregate['win_rate']:.1f}% | PF {_pf(aggregate['profit_factor'])} | "
          f"net Rs {aggregate['net_pnl']:,.0f} | "
          f"return {aggregate['return_pct']:.1f}% on Rs {aggregate['capital']:,.0f} | "
          f"max DD Rs {aggregate['max_drawdown']:,.0f} "
          f"({aggregate['max_drawdown_pct']:.1f}%)")

    positive = sum(1 for row in results.values() if row["stats"]["net_pnl"] > 0)
    traded = sum(1 for row in results.values() if row["stats"]["trades"] > 0)
    print(f"Traded symbols: {traded} | profitable: {positive} "
          f"({positive / traded * 100.0:.1f}% of traded)" if traded else "")

    print("\nTOP 25 CANDIDATES")
    print("=" * 96)
    print(format_candidates(rows, limit=25))
    print("\nBOTTOM 15 CANDIDATES")
    print("=" * 96)
    print(format_candidates(list(reversed(rows))[:15], limit=15))

    print("\nMONTH BY MONTH (all symbols combined)")
    print("=" * 96)
    print(format_months(months))

    payload = {
        "config": {
            "start": args.start,
            "end": args.end,
            "since": args.since,
            "until": args.until,
            "min_trades": args.min_trades,
            "notional_inr": args.notional,
            "cost_bps_per_side": args.cost_bps,
            "universe": len(universe),
            "processed": len(results),
            "skipped": skipped,
        },
        "aggregate": aggregate,
        "candidates": [
            {"name": row["name"], "bars": row["bars"], "first": row["first"],
             "last": row["last"], **row["stats"]}
            for row in rows
        ],
        "months": months.to_dict(orient="records"),
        "daywise": daywise.to_dict(orient="records"),
    }
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    pd.DataFrame(payload["candidates"]).to_csv(args.candidates_csv, index=False)
    months.to_csv(args.months_csv, index=False)
    daywise.to_csv(args.daywise_csv, index=False)
    write_html(rows=rows, months=months, aggregate=aggregate, daywise=daywise,
               notional=args.notional, cost_bps=args.cost_bps,
               start=args.start, end=args.end, path=args.html)

    print(f"\nSaved JSON     : {args.json}")
    print(f"Saved candidates: {args.candidates_csv}")
    print(f"Saved months   : {args.months_csv}")
    print(f"Saved day-wise : {args.daywise_csv}")
    print(f"Saved HTML     : {args.html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
