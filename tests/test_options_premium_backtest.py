"""Offline tests for the read-only SQLite option-premium backtest."""

from datetime import date, datetime, timedelta, timezone

from src.options_premium_backtest import (
    NOT_READY_FOR_OPTIONS_PNL,
    OPTIONS_PNL_READY,
    QUOTE_POLICY_INTERVAL_ALIGNED,
    compare_walk_forward,
    run_options_premium_backtest,
)
from src.zerodha_option_archive import SQLiteOptionArchive


IST = timezone(timedelta(hours=5, minutes=30))
DAY = date(2026, 10, 15)
EXPIRY = date(2026, 10, 29)


def _timestamp(minute: int) -> datetime:
    return datetime(2026, 10, 15, 9, 15, tzinfo=IST) + timedelta(minutes=minute)


def _instrument(token: str, symbol: str, option_type: str, strike: float = 25_000):
    return {
        "instrument_token": token,
        "exchange_token": token,
        "tradingsymbol": symbol,
        "name": "NIFTY",
        "expiry": EXPIRY,
        "strike": strike,
        "tick_size": 0.05,
        "lot_size": 65,
        "instrument_type": option_type,
        "segment": "NFO-OPT",
        "exchange": "NFO",
    }


def _signal(symbol: str, option_type: str, *, lot_size: int = 65, minute: int = 0,
            **extra):
    row = {
        "timestamp": _timestamp(minute).isoformat(),
        "underlying": "NIFTY",
        "trading_symbol": symbol,
        "expiry": EXPIRY.isoformat(),
        "strike": 25_000,
        "option_type": option_type,
        "lot_size": lot_size,
        "action": "BUY",
        "completed": True,
    }
    row.update(extra)
    return row


def _seed_archive(tmp_path, contracts, candles, quotes):
    archive = SQLiteOptionArchive(tmp_path / "options.sqlite3")
    run_id = archive.start_run(["NIFTY"], "5minute", "test")
    for contract in contracts:
        archive.upsert_contracts([contract], seen_at=_timestamp(0), run_id=run_id)
        symbol = contract["tradingsymbol"]
        archive.append_candles(
            contract,
            candles[symbol],
            interval="5minute",
            fetched_at=_timestamp(0),
            run_id=run_id,
        )
        for quote in quotes.get(symbol, []):
            archive.append_quote(
                contract,
                quote,
                observed_at=quote["observed_at"],
                run_id=run_id,
            )
    archive.connection.commit()
    return archive


def _candle(minute: int, open_: float, high: float, low: float, close: float):
    return [
        _timestamp(minute).isoformat(), open_, high, low, close, 1000, 5000
    ]


def _quote(
    minute: int,
    bid: float,
    ask: float,
    *,
    source_minute: int | None = None,
    observed_minute: int | None = None,
):
    source = _timestamp(minute if source_minute is None else source_minute).isoformat()
    return {
        "timestamp": source,
        "observed_at": _timestamp(minute if observed_minute is None else observed_minute),
        "bid": bid,
        "ask": ask,
        "last_price": bid,
        "volume": 1000,
        "open_interest": 5000,
    }


def _run(archive, signals, **kwargs):
    return run_options_premium_backtest(
        archive.connection,
        signals,
        underlyings=["NIFTY"],
        start=DAY.isoformat(),
        end=DAY.isoformat(),
        stop_fraction=0.20,
        target_fraction=0.30,
        profit_step_fraction=0.10,
        exit_policy="staged_bid_ladder",
        **kwargs,
    )


