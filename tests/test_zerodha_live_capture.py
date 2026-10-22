"""Focused offline tests for the read-only Zerodha capture loop."""

from datetime import date, datetime, timezone, timedelta
import json

from src.zerodha_live_capture import (
    CaptureSettings,
    CaptureSQLiteOptionArchive,
    ZerodhaLiveCapture,
)


IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 10, 15, 10, 0, tzinfo=IST)


def _instrument(token, symbol, underlying, kind, strike):
    return {
        "instrument_token": token,
        "exchange_token": str(token),
        "tradingsymbol": symbol,
        "name": underlying,
        "expiry": date(2026, 10, 29),
        "strike": strike,
        "tick_size": 0.05,
        "lot_size": 65 if underlying == "NIFTY" else 25,
        "instrument_type": kind,
        "segment": "NFO-OPT",
        "exchange": "NFO",
    }


class _FakeAdapter:
    def __init__(self, rows):
        self.rows = rows
        self.instrument_calls = 0
        self.historical_calls = []
        self.quote_calls = []
        self.fail_historical = set()
        self.historical_failures = {}
        self.quote_batch_failures = 0
        self.missing_quotes = set()

    def instruments(self, exchange):
        assert exchange == "NFO"
        self.instrument_calls += 1
        return list(self.rows)

    def historical(self, exchange, symbol, interval="5minute", period="30d"):
        assert exchange == "NFO"
        assert interval == "5minute"
        self.historical_calls.append(symbol)
        remaining = self.historical_failures.get(symbol, 0)
        if remaining:
            self.historical_failures[symbol] = remaining - 1
            raise TimeoutError("temporary historical failure")
        if symbol in self.fail_historical:
            raise RuntimeError("contract history unavailable")
        return [{
            "date": "2026-10-15T09:15:00+05:30",
            "open": 100,
            "high": 102,
            "low": 99,
            "close": 101,
            "volume": 1000,
            "open_interest": 5000,
        }]

    def quote(self, exchange, symbols):
        assert exchange == "NFO"
        self.quote_calls.append(list(symbols))
        if self.quote_batch_failures and len(symbols) > 1:
            self.quote_batch_failures -= 1
            raise TimeoutError("temporary quote batch failure")
        return {
            f"NFO:{symbol}": {
                "last_price": 101.5,
                "oi": 5000,
                "volume": 1000,
                "timestamp": "2026-10-15T09:15:00+05:30",
                "depth": {
                    "buy": [{"price": 100, "quantity": 10}],
                    "sell": [{"price": 101, "quantity": 12}],
                },
            }
            for symbol in symbols if symbol not in self.missing_quotes
        }


def _rows():
    return [
        _instrument("1", "NIFTY26OCT25000CE", "NIFTY", "CE", 25000),
        _instrument("2", "NIFTY26OCT25000PE", "NIFTY", "PE", 25000),
        _instrument("3", "BANKNIFTY26OCT52000CE", "BANKNIFTY", "CE", 52000),
        _instrument("4", "BANKNIFTY26OCT52000PE", "BANKNIFTY", "PE", 52000),
        _instrument("5", "RELIANCE26OCT2500CE", "RELIANCE", "CE", 2500),
        _instrument("6", "RELIANCE26OCT2500PE", "RELIANCE", "PE", 2500),
        {**_instrument("7", "OLDNIFTY25000CE", "NIFTY", "CE", 25000),
         "expiry": date(2026, 10, 14)},
    ]


def _capture(tmp_path, adapter, **kwargs):
    underlyings = kwargs.pop("underlyings", ("NIFTY", "BANKNIFTY", "RELIANCE"))
    settings = CaptureSettings(
        underlyings=underlyings,
        rate_limit_seconds=0,
        backoff_seconds=0,
        **kwargs,
    )
    archive = CaptureSQLiteOptionArchive(tmp_path / "capture.sqlite3")
    return archive, ZerodhaLiveCapture(adapter, archive, settings)


def test_selects_active_exact_ce_pe_identity_for_indices_and_stock(tmp_path):
    adapter = _FakeAdapter(_rows())
    archive, capture = _capture(tmp_path, adapter)
    try:
        report = capture.capture_once(as_of=NOW)
        assert report["status"] == "complete"
        assert {(row["underlying"], row["option_type"])
                for row in report["contracts"]} == {
                    ("NIFTY", "CE"), ("NIFTY", "PE"),
                    ("BANKNIFTY", "CE"), ("BANKNIFTY", "PE"),
                    ("RELIANCE", "CE"), ("RELIANCE", "PE"),
                }
        assert report["selected_contracts"] == 6
        assert report["orders_placed"] == 0
        assert report["coverage"]["selection"]["by_underlying"]["RELIANCE"] == 2
    finally:
        archive.close()


