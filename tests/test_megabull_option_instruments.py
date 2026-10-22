"""Read-only current listing access; all HTTP is fake or explicitly forbidden."""

import csv
import io
import os
from datetime import datetime, timedelta

import pytest
import requests

from src.broker import megabull
from src.utils import IST


class Clock(datetime):
    current = datetime(2026, 10, 19, 10, 0, tzinfo=IST)

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("external HTTP/order operation is forbidden")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(megabull.MegaBullBroker, "_post", forbidden)
    monkeypatch.setattr(megabull.MegaBullBroker, "place_order", forbidden)
    monkeypatch.setattr(megabull.time, "sleep", lambda *_: None)
    monkeypatch.setattr(megabull, "datetime", Clock)
    Clock.current = datetime(2026, 10, 19, 10, 0, tzinfo=IST)


def listing(**overrides):
    return {
        "instrumentToken": "00123456", "tradingSymbol": "RELIANCE26O272500CE",
        "instrumentName": "RELIANCE 27 OCT 2500 CE", "underlying": "RELIANCE",
        "exchange": "NSE", "instrumentType": "OPTSTK", "lotSize": "500",
        "expiry": "27-Oct-2026", "strike": "2500.00", "optionType": "CE",
        "vendorExtra": "retained verbatim", **overrides,
    }


def csv_text(rows):
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return handle.getvalue()


def write_cache(path, rows, when=None):
    path.write_text(csv_text(rows), encoding="utf-8")
    stamp = (when or Clock.current).timestamp()
    os.utime(path, (stamp, stamp))


class Response:
    def __init__(self, payload=None, text="", status=200):
        self.payload, self.text, self.status_code = payload, text, status

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class InstrumentSession:
    download_url = "https://fake.invalid/instruments.csv"

    def __init__(self, rows, status=200):
        self.rows, self.status, self.calls = rows, status, []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url == megabull.BASE_URL + "/api/marketwatch/instruments":
            return Response({"downloadUrl": self.download_url}, status=self.status)
        assert url == self.download_url
        return Response(text=csv_text(self.rows), status=self.status)

    def post(self, *args, **kwargs):
        pytest.fail("listing accessor must never POST")


def test_current_cache_preserves_exact_rows_and_conflicting_metadata(tmp_path):
    first = listing()
    conflicting = listing(instrumentToken="999", lotSize="250")
    index = listing(instrumentToken="002", tradingSymbol="NIFTY26O2725000PE",
                    underlying="NIFTY", instrumentType="OPTIDX", optionType="PE",
                    lotSize="65", strike="25000")
    rows = [first, conflicting, index, listing(instrumentType="EQ"),
            listing(instrumentType="FUTIDX"), listing(exchange="BSE")]
    cache = tmp_path / "instruments.csv"
    write_cache(cache, rows)
    broker = megabull.MegaBullBroker(instrument_cache=cache)

    result = broker.option_instruments()

    assert result == [first, conflicting, index]
    # Consumers cannot mutate the shared same-day listing.
    result[0]["lotSize"] = "1"
    result.pop()
    assert broker.option_instruments() == [first, conflicting, index]
    assert broker.orders == broker.positions == {}


def test_missing_cache_uses_only_shared_instrument_get_api(tmp_path):
    rows = [listing()]
    session = InstrumentSession(rows)
    broker = megabull.MegaBullBroker(api_key="fake-key", session=session,
                                    instrument_cache=tmp_path / "instruments.csv")

    assert broker.option_instruments() == rows
    assert broker.instrument_for(rows[0]["tradingSymbol"]) == rows[0]
    assert broker.token_for(rows[0]["tradingSymbol"]) == "00123456"
    assert [call[0] for call in session.calls] == [
        megabull.BASE_URL + "/api/marketwatch/instruments", session.download_url]
    assert session.calls[0][1] == {
        "headers": {"api-key": "fake-key", "Content-Type": "application/json"},
        "timeout": megabull.TIMEOUT,
    }
    assert session.calls[1][1] == {"timeout": 60}
    assert broker.option_instruments() == rows
    assert len(session.calls) == 2