def test_valid_long_ce_and_pe_use_actual_ask_and_bid_values(tmp_path):
    ce = _instrument("1", "NIFTY26OCT25000CE", "CE")
    pe = _instrument("2", "NIFTY26OCT25000PE", "PE")
    candles = {
        ce["tradingsymbol"]: [
            _candle(0, 100, 101, 99, 100), _candle(5, 100, 105, 99, 104),
            _candle(10, 104, 115, 103, 112),
        ],
        pe["tradingsymbol"]: [
            _candle(0, 200, 201, 199, 200), _candle(5, 200, 205, 199, 204),
            _candle(10, 204, 225, 203, 220),
        ],
    }
    quotes = {
        ce["tradingsymbol"]: [_quote(5, 99, 100), _quote(10, 112, 113)],
        pe["tradingsymbol"]: [_quote(5, 199, 200), _quote(10, 222, 223)],
    }
    archive = _seed_archive(tmp_path, [ce, pe], candles, quotes)
    try:
        report = _run(archive, [
            _signal(ce["tradingsymbol"], "CE"),
            _signal(pe["tradingsymbol"], "PE"),
        ])
    finally:
        archive.close()

    assert report["status"] == OPTIONS_PNL_READY
    assert len(report["trade_ledger"]) == 2
    assert {trade["option_type"] for trade in report["trade_ledger"]} == {"CE", "PE"}
    assert {trade["entry_price_raw_ask"] for trade in report["trade_ledger"]} == {100.0, 200.0}
    assert {trade["exit_price_raw_bid"] for trade in report["trade_ledger"]} == {112.0, 222.0}
    assert report["provenance"]["quote_policy"] == "exact"
    assert report["coverage"]["exact_quotes"] == 4
    assert report["coverage"]["interval_aligned_quotes"] == 0
    assert report["trade_ledger"][0]["entry_quote_bid"] == 99.0
    assert report["trade_ledger"][0]["entry_quote_ask"] == 100.0
    assert report["trade_ledger"][0]["entry_quote_source_timestamp_ms"] == report["trade_ledger"][0]["entry_quote_observed_at_ms"]
    assert report["pnl"]["net_pnl"] > 0
    assert all(trade["exit_policy"] == "staged_bid_ladder" for trade in report["trade_ledger"])
    assert all(trade["highest_profit_stage"] == 1 for trade in report["trade_ledger"])
    assert report["provenance"]["black_scholes"] is False
    assert report["provenance"]["underlying_substitution"] is False


def test_executable_bid_stop_wins_even_when_candle_ohlc_touches_both(tmp_path):
    contract = _instrument("3", "NIFTY26OCT25000CE", "CE")
    candles = {contract["tradingsymbol"]: [
        _candle(0, 100, 101, 99, 100), _candle(5, 100, 105, 99, 100),
        _candle(10, 100, 120, 80, 100),
    ]}
    # The candle spans both old OHLC levels, but only the executable bid can
    # trigger the staged policy.  78 touches the initial -20% stop from 100.
    quotes = {contract["tradingsymbol"]: [_quote(5, 99, 100), _quote(10, 78, 79)]}
    archive = _seed_archive(tmp_path, [contract], candles, quotes)
    try:
        report = _run(archive, [_signal(contract["tradingsymbol"], "CE")])
    finally:
        archive.close()

    assert report["status"] == OPTIONS_PNL_READY
    assert report["trade_ledger"][0]["exit_reason"] == "stop"
    assert report["trade_ledger"][0]["exit_trigger"] == "initial_stop"
    assert report["trade_ledger"][0]["exit_price_raw_bid"] == 78.0


def test_ohlc_touch_without_bid_touch_does_not_exit_until_hard_flat(tmp_path):
    contract = _instrument("12", "NIFTY26OCT25000CE", "CE")
    candles = {contract["tradingsymbol"]: [
        _candle(0, 100, 101, 99, 100),
        _candle(5, 100, 105, 99, 100),
        _candle(10, 100, 140, 70, 100),
        _candle(15, 100, 105, 95, 101),
    ]}
    quotes = {contract["tradingsymbol"]: [
        _quote(5, 99, 100), _quote(10, 100, 101), _quote(15, 101, 102),
    ]}
    archive = _seed_archive(tmp_path, [contract], candles, quotes)
    try:
        report = _run(archive, [_signal(contract["tradingsymbol"], "CE")])
    finally:
        archive.close()

    trade = report["trade_ledger"][0]
    assert trade["exit_reason"] == "same_day_hard_flat"
    assert trade["exit_timestamp"] == _timestamp(15).isoformat()
    assert trade["highest_profit_stage"] == 0


def test_staged_bid_ladder_advances_all_levels_and_closes_at_30_percent(tmp_path):
    contract = _instrument("13", "NIFTY26OCT25000CE", "CE")
    candles = {contract["tradingsymbol"]: [
        _candle(0, 100, 101, 99, 100),
        _candle(5, 100, 105, 99, 100),
        _candle(10, 100, 112, 99, 111),
        _candle(15, 111, 122, 109, 121),
        _candle(20, 121, 132, 119, 131),
    ]}
    quotes = {contract["tradingsymbol"]: [
        _quote(5, 99, 100), _quote(10, 110, 111),
        _quote(15, 120, 121), _quote(20, 130, 131),
    ]}
    archive = _seed_archive(tmp_path, [contract], candles, quotes)
    try:
        report = _run(archive, [_signal(contract["tradingsymbol"], "CE")])
    finally:
        archive.close()

    trade = report["trade_ledger"][0]
    assert trade["exit_reason"] == "target"
    assert trade["exit_trigger"] == "final_target_30"
    assert trade["exit_price_raw_bid"] == 130.0
    assert trade["highest_profit_stage"] == 3
    assert trade["reached_profit_targets"] == [0.1, 0.2, 0.3]
    assert trade["stop_price"] == 120.0
    assert trade["target_price"] == 130.0


