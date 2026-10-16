"""Offline tests for the incremental read-only Zerodha option archive."""

from datetime import date, datetime, timezone, timedelta

from src.zerodha_option_archive import (
    SQLiteOptionArchive,
    archive_active_options,
    discover_active_option_instruments,
)


IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 10, 15, 10, 0, tzinfo=IST)


def _instrument(token, symbol, underlying="NIFTY", kind="CE",
                expiry=date(2026, 10, 29), strike=25000):
    return {
        "instrument_token": token,
        "exchange_token": str(token),
        "tradingsymbol": symbol,
        "name": underlying,
        "expiry": expiry,
        "strike": strike,
        "tick_size": 0.05,
        "lot_size": 65 if underlying == "NIFTY" else 35,
        "instrument_type": kind,
        "segment": "NFO-OPT",
        "exchange": "NFO",
    }


class _FakeKite:
    def __init__(self):
        self.historical_calls = []
        self.quote_calls = []
        self.rows = [
            _instrument("1", "NIFTY26OCT25000CE"),
            _instrument("2", "NIFTY26OCT25000PE", kind="PE"),
            _instrument("3", "BANKNIFTY26OCT52000CE", "BANKNIFTY", strike=52000),
            _instrument("4", "OLDNIFTY25000CE", expiry=date(2026, 10, 14)),
            _instrument("5", "FINNIFTY26OCT25000CE", "FINNIFTY"),
            {"tradingsymbol": "BROKEN", "name": "NIFTY", "instrument_type": "CE"},
        ]

    def instruments(self, exchange):
        assert exchange == "NFO"
        return list(self.rows)

    def historical_data(self, token, start, end, interval):
        self.historical_calls.append((token, interval))
        assert interval == "5minute"
        return [
            ["2026-10-15T09:15:00+05:30", 100, 102, 99, 101, 1000, 5000],
            ["2026-10-15T09:20:00+05:30", 101, 103, 100, 102, 1200, 5100],
            # Invalid and non-session rows prove fail-closed ingestion.
            ["2026-10-15T09:25:00+05:30", 0, 103, 100, 102, 1200, 5100],
            ["2026-10-15T08:00:00+05:30", 100, 103, 99, 102, 1200, 5100],
        ]

    def quote(self, keys):
        self.quote_calls.append(list(keys))
        return {
            key: {
                "last_price": 101.5,
                "oi": 5000,
                "volume": 1000,
                "timestamp": "2026-10-15T09:30:00+05:30",
                "depth": {"buy": [{"price": 100}], "sell": [{"price": 101}]},
            }
            for key in keys
        }


class _Adapter:
    def __init__(self):
        self.kite = _FakeKite()

    def instruments(self, exchange):
        return self.kite.instruments(exchange)

    def historical(self, exchange, symbol, interval="5minute", period="30d"):
        row = next(item for item in self.kite.rows
                   if item.get("tradingsymbol") == symbol)
        return self.kite.historical_data(row["instrument_token"], None, None, interval)

    def quote(self, exchange, symbols):
        return self.kite.quote([f"{exchange}:{symbol}" for symbol in symbols])


def test_discovery_filters_active_nifty_and_banknifty_rows():
    adapter = _Adapter()
    rejected = []
    rows = discover_active_option_instruments(
        adapter, as_of=NOW, rejections=rejected)
    assert {row["tradingsymbol"] for row in rows} == {
        "NIFTY26OCT25000CE", "NIFTY26OCT25000PE", "BANKNIFTY26OCT52000CE"}
    assert any(item["reason"] == "expired_contract" for item in rejected)
    assert any(item["reason"] == "unsupported_underlying" for item in rejected)
    assert any(item["reason"] == "invalid_contract_identity" for item in rejected)


def test_archive_deduplicates_candles_and_persists_quotes(tmp_path):
    path = tmp_path / "options.sqlite3"
    adapter = _Adapter()
    with SQLiteOptionArchive(path) as archive:
        run_id = archive.start_run(["NIFTY"], "5minute", "1d")
        instrument = _instrument("99", "NIFTY26OCT25000CE")
        archive.upsert_contracts([instrument], seen_at=NOW, run_id=run_id)
        first = archive.append_candles(
            instrument, adapter.historical("NFO", instrument["tradingsymbol"]),
            interval="5minute", fetched_at=NOW, run_id=run_id)
        second = archive.append_candles(
            instrument, adapter.historical("NFO", instrument["tradingsymbol"]),
            interval="5minute", fetched_at=NOW, run_id=run_id)
        assert first.inserted == 2 and first.rejected == 2
        assert second.inserted == 0 and second.duplicates == 2
        q = {"bid": 100, "ask": 101, "last_price": 101,
             "volume": 1000, "open_interest": 5000,
             "timestamp": "2026-10-15T09:30:00+05:30"}
        assert archive.append_quote(instrument, q, observed_at=NOW,
                                    run_id=run_id).inserted == 1
        assert archive.append_quote(instrument, q, observed_at=NOW,
                                    run_id=run_id).duplicates == 1
        report = archive.coverage(underlyings=["NIFTY"], trading_dates=[NOW.date()])
        assert report["candles"]["rows"] == 2
        assert report["quotes"]["executable_rows"] == 1
        assert report["readiness"] == "NOT_READY_FOR_OPTIONS_PNL"

    with SQLiteOptionArchive(path) as reopened:
        report = reopened.coverage(underlyings=["NIFTY"])
        assert report["selection"]["contracts"] == 1
        assert report["candles"]["rows"] == 2
        assert report["quotes"]["rows"] == 1


def test_archive_collector_uses_nfo_candles_batches_quotes_and_no_orders(tmp_path):
    adapter = _Adapter()
    with SQLiteOptionArchive(tmp_path / "archive.sqlite3") as archive:
        report = archive_active_options(
            adapter, archive, as_of=NOW, period="1d", max_contracts=2,
            quote_batch_size=1)
        assert report["selection"]["partial_selection"] is True
        assert report["run"]["historical_contracts_queried"] == 2
        assert len(adapter.kite.historical_calls) == 2
        assert len(adapter.kite.quote_calls) == 2
        assert report["run"]["orders_placed"] == 0
        assert report["readiness"] == "NOT_READY_FOR_OPTIONS_PNL"


def test_non_executable_quote_is_preserved_without_fabrication(tmp_path):
    instrument = _instrument("77", "NIFTY26OCT25000CE")
    with SQLiteOptionArchive(tmp_path / "archive.sqlite3") as archive:
        run_id = archive.start_run(["NIFTY"], "5minute", "1d")
        archive.upsert_contracts([instrument], seen_at=NOW, run_id=run_id)
        stats = archive.append_quote(
            instrument, {"bid": None, "ask": 101, "last_price": 101},
            observed_at=NOW, run_id=run_id)
        assert stats.inserted == 1
        row = archive.connection.execute(
            "SELECT bid, ask, executable FROM quotes").fetchone()
        assert row["bid"] is None
        assert row["ask"] == 101
        assert row["executable"] == 0
