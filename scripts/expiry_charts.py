"""Corrected NIFTY expiry-day chart pack (replaces the Desktop nifty_charts.py).

What was wrong with the original
--------------------------------
1. It built the expiry calendar from ``weekday() == 3`` (Thursday).  NIFTY
   weekly options expire on **Tuesday**, holiday-shifted.  Every chart was
   therefore an E+2 session, not an expiry session.
2. ``today`` was hardcoded to 2026-11-19.
3. It emitted a chart for the current month's expiry even when that date was
   still in the future, so that page silently came out blank.
4. It plotted only the close, so an expiry-day open-drive and a flat pin looked
   identical.

This version derives the expiry calendar from the exchange's own trading
calendar (yfinance daily bars), so holidays shift automatically, drops future
dates, draws OHLC candles, and labels the bar interval actually used.

Usage::

    python scripts/expiry_charts.py
    python scripts/expiry_charts.py --weeks 9 --months 9
    python scripts/expiry_charts.py --no-pdf
"""

from __future__ import annotations

import argparse
import calendar
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "output" / "expiry_study" / "charts"
TICKER = "^NSEI"
INTERVALS = ("5m", "15m", "60m")


def trading_days(today: date, lookback_years: int = 2) -> List[date]:
    """Real NSE session dates, straight from the daily index bars."""
    import yfinance as yf

    start = today - timedelta(days=366 * lookback_years)
    frame = yf.download(TICKER, start=start.isoformat(),
                        end=(today + timedelta(days=1)).isoformat(),
                        interval="1d", progress=False, auto_adjust=False)
    if frame is None or frame.empty:
        raise RuntimeError("no daily ^NSEI data; cannot build the trading calendar")
    return sorted({stamp.date() for stamp in pd.to_datetime(frame.index)})


def _announced(weekday: int, day: date) -> date:
    """The nominal expiry weekday on or before ``day``."""
    return day - timedelta(days=(day.weekday() - weekday) % 7)


def expiry_calendar(
    sessions: Sequence[date], today: date, weeks: int, months: int,
    weekday: int = 1,
) -> Tuple[List[date], List[date]]:
    """Weekly + monthly expiries that have already traded, holiday-shifted.

    An expiry that falls on a holiday moves back to the previous session, so the
    calendar is resolved against the exchange's own session list rather than
    guessed.
    """
    session_set = set(sessions)
    past = [day for day in sessions if day <= today]

    def shift(nominal: date) -> date | None:
        for back in range(0, 7):
            candidate = nominal - timedelta(days=back)
            if candidate in session_set and candidate <= today:
                return candidate
        return None

    weekly: List[date] = []
    nominal = _announced(weekday, today)
    while len(weekly) < weeks and nominal >= sessions[0]:
        resolved = shift(nominal)
        if resolved and resolved not in weekly:
            weekly.append(resolved)
        nominal -= timedelta(days=7)

    monthly: List[date] = []
    year, month = today.year, today.month
    while len(monthly) < months:
        last_day = date(year, month, calendar.monthrange(year, month)[1])
        nominal = _announced(weekday, last_day)
        # A month whose nominal expiry has not happened yet is not an expiry we
        # can chart; without this the back-shift would walk into an ordinary
        # earlier session and invent one.
        if nominal <= today:
            resolved = shift(nominal)
            if resolved and resolved not in monthly:
                monthly.append(resolved)
        month -= 1
        if month == 0:
            year, month = year - 1, 12
        if year < sessions[0].year:
            break

    return sorted(weekly), sorted(monthly)


def fetch_day(day: date) -> Tuple[pd.DataFrame, str]:
    """Intraday bars for one session, with the interval that actually worked."""
    import yfinance as yf

    start = day.isoformat()
    end = (day + timedelta(days=1)).isoformat()
    for interval in INTERVALS:
        try:
            frame = yf.download(TICKER, start=start, end=end,
                                interval=interval, progress=False,
                                auto_adjust=False)
        except Exception:
            continue
        if frame is None or frame.empty:
            continue
        if isinstance(frame.columns, pd.MultiIndex):
            frame.columns = frame.columns.get_level_values(0)
        frame.index = pd.to_datetime(frame.index)
        if frame.index.tz is None:
            frame.index = frame.index.tz_localize("Asia/Kolkata")
        else:
            frame.index = frame.index.tz_convert("Asia/Kolkata")
        return frame, interval
    return pd.DataFrame(), ""