def test_staged_bid_ladder_skipped_level_ratchets_to_highest_reached(tmp_path):
    contract = _instrument("14", "NIFTY26OCT25000CE", "CE")
    candles = {contract["tradingsymbol"]: [
        _candle(0, 100, 101, 99, 100),
        _candle(5, 100, 105, 99, 100),
        _candle(10, 100, 126, 99, 125),
        _candle(15, 125, 126, 118, 119),
    ]}
    quotes = {contract["tradingsymbol"]: [
        _quote(5, 99, 100), _quote(10, 125, 126), _quote(15, 119, 120),
    ]}
    archive = _seed_archive(tmp_path, [contract], candles, quotes)
    try:
        report = _run(archive, [_signal(contract["tradingsymbol"], "CE")])
    finally:
        archive.close()

    trade = report["trade_ledger"][0]
    assert trade["exit_reason"] == "stop"
    assert trade["exit_trigger"] == "profit_lock_stop_20"
    assert trade["exit_price_raw_bid"] == 119.0
    assert trade["highest_profit_stage"] == 2
    assert trade["reached_profit_targets"] == [0.1, 0.2]
    assert trade["stop_price"] == 120.0


def test_same_day_hard_flat_uses_last_candle_bid_not_close(tmp_path):
    contract = _instrument("4", "NIFTY26OCT25000CE", "CE")
    candles = {contract["tradingsymbol"]: [
        _candle(0, 100, 101, 99, 100), _candle(5, 100, 105, 99, 100),
        _candle(10, 100, 105, 95, 101), _candle(15, 101, 106, 96, 102),
    ]}
    quotes = {contract["tradingsymbol"]: [
        _quote(5, 99, 100), _quote(10, 95, 96), _quote(15, 97, 98),
    ]}
    archive = _seed_archive(tmp_path, [contract], candles, quotes)
    try:
        report = _run(archive, [_signal(contract["tradingsymbol"], "CE")])
    finally:
        archive.close()

    trade = report["trade_ledger"][0]
    assert trade["exit_reason"] == "same_day_hard_flat"
    assert trade["exit_price_raw_bid"] == 97.0
    assert trade["exit_price_raw_bid"] != 102.0


def test_unfillable_first_entry_candle_is_not_skipped(tmp_path):
    contract = _instrument("15", "NIFTY26OCT25000CE", "CE")
    candles = {contract["tradingsymbol"]: [
        _candle(0, 100, 101, 99, 100),
        _candle(5, 100, 105, 99, 100),
        _candle(10, 100, 130, 99, 129),
    ]}
    quotes = {contract["tradingsymbol"]: [_quote(10, 128, 129)]}
    archive = _seed_archive(tmp_path, [contract], candles, quotes)
    try:
        report = _run(archive, [_signal(contract["tradingsymbol"], "CE")])
    finally:
        archive.close()

    assert report["status"] == NOT_READY_FOR_OPTIONS_PNL
    assert report["pnl"]["closed_trades"] == 0
    assert report["diagnostics"]["counts"]["unfillable_first_entry_candle"] == 1


def test_missing_intermediate_exit_bid_does_not_use_later_profit(tmp_path):
    contract = _instrument("16", "NIFTY26OCT25000CE", "CE")
    candles = {contract["tradingsymbol"]: [
        _candle(0, 100, 101, 99, 100),
        _candle(5, 100, 105, 99, 100),
        _candle(10, 100, 105, 99, 100),
        _candle(15, 100, 140, 99, 139),
    ]}
    quotes = {contract["tradingsymbol"]: [
        _quote(5, 99, 100), _quote(15, 138, 139),
    ]}
    archive = _seed_archive(tmp_path, [contract], candles, quotes)
    try:
        report = _run(archive, [_signal(contract["tradingsymbol"], "CE")])
    finally:
        archive.close()

    assert report["status"] == NOT_READY_FOR_OPTIONS_PNL
    assert report["pnl"]["net_pnl"] == 0.0
    assert report["diagnostics"]["counts"]["missing_intermediate_exit_bid"] == 1