def test_prior_calendar_day_refreshes_even_when_less_than_24_hours_old(tmp_path):
    cache = tmp_path / "instruments.csv"
    Clock.current = datetime(2026, 10, 19, 0, 1, tzinfo=IST)
    write_cache(cache, [listing()], Clock.current - timedelta(minutes=2))
    current = listing(lotSize="250", instrumentToken="new-token")
    session = InstrumentSession([current])
    broker = megabull.MegaBullBroker(instrument_cache=cache, session=session)

    assert broker.option_instruments() == [current]
    assert len(session.calls) == 2


def test_reused_broker_refreshes_at_ist_midnight_and_new_instance_reuses_today(tmp_path):
    cache = tmp_path / "instruments.csv"
    old = listing()
    write_cache(cache, [old])
    current = listing(instrumentToken="new-token", lotSize="250")
    session = InstrumentSession([current])
    broker = megabull.MegaBullBroker(instrument_cache=cache, session=session)
    assert broker.option_instruments() == [old]
    assert session.calls == []

    Clock.current = datetime(2026, 10, 20, 0, 1, tzinfo=IST)
    assert broker.option_instruments() == [current]
    assert len(session.calls) == 2
    stamp = Clock.current.timestamp()
    os.utime(cache, (stamp, stamp))
    restarted = megabull.MegaBullBroker(instrument_cache=cache)
    assert restarted.option_instruments() == [current]


@pytest.mark.parametrize("status", [401, 503])
def test_failed_rollover_never_serves_previous_listing(tmp_path, status):
    cache = tmp_path / "instruments.csv"
    write_cache(cache, [listing()])
    session = InstrumentSession([listing()], status=status)
    broker = megabull.MegaBullBroker(instrument_cache=cache, session=session)
    assert broker.option_instruments()
    Clock.current += timedelta(days=1)

    with pytest.raises((PermissionError, requests.HTTPError)):
        broker.option_instruments()
    if status == 401:
        with pytest.raises(PermissionError):
            broker.option_instruments()
        assert len(session.calls) == 1


@pytest.mark.parametrize("bad_cache", ["not,a,listing\n", ""])
def test_malformed_current_cache_is_refreshed(tmp_path, bad_cache):
    cache = tmp_path / "instruments.csv"
    cache.write_text(bad_cache, encoding="utf-8")
    stamp = Clock.current.timestamp()
    os.utime(cache, (stamp, stamp))
    session = InstrumentSession([listing()])
    broker = megabull.MegaBullBroker(instrument_cache=cache, session=session)
    assert broker.option_instruments() == [listing()]
    assert len(session.calls) == 2


def test_raw_provider_rows_are_accepted_by_scanner_metadata(tmp_path):
    from src.options_live_scan import _metadata

    rows = [listing(), listing(instrumentToken="2", tradingSymbol="NIFTY26O2725000PE",
                              instrumentName="NIFTY 27 OCT 25000 PE", underlying="NIFTY",
                              instrumentType="OPTIDX", optionType="PE", lotSize="65",
                              strike="25000")]
    cache = tmp_path / "instruments.csv"
    write_cache(cache, rows)
    broker = megabull.MegaBullBroker(instrument_cache=cache)
    diagnostic = {}
    contracts = _metadata(broker.option_instruments(), [
        {"symbol": "RELIANCE", "instrument_type": "stock"},
        {"symbol": "NIFTY", "instrument_type": "index"},
    ], Clock.current, diagnostic)

    assert diagnostic == {"metadata_rejections": {}, "listed_contracts": 2}
    assert {row["underlying_symbol"] for row in contracts.values()} == {"RELIANCE", "NIFTY"}
    stock = next(row for row in contracts.values() if row["underlying_symbol"] == "RELIANCE")
    assert stock["instrument_token"] == "00123456"
    assert stock["lot_size"] == 500
