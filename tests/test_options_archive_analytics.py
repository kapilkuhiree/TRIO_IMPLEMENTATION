"""Offline tests for the additive option-archive analytics sidecar."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from src.options_archive_analytics import (
    FeatureConfig,
    asof_spot,
    build_analytics,
    coverage_summary,
    day_bounds_ms,
    derive_features,
    dte_days,
    latest_candle_timestamp,
    select_contract,
    time_to_expiry_years,
)
from src.options_pricing import bs_price
from src.zerodha_option_archive import SQLiteOptionArchive

IST = timezone(timedelta(hours=5, minutes=30))
DAY = date(2026, 10, 15)
EXPIRY = date(2026, 10, 29)


def _ts(minute: int = 0) -> datetime:
    return datetime(2026, 10, 15, 9, 15, tzinfo=IST) + timedelta(minutes=minute)


def _instrument(token: str, symbol: str, option_type: str, strike: float,
                expiry: date = EXPIRY):
    return {
        "instrument_token": token,
        "exchange_token": token,
        "tradingsymbol": symbol,
        "name": "NIFTY",
        "expiry": expiry,
        "strike": strike,
        "tick_size": 0.05,
        "lot_size": 65,
        "instrument_type": option_type,
        "segment": "NFO-OPT",
        "exchange": "NFO",
    }


def _candle(minute: int, close: float):
    return [_ts(minute).isoformat(), close, close, close, close, 1000, 5000]


def _quote(minute: int, bid: float, ask: float):
    return {
        "timestamp": _ts(minute).isoformat(),
        "bid": bid,
        "ask": ask,
        "last_price": bid,
        "volume": 1000,
        "open_interest": 5000,
    }


def _seed(tmp_path, contracts, candles=None, quotes=None):
    archive = SQLiteOptionArchive(tmp_path / "options.sqlite3")
    run_id = archive.start_run(["NIFTY"], "5minute", "test")
    candles = candles or {}
    quotes = quotes or {}
    for contract in contracts:
        archive.upsert_contracts([contract], seen_at=_ts(0), run_id=run_id)
        symbol = contract["tradingsymbol"]
        if candles.get(symbol):
            archive.append_candles(
                contract, candles[symbol], interval="5minute",
                fetched_at=_ts(0), run_id=run_id,
            )
        for quote in quotes.get(symbol, []):
            archive.append_quote(
                contract, quote, observed_at=_ts(quote_minute(quote)), run_id=run_id,
            )
    archive.connection.commit()
    return archive


def quote_minute(quote):
    stamp = datetime.fromisoformat(quote["timestamp"])
    return int((stamp - _ts(0)).total_seconds() // 60)


def _spot_pickle(tmp_path: Path) -> Path:
    frame = pd.DataFrame(
        {
            "Open": [23_000.0, 23_010.0],
            "High": [23_020.0, 23_030.0],
            "Low": [22_990.0, 23_000.0],
            "Close": [23_010.0, 23_020.0],
        },
        index=pd.DatetimeIndex([_ts(0), _ts(45)]),
    )
    path = tmp_path / "NIFTY_spot.pkl"
    frame.to_pickle(path)
    return path


def test_time_to_expiry_and_dte_are_calendar_based():
    when = datetime(2026, 10, 28, 15, 30, tzinfo=IST)
    stamp = int(when.timestamp() * 1000)
    years = time_to_expiry_years(stamp, EXPIRY.isoformat())
    assert years is not None
    assert abs(years - 1.0 / 365.25) < 1e-9
    assert dte_days(stamp, EXPIRY.isoformat()) == 1.0


def test_asof_spot_uses_latest_bar_and_reports_the_gap():
    series = {"NIFTY": ([1_000, 2_000, 3_000], [100.0, 101.0, 102.0])}
    spot, stamp, gap = asof_spot(series, "nifty", 2_500)
    assert spot == 101.0
    assert stamp == 2_000
    assert gap == 0.5


def test_asof_spot_returns_none_when_the_bar_is_too_stale():
    series = {"NIFTY": ([1_000], [100.0])}
    spot, stamp, gap = asof_spot(series, "NIFTY", 10_000, max_gap_seconds=1.0)
    assert spot is None
    assert stamp == 1_000
    assert gap == 9.0


def test_derive_features_recovers_the_input_implied_vol():
    when = int(_ts(45).timestamp() * 1000)
    years = time_to_expiry_years(when, EXPIRY.isoformat())
    price = bs_price(23_000.0, 23_000.0, years, 0.18, "CE")
    features = derive_features(
        underlying="NIFTY",
        expiry=EXPIRY.isoformat(),
        strike=23_000.0,
        timestamp_ms=when,
        spot=23_000.0,
        spot_gap_seconds=0.0,
        bid=price - 0.5,
        ask=price + 0.5,
        executable=True,
        option_type="CE",
    )
    assert features["flags"] == "ok"
    assert features["implied_vol"] is not None
    assert abs(features["implied_vol"] - 0.18) < 0.02
    assert abs(features["moneyness"]) < 1e-9
    assert features["spread_abs"] == 1.0


def test_derive_features_flags_zero_bid_non_executable_and_missing_spot():
    features = derive_features(
        underlying="NIFTY",
        expiry=EXPIRY.isoformat(),
        strike=23_000.0,
        timestamp_ms=int(_ts(0).timestamp() * 1000),
        spot=None,
        spot_gap_seconds=None,
        bid=0.0,
        ask=100.0,
        last_price=50.0,
        executable=False,
        option_type="CE",
    )
    flags = set(features["flags"].split(","))
    assert {"zero_bid", "non_executable", "no_spot"} <= flags
    assert features["price"] == 50.0
    assert features["implied_vol"] is None


def test_derive_features_flags_crossed_and_wide_spreads():
    crossed = derive_features(
        underlying="NIFTY", expiry=EXPIRY.isoformat(), strike=23_000.0,
        timestamp_ms=int(_ts(0).timestamp() * 1000), spot=23_000.0,
        spot_gap_seconds=0.0, bid=101.0, ask=100.0, executable=True,
    )
    assert "crossed" in crossed["flags"].split(",")

    wide = derive_features(
        underlying="NIFTY", expiry=EXPIRY.isoformat(), strike=23_000.0,
        timestamp_ms=int(_ts(0).timestamp() * 1000), spot=23_000.0,
        spot_gap_seconds=0.0, bid=100.0, ask=130.0, executable=True,
        config=FeatureConfig(wide_spread_pct=0.10),
    )
    assert "wide_spread" in wide["flags"].split(",")


def test_select_contract_prefers_nearest_expiry_then_strike(tmp_path):
    near = EXPIRY
    far = date(2026, 11, 5)
    contracts = [
        _instrument("1", "NIFTY26OCT23000CE", "CE", 23_000.0, near),
        _instrument("2", "NIFTY26OCT23100CE", "CE", 23_100.0, near),
        _instrument("3", "NIFTY26NOV23000CE", "CE", 23_000.0, far),
    ]
    archive = _seed(tmp_path, contracts)
    try:
        chosen = select_contract(
            archive.connection,
            underlying="NIFTY",
            option_type="CE",
            target_strike=23_040.0,
            trade_date=DAY,
        )
    finally:
        archive.close()
    assert chosen is not None
    assert chosen["trading_symbol"] == "NIFTY26OCT23000CE"
    assert chosen["expiry_date"] == near.isoformat()
    assert abs(chosen["strike_drift_pct"] - 40.0 / 23_040.0) < 1e-9


def test_select_contract_returns_none_without_a_future_expiry(tmp_path):
    contracts = [
        _instrument("1", "NIFTY26OCT23000CE", "CE", 23_000.0, EXPIRY),
    ]
    archive = _seed(tmp_path, contracts)
    try:
        chosen = select_contract(
            archive.connection,
            underlying="NIFTY",
            option_type="CE",
            target_strike=23_000.0,
            trade_date=EXPIRY,
        )
    finally:
        archive.close()
    assert chosen is None


def test_latest_candle_timestamp_returns_the_last_candle_of_the_day(tmp_path):
    contract = _instrument("1", "NIFTY26OCT23000CE", "CE", 23_000.0)
    candles = {contract["tradingsymbol"]: [_candle(0, 100.0), _candle(45, 110.0)]}
    archive = _seed(tmp_path, [contract], candles)
    try:
        latest = latest_candle_timestamp(
            archive.connection, contract_id=1, day=DAY,
        )
    finally:
        archive.close()
    assert latest is not None
    assert latest == int(_ts(45).timestamp() * 1000)


def test_build_analytics_writes_sidecar_and_coverage(tmp_path):
    contract = _instrument("1", "NIFTY26OCT23000CE", "CE", 23_000.0)
    candles = {contract["tradingsymbol"]: [_candle(0, 100.0), _candle(45, 105.0)]}
    quotes = {contract["tradingsymbol"]: [_quote(45, 99.0, 100.0)]}
    archive = _seed(tmp_path, [contract], candles, quotes)
    archive_path = tmp_path / "options.sqlite3"
    sidecar_path = tmp_path / "options_analytics.sqlite3"
    archive.close()

    summary = build_analytics(
        archive_path,
        sidecar_path,
        spot_pickles={"NIFTY": _spot_pickle(tmp_path)},
        underlyings=["NIFTY"],
    )
    assert summary["contracts"] == 1
    assert summary["quotes"] == 1
    assert summary["candles"] == 2
    assert summary["iv_quotes"] == 1
    assert summary["spot_bars"]["NIFTY"] == 2
    assert summary["flags"]["ok"] >= 1

    coverage = coverage_summary(sidecar_path)
    assert coverage["quotes"]["rows"] == 1
    assert coverage["quotes"]["executable"] == 1
    assert coverage["quotes"]["iv"] == 1
    assert coverage["candles"]["rows"] == 2
    assert coverage["iv"]["quotes_n"] == 1
    assert coverage["by_underlying"]["NIFTY"]["executable"] == 1
    assert coverage["meta"]["contracts"] == "1"

    start_ms, end_ms = day_bounds_ms(DAY)
    assert start_ms < end_ms