def test_quote_observed_before_source_timestamp_is_rejected(tmp_path):
    contract = _instrument("17", "NIFTY26OCT25000CE", "CE")
    candles = {contract["tradingsymbol"]: [
        _candle(0, 100, 101, 99, 100), _candle(5, 100, 105, 99, 100),
    ]}
    quotes = {contract["tradingsymbol"]: [
        _quote(5, 99, 100, source_minute=5, observed_minute=4),
    ]}
    archive = _seed_archive(tmp_path, [contract], candles, quotes)
    try:
        report = _run(archive, [_signal(contract["tradingsymbol"], "CE")])
    finally:
        archive.close()

    assert report["status"] == NOT_READY_FOR_OPTIONS_PNL
    assert report["diagnostics"]["counts"]["quote_observed_before_source_timestamp"] == 1
    assert report["pnl"]["closed_trades"] == 0


def test_missing_and_false_like_completion_markers_are_rejected(tmp_path):
    contract = _instrument("18", "NIFTY26OCT25000CE", "CE")
    candles = {contract["tradingsymbol"]: [
        _candle(0, 100, 101, 99, 100), _candle(5, 100, 105, 99, 100),
        _candle(10, 100, 115, 99, 110),
    ]}
    quotes = {contract["tradingsymbol"]: [_quote(5, 99, 100), _quote(10, 112, 113)]}
    archive = _seed_archive(tmp_path, [contract], candles, quotes)
    signals = []
    for minute, completed in ((0, None), (0, False), (0, "false"), (0, "0"),
                              (0, "forming"), (0, "incomplete")):
        row = _signal(contract["tradingsymbol"], "CE", minute=minute)
        if completed is None:
            row.pop("completed")
        else:
            row["completed"] = completed
        signals.append(row)
    try:
        report = _run(archive, signals)
    finally:
        archive.close()

    assert report["status"] == NOT_READY_FOR_OPTIONS_PNL
    assert report["diagnostics"]["counts"]["incomplete_underlying_signal"] == 6
    assert report["pnl"]["closed_trades"] == 0


def test_closed_trade_with_another_data_failure_is_partial(tmp_path):
    contract = _instrument("19", "NIFTY26OCT25000CE", "CE")
    candles = {contract["tradingsymbol"]: [
        _candle(0, 100, 101, 99, 100), _candle(5, 100, 105, 99, 100),
        _candle(10, 100, 115, 99, 110),
    ]}
    quotes = {contract["tradingsymbol"]: [_quote(5, 99, 100), _quote(10, 112, 113)]}
    archive = _seed_archive(tmp_path, [contract], candles, quotes)
    incomplete = _signal(contract["tradingsymbol"], "CE", minute=0)
    incomplete["completed"] = False
    try:
        report = _run(archive, [
            _signal(contract["tradingsymbol"], "CE"),
            incomplete,
        ])
    finally:
        archive.close()

    assert report["pnl"]["closed_trades"] == 1
    assert report["coverage"]["coverage_complete"] is False
    assert report["coverage"]["partial_data"] is True
    assert report["coverage"]["profitability_claim_allowed"] is False


def test_missing_or_misaligned_quotes_fail_closed(tmp_path):
    contract = _instrument("5", "NIFTY26OCT25000CE", "CE")
    candles = {contract["tradingsymbol"]: [
        _candle(0, 100, 101, 99, 100), _candle(5, 100, 105, 99, 100),
    ]}
    # This quote is observed at the execution candle but sourced from 09:21;
    # exact source-timestamp alignment rejects it rather than using it.
    quotes = {contract["tradingsymbol"]: [_quote(5, 99, 100, source_minute=6)]}
    archive = _seed_archive(tmp_path, [contract], candles, quotes)
    try:
        report = _run(archive, [_signal(contract["tradingsymbol"], "CE")])
    finally:
        archive.close()

    assert report["status"] == NOT_READY_FOR_OPTIONS_PNL
    assert report["pnl"]["net_pnl"] == 0.0
    assert report["coverage"]["misaligned_quotes"] == 1
    assert report["provenance"]["order_calls"] == 0