def plot_day(day: date, frame: pd.DataFrame, interval: str,
             out_dir: Path, label: str) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 6))
    x = range(len(frame))
    ax.vlines(x, frame["Low"], frame["High"], color="#444", linewidth=0.8)
    rising = frame["Close"] >= frame["Open"]
    ax.bar([i for i, up in zip(x, rising) if up],
           (frame["Close"] - frame["Open"])[rising],
           bottom=frame["Open"][rising], width=0.65, color="#1a9850")
    ax.bar([i for i, up in zip(x, rising) if not up],
           (frame["Close"] - frame["Open"])[~rising],
           bottom=frame["Open"][~rising], width=0.65, color="#d73027")

    step = max(1, len(frame) // 10)
    ax.set_xticks(list(x)[::step])
    ax.set_xticklabels([stamp.strftime("%H:%M") for stamp in frame.index[::step]],
                       rotation=45, ha="right")
    ax.set_title(f"NIFTY 50 - {label} expiry day {day:%Y-%m-%d} "
                 f"({day:%a}) - {interval} bars")
    ax.set_xlabel("Time (IST)")
    ax.set_ylabel("Index")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{label}_{day}.png"
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def build_pdf(weekly: Sequence[Tuple[date, Path]], monthly: Sequence[Tuple[date, Path]],
              output: Path) -> Path:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    width, height = letter
    pdf = canvas.Canvas(str(output), pagesize=letter)

    def section(title: str, rows: Sequence[Tuple[date, Path]]) -> None:
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawCentredString(width / 2, height - 60, title)
        pdf.setFont("Helvetica", 10)
        pdf.drawCentredString(width / 2, height - 80,
                              "NIFTY weekly expiry = Tuesday, shifted back to the "
                              "previous session on holidays")
        pdf.showPage()
        for day, path in rows:
            pdf.setFont("Helvetica-Bold", 12)
            pdf.drawCentredString(width / 2, height - 40, f"{day:%Y-%m-%d} ({day:%a})")
            pdf.drawImage(str(path), (width - 520) / 2, height - 430,
                          width=520, height=312, preserveAspectRatio=True)
            pdf.showPage()

    section("NIFTY Weekly Expiry Charts", weekly)
    section("NIFTY Monthly Expiry Charts", monthly)
    pdf.save()
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="NIFTY expiry-day candle charts")
    parser.add_argument("--weeks", type=int, default=9)
    parser.add_argument("--months", type=int, default=9)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--pdf", default=str(Path.home() / "Desktop" / "Nifty_Expiry_Report.pdf"))
    parser.add_argument("--no-pdf", action="store_true")
    parser.add_argument("--today", default=None)
    args = parser.parse_args()

    today = date.fromisoformat(args.today) if args.today else date.today()
    out_dir = Path(args.out)

    # yfinance writes "5m data not available" to stderr on every fallback; the
    # ladder below is expected to walk it, so keep the console readable.
    import logging
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)

    sessions = trading_days(today)
    weekly, monthly = expiry_calendar(sessions, today, args.weeks, args.months)
    print(f"trading calendar: {len(sessions)} sessions, "
          f"{sessions[0]} .. {sessions[-1]}")
    print(f"weekly expiries : {[str(d) for d in weekly]}")
    print(f"monthly expiries: {[str(d) for d in monthly]}")

    weekly_rows: List[Tuple[date, Path]] = []
    monthly_rows: List[Tuple[date, Path]] = []
    for label, days, sink in (("weekly", weekly, weekly_rows),
                              ("monthly", monthly, monthly_rows)):
        for day in days:
            frame, interval = fetch_day(day)
            if frame.empty:
                print(f"  {label} {day}: no intraday data, skipped")
                continue
            path = plot_day(day, frame, interval, out_dir, label)
            print(f"  {label} {day} ({day:%a}): {len(frame)} bars @ {interval} -> {path.name}")
            sink.append((day, path))

    if not args.no_pdf:
        pdf_path = build_pdf(weekly_rows, monthly_rows, Path(args.pdf))
        print(f"\nPDF: {pdf_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
