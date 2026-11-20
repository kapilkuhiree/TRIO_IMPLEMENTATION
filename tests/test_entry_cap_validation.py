"""Offline tests for the entry-clock parameter added for cap validation.

``scripts/validate_entry_cap.py`` compares a 12:30 last-entry rule with the
engine default of 14:30.  These tests pin the boundary the comparison relies
on: the cutoff is an upper bound in minutes after 09:15 and is inclusive.
"""

from collections import defaultdict

from scripts import backtest_orb_retest_v2 as engine

DAY = "2026-11-20"
META = {"name": "NIFTY", "lot": 65, "step": 50, "vol": 0.14}
CUTOFF_1230 = 12 * 60 + 30 - (9 * 60 + 15)  # 195


def _bar(hhmm: str, spot: float = 25_000.0) -> dict:
    stamp = f"{DAY}T{hhmm}:00+05:30"
    return {
        "timestamp": stamp,
        "open": spot, "high": spot + 5, "low": spot - 5, "close": spot,
        "volume": 0.0, "underlying_symbol": "NIFTY",
        "exchange": "NSE", "instrument_type": "index",
    }


def _candles(*times):
    return [_bar(t) for t in times]


def _by_date(candles):
    out = defaultdict(list)
    for i, candle in enumerate(candles):
        out[candle["timestamp"][:10]].append(i)
    return out


# index 1 is the 12:25 bar, so the trade enters on the 12:30 bar.
BOUNDARY = (_candles("09:15", "12:20", "12:25", "12:30", "12:35", "12:40"), 2)
# index 2 is the 12:30 bar, so the trade enters on the 12:35 bar.
LATE = (_candles("09:15", "12:25", "12:30", "12:35", "12:40", "12:45"), 2)


def test_build_candidates_keeps_a_post_1230_entry_by_default():
    candles, signal_idx = LATE
    kept = engine.build_candidates(
        candles, {signal_idx: "CE"}, META, strategy="orb", earliest_min=45)
    assert len(kept) == 1
    assert kept[0]["entry_ts"][11:16] == "12:35"


def test_entry_cutoff_rejects_the_same_entry():
    candles, signal_idx = LATE
    blocked = engine.build_candidates(
        candles, {signal_idx: "CE"}, META, strategy="orb",
        earliest_min=45, entry_cutoff_min=CUTOFF_1230)
    assert blocked == []


def test_entry_cutoff_is_inclusive_at_exactly_1230():
    candles, signal_idx = BOUNDARY
    at_cutoff = engine.build_candidates(
        candles, {signal_idx: "CE"}, META, strategy="orb",
        earliest_min=45, entry_cutoff_min=CUTOFF_1230)
    assert len(at_cutoff) == 1
    assert at_cutoff[0]["entry_ts"][11:16] == "12:30"

    one_minute_earlier = engine.build_candidates(
        candles, {signal_idx: "CE"}, META, strategy="orb",
        earliest_min=45, entry_cutoff_min=CUTOFF_1230 - 1)
    assert one_minute_earlier == []


def test_run_variant_honours_the_entry_cutoff():
    candles, signal_idx = LATE
    signals = {signal_idx: "CE"}
    by_date = _by_date(candles)

    taken = engine.run_variant(
        candles, by_date, signals, META, strategy="orb",
        earliest_min=45, max_trades_per_day=99)
    assert len(taken) == 1
    assert taken[0]["entry_ts"][11:16] == "12:35"

    refused = engine.run_variant(
        candles, by_date, signals, META, strategy="orb",
        earliest_min=45, max_trades_per_day=99,
        entry_cutoff_min=CUTOFF_1230)
    assert refused == []