def test_interval_aligned_quotes_use_source_time_without_relabeling_the_quote(tmp_path):
    contract = _instrument("9", "NIFTY26OCT25000CE", "CE")
    candles = {contract["tradingsymbol"]: [
        _candle(0, 100, 101, 99, 100),
        _candle(5, 100, 105, 99, 104),
        _candle(10, 104, 115, 103, 112),
    ]}
    # Source observations are inside the 09:20-09:25 and 09:25-09:30
    # intervals, rather than exactly at either candle start.
    quotes = {contract["tradingsymbol"]: [
        _quote(6, 99, 100, source_minute=6),
        _quote(11, 112, 113, source_minute=11),
    ]}
    archive = _seed_archive(tmp_path, [contract], candles, quotes)
    try:
        report = _run(
            archive,
            [_signal(contract["tradingsymbol"], "CE")],
            quote_policy=QUOTE_POLICY_INTERVAL_ALIGNED,
        )
    finally:
        archive.close()

    assert report["status"] == OPTIONS_PNL_READY
    assert report["provenance"]["quote_policy"] == QUOTE_POLICY_INTERVAL_ALIGNED
    assert report["provenance"]["quote_timestamp_basis"] == "source_timestamp_within_candle_interval"
    assert report["coverage"]["exact_quotes"] == 0
    assert report["coverage"]["interval_aligned_quotes"] == 2
    trade = report["trade_ledger"][0]
    assert trade["entry_price_raw_ask"] == 100.0
    assert trade["exit_price_raw_bid"] == 112.0
    assert trade["entry_quote_source_timestamp"] == _timestamp(6).isoformat()
    assert trade["entry_quote_observed_at"] == _timestamp(6).isoformat()
    assert trade["exit_quote_source_timestamp"] == _timestamp(11).isoformat()
    assert trade["exit_quote_observed_at"] == _timestamp(11).isoformat()


def test_interval_aligned_quote_observed_after_candle_completion_is_rejected(tmp_path):
    contract = _instrument("10", "NIFTY26OCT25000CE", "CE")
    candles = {contract["tradingsymbol"]: [
        _candle(0, 100, 101, 99, 100),
        _candle(5, 100, 105, 99, 100),
    ]}
    # The source time is in the candle, but the archive capture happened
    # after that candle's 09:25 completion.
    quotes = {contract["tradingsymbol"]: [
        _quote(5, 99, 100, source_minute=6, observed_minute=11),
    ]}
    archive = _seed_archive(tmp_path, [contract], candles, quotes)
    try:
        report = _run(
            archive,
            [_signal(contract["tradingsymbol"], "CE")],
            quote_policy=QUOTE_POLICY_INTERVAL_ALIGNED,
        )
    finally:
        archive.close()

    assert report["status"] == NOT_READY_FOR_OPTIONS_PNL
    assert report["coverage"]["quotes_observed_after_candle_completion"] == 1
    assert report["diagnostics"]["counts"]["quote_observed_after_candle_completion"] == 1
    assert report["pnl"]["closed_trades"] == 0


def test_interval_aligned_multiple_executable_quotes_are_ambiguous(tmp_path):
    contract = _instrument("11", "NIFTY26OCT25000CE", "CE")
    candles = {contract["tradingsymbol"]: [
        _candle(0, 100, 101, 99, 100),
        _candle(5, 100, 105, 99, 100),
    ]}
    quotes = {contract["tradingsymbol"]: [
        _quote(6, 99, 100, source_minute=6),
        _quote(7, 98, 101, source_minute=7),
    ]}
    archive = _seed_archive(tmp_path, [contract], candles, quotes)
    try:
        report = _run(
            archive,
            [_signal(contract["tradingsymbol"], "CE")],
            quote_policy=QUOTE_POLICY_INTERVAL_ALIGNED,
        )
    finally:
        archive.close()

    assert report["status"] == NOT_READY_FOR_OPTIONS_PNL
    assert report["coverage"]["ambiguous_quotes"] == 1
    assert report["diagnostics"]["counts"]["ambiguous_interval_quote"] == 1
    assert report["diagnostics"]["counts"]["ambiguous_quote_candidates"] == 1
    assert report["pnl"]["closed_trades"] == 0


def test_future_and_lookahead_signal_is_rejected(tmp_path):
    contract = _instrument("6", "NIFTY26OCT25000CE", "CE")
    candles = {contract["tradingsymbol"]: [
        _candle(0, 100, 101, 99, 100), _candle(5, 100, 105, 99, 100),
    ]}
    quotes = {contract["tradingsymbol"]: [_quote(5, 99, 100)]}
    archive = _seed_archive(tmp_path, [contract], candles, quotes)
    try:
        report = _run(archive, [_signal(
            contract["tradingsymbol"], "CE", minute=5,
            entry_timestamp=_timestamp(0).isoformat(),
        )])
    finally:
        archive.close()

    assert report["status"] == NOT_READY_FOR_OPTIONS_PNL
    assert report["pnl"]["closed_trades"] == 0
    assert report["diagnostics"]["counts"]["lookahead_signal"] == 1