def test_preserves_observed_and_provider_timestamps_and_depth_alignment(tmp_path):
    adapter = _FakeAdapter(_rows()[:2])
    archive, capture = _capture(tmp_path, adapter)
    try:
        report = capture.capture_once(as_of=NOW)
        quote = archive.connection.execute(
            "SELECT observed_at_utc, source_timestamp_raw, source_timestamp_ms, raw_json "
            "FROM quotes ORDER BY quote_id LIMIT 1").fetchone()
        candle = archive.connection.execute(
            "SELECT timestamp_utc FROM candles ORDER BY timestamp_ms LIMIT 1").fetchone()
        assert quote["observed_at_utc"] == "2026-10-15T04:30:00+00:00"
        assert quote["source_timestamp_raw"] == "2026-10-15T09:15:00+05:30"
        assert quote["source_timestamp_ms"] is not None
        assert candle["timestamp_utc"] == "2026-10-15T03:45:00+00:00"
        assert quote["observed_at_utc"] != candle["timestamp_utc"]
        assert json.loads(quote["raw_json"])["depth"]["buy"][0]["quantity"] == 10
        alignment = report["quote_candle_alignment"]
        assert alignment["exact_timestamp_matches"] == 2
        assert alignment["coverage_percent"] == 100.0
        assert alignment["timestamps_fabricated"] is False
    finally:
        archive.close()


def test_retries_provider_calls_and_rate_limits_injected_sleep(tmp_path):
    adapter = _FakeAdapter(_rows()[:1])
    adapter.historical_failures["NIFTY26OCT25000CE"] = 1
    sleeps = []
    settings = CaptureSettings(
        underlyings=("NIFTY",), max_retries=2,
        backoff_seconds=0.5, rate_limit_seconds=0.25,
    )
    archive = CaptureSQLiteOptionArchive(tmp_path / "capture.sqlite3")
    try:
        report = ZerodhaLiveCapture(
            adapter, archive, settings, sleep=sleeps.append,
            monotonic=lambda: 0.0,
        ).capture_once(as_of=NOW)
        assert report["status"] == "complete"
        assert adapter.historical_calls == [
            "NIFTY26OCT25000CE", "NIFTY26OCT25000CE"]
        assert any(abs(value - 0.5) < 1e-9 for value in sleeps)
        assert any(abs(value - 0.25) < 1e-9 for value in sleeps)
    finally:
        archive.close()


def test_failed_contract_and_missing_quote_do_not_lose_other_contracts(tmp_path):
    adapter = _FakeAdapter(_rows()[:2])
    adapter.fail_historical.add("NIFTY26OCT25000PE")
    adapter.missing_quotes.add("NIFTY26OCT25000PE")
    archive, capture = _capture(tmp_path, adapter, underlyings=("NIFTY",))
    try:
        report = capture.capture_once(as_of=NOW)
        assert report["status"] == "partial"
        assert report["historical_contracts_queried"] == 1
        assert report["candle"]["inserted"] == 1
        assert report["quote"]["inserted"] == 1
        assert any(item["trading_symbol"] == "NIFTY26OCT25000PE"
                   for item in report["errors"])
        assert archive.connection.execute("SELECT COUNT(*) FROM candles").fetchone()[0] == 1
        assert archive.connection.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 1
    finally:
        archive.close()


def test_restart_is_idempotent_for_candles_and_same_observation(tmp_path):
    adapter = _FakeAdapter(_rows()[:2])
    archive, capture = _capture(tmp_path, adapter)
    try:
        first = capture.capture_once(as_of=NOW)
        second = capture.capture_once(as_of=NOW)
        assert first["candle"]["inserted"] == 2
        assert second["candle"]["inserted"] == 0
        assert second["candle"]["duplicates"] == 2
        assert second["quote"]["inserted"] == 0
        assert second["quote"]["duplicates"] == 2
        assert archive.connection.execute("SELECT COUNT(*) FROM candles").fetchone()[0] == 2
        assert archive.connection.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 2
    finally:
        archive.close()


def test_batch_failure_falls_back_to_individual_quotes_without_orders(tmp_path):
    adapter = _FakeAdapter(_rows()[:2])
    adapter.quote_batch_failures = 1
    archive, capture = _capture(tmp_path, adapter, quote_batch_size=2, max_retries=1)
    try:
        report = capture.capture_once(as_of=NOW)
        assert report["quote"]["batch_errors"] == 1
        assert report["quote"]["inserted"] == 2
        assert ["NIFTY26OCT25000CE", "NIFTY26OCT25000PE"] in adapter.quote_calls
        assert adapter.quote_calls[-2:] == [
            ["NIFTY26OCT25000CE"], ["NIFTY26OCT25000PE"]]
        assert report["orders_placed"] == 0
    finally:
        archive.close()


def test_loop_stops_on_keyboard_interrupt_and_has_no_order_boundary(tmp_path):
    adapter = _FakeAdapter(_rows()[:1])
    archive, capture = _capture(tmp_path, adapter)
    def stop(_seconds):
        raise KeyboardInterrupt
    try:
        result = ZerodhaLiveCapture(
            adapter, archive, capture.settings, sleep=stop,
        ).run(cycles=3, interval_seconds=1)
        assert result["cycles"] == 1
        assert result["stopped_reason"] == "keyboard_interrupt"
        assert result["orders_placed"] == 0
        source = __import__("src.zerodha_live_capture", fromlist=["__name__"])
        text = __import__("inspect").getsource(source)
        assert "from src.broker" not in text
        assert ".place_order(" not in text
    finally:
        archive.close()