def test_exact_lot_identity_is_required(tmp_path):
    contract = _instrument("7", "NIFTY26OCT25000CE", "CE")
    candles = {contract["tradingsymbol"]: [
        _candle(0, 100, 101, 99, 100), _candle(5, 100, 105, 99, 100),
    ]}
    quotes = {contract["tradingsymbol"]: [_quote(5, 99, 100)]}
    archive = _seed_archive(tmp_path, [contract], candles, quotes)
    try:
        report = _run(archive, [_signal(contract["tradingsymbol"], "CE", lot_size=75)])
    finally:
        archive.close()

    assert report["status"] == NOT_READY_FOR_OPTIONS_PNL
    assert report["diagnostics"]["counts"]["missing_contract_identity"] == 1
    assert report["pnl"]["net_pnl"] == 0.0


def test_empty_archive_is_not_ready_and_has_zero_pnl(tmp_path):
    archive = SQLiteOptionArchive(tmp_path / "empty.sqlite3")
    try:
        report = _run(archive, [_signal("NIFTY26OCT25000CE", "CE")])
    finally:
        archive.close()

    assert report["status"] == NOT_READY_FOR_OPTIONS_PNL
    assert report["coverage"]["contracts"] == 0
    assert report["pnl"] == {
        "gross_pnl": 0.0,
        "net_pnl": 0.0,
        "brokerage": 0.0,
        "taxes": 0.0,
        "slippage": 0.0,
        "total_costs": 0.0,
        "closed_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "breakeven_trades": 0,
    }


def test_supplied_sqlite_path_is_read_only_and_uses_archive_data(tmp_path):
    contract = _instrument("8", "NIFTY26OCT25000CE", "CE")
    candles = {contract["tradingsymbol"]: [
        _candle(0, 100, 101, 99, 100), _candle(5, 100, 105, 99, 100),
        _candle(10, 100, 115, 99, 110),
    ]}
    quotes = {contract["tradingsymbol"]: [_quote(5, 99, 100), _quote(10, 112, 113)]}
    archive = _seed_archive(tmp_path, [contract], candles, quotes)
    path = archive.path
    archive.close()

    report = run_options_premium_backtest(
        path,
        [_signal(contract["tradingsymbol"], "CE")],
        underlyings=["NIFTY"],
        start=DAY.isoformat(),
        end=DAY.isoformat(),
        stop_fraction=0.20,
        target_fraction=0.30,
    )

    assert report["status"] == OPTIONS_PNL_READY
    assert report["provenance"]["archive_path"] == str(path)
    assert report["orders_placed"] == 0


def test_walk_forward_comparison_selects_on_validation_not_test():
    candidate_results = {
        "candidate-a": [
            {"timestamp": _timestamp(0), "net_pnl": 1},
            {"timestamp": _timestamp(5), "net_pnl": 1},
            {"timestamp": _timestamp(10), "net_pnl": 5},
            {"timestamp": _timestamp(15), "net_pnl": 5},
            {"timestamp": _timestamp(20), "net_pnl": -100},
            {"timestamp": _timestamp(25), "net_pnl": -100},
        ],
        "candidate-b": [
            {"timestamp": _timestamp(0), "net_pnl": 2},
            {"timestamp": _timestamp(5), "net_pnl": 2},
            {"timestamp": _timestamp(10), "net_pnl": 4},
            {"timestamp": _timestamp(15), "net_pnl": 4},
            {"timestamp": _timestamp(20), "net_pnl": 1000},
            {"timestamp": _timestamp(25), "net_pnl": 1000},
        ],
    }

    comparison = compare_walk_forward(
        candidate_results,
        train_end=_timestamp(10),
        validation_end=_timestamp(20),
    )

    assert comparison["selection"]["selected_candidate"] == "candidate-a"
    assert comparison["selection"]["selection_partition"] == "validation"
    assert comparison["selection"]["test_used_for_selection"] is False
    assert comparison["candidate_metrics"]["candidate-a"]["train"]["score"] == 2.0
    assert comparison["candidate_metrics"]["candidate-a"]["validation"]["score"] == 10.0
    assert comparison["test"] == {
        "candidate": "candidate-a",
        "observations": 2,
        "score": -200.0,
        "mean_score": -100.0,
        "winning_observations": 0,
        "losing_observations": 2,
    }
