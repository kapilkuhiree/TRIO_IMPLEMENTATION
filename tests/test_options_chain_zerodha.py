"""Zerodha chain fetch: request budget, pacing and failover contract.

These are offline tests.  The 2026-11-21 rate-limit commit shipped a NameError
on the success path and a ``_finite_number`` reference that did not exist; the
whole chain fetch silently degraded to "always fall back to NSE".  Nothing
covered this module, so the suite stayed green.  Every branch below is here to
make that impossible again.
"""

import datetime as dt

import pytest

from src import options_chain_zerodha as ocz

SPOT = 25000.0
TODAY = dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30))).date()


def future_expiry(weeks=0):
    return TODAY + dt.timedelta(days=7 + 7 * weeks)


class FakeAdapter:
    """Minimal Kite adapter: records quote calls, serves deterministic data."""

    def __init__(self, expiries, strikes=None, fail_after=None):
        self.expiries = list(expiries)
        self.strikes = list(strikes or range(24000, 26001, 50))
        self.fail_after = fail_after
        self.quote_calls = []

    def instruments(self, exchange):
        assert exchange == "NFO"
        rows = []
        for expiry in self.expiries:
            for strike in self.strikes:
                for kind in ("CE", "PE"):
                    rows.append({
                        "name": "NIFTY",
                        "instrument_type": kind,
                        "expiry": expiry,
                        "strike": float(strike),
                        "tradingsymbol": f"NIFTY{expiry:%d%b%Y}{strike}{kind}".upper(),
                        "instrument_token": strike,
                    })
        return rows

    def quote(self, exchange, symbols):
        if exchange == "NSE":
            return {"NSE:NIFTY 50": {"last_price": SPOT}}
        self.quote_calls.append(list(symbols))
        if self.fail_after is not None and len(self.quote_calls) > self.fail_after:
            raise RuntimeError("Too many requests")
        return {
            f"NFO:{symbol}": {
                "last_price": 100.0,
                "bid": 99.0,
                "ask": 101.0,
                "oi": 5000,
                "volume": 1000,
                "timestamp": "2026-11-23T10:00:00+05:30",
            }
            for symbol in symbols
        }


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Reset pacing state and provide a controllable clock."""
    ocz.reset_rate_limit_state()
    monkeypatch.setenv("TRIO_ZERODHA_QUOTE_SPACING", "0.25")
    monkeypatch.delenv("TRIO_ZERODHA_QUOTE_BATCH", raising=False)
    yield
    ocz.reset_rate_limit_state()


def install(monkeypatch, adapter):
    monkeypatch.setattr(ocz, "get_shared_adapter", lambda: adapter)


def test_success_path_returns_a_fully_quoted_chain(monkeypatch):
    """Regression: the success path used to raise NameError and return None."""
    expiry = future_expiry()
    adapter = FakeAdapter([expiry])
    install(monkeypatch, adapter)

    chain = ocz.fetch_zerodha_chain("NIFTY")

    assert chain is not None, "Zerodha chain must not fail on the success path"
    assert chain["underlying"] == SPOT
    key = expiry.strftime("%d-%b-%Y")
    assert chain["expiries"] == [key]
    row = chain["by_expiry"][key][0]
    assert row["CE"]["bidPrice"] == 99.0
    assert row["CE"]["askPrice"] == 101.0
    assert row["CE"]["openInterest"] == 5000
    assert row["PE"]["trading_symbol"].endswith("PE")


def test_max_expiries_quotes_only_the_nearest(monkeypatch):
    near, far = future_expiry(0), future_expiry(1)
    adapter = FakeAdapter([far, near])  # deliberately unsorted
    install(monkeypatch, adapter)

    chain = ocz.fetch_zerodha_chain("NIFTY", max_expiries=1)

    assert chain["expiries"] == [near.strftime("%d-%b-%Y")]
    assert set(chain["by_expiry"]) == {near.strftime("%d-%b-%Y")}
    quoted = {symbol for call in adapter.quote_calls for symbol in call}
    assert all(f"{near:%d%b%Y}".upper() in symbol for symbol in quoted)
    assert all(f"{far:%d%b%Y}".upper() not in symbol for symbol in quoted)


def test_strike_band_drops_far_wings_without_emptying_the_chain(monkeypatch):
    expiry = future_expiry()
    adapter = FakeAdapter([expiry])
    install(monkeypatch, adapter)

    chain = ocz.fetch_zerodha_chain("NIFTY", max_strike_distance_pct=0.02)

    strikes = [row["strike"] for row in chain["by_expiry"][expiry.strftime("%d-%b-%Y")]]
    assert strikes, "a strike band must never produce an empty chain"
    assert min(strikes) == 24500 and max(strikes) == 25500
    assert all(abs(strike - SPOT) <= SPOT * 0.02 for strike in strikes)


def test_quote_batch_size_splits_requests(monkeypatch):
    expiry = future_expiry()
    adapter = FakeAdapter([expiry], strikes=range(24000, 26001, 50))
    install(monkeypatch, adapter)

    ocz.fetch_zerodha_chain("NIFTY", quote_batch_size=50)

    assert adapter.quote_calls, "at least one quote call is required"
    assert all(len(call) <= 50 for call in adapter.quote_calls)
    assert len(adapter.quote_calls) > 1


def test_quote_calls_are_paced_between_batches_and_across_fetches(monkeypatch):
    expiry = future_expiry()
    adapter = FakeAdapter([expiry])
    install(monkeypatch, adapter)

    clock = {"now": 1000.0}
    sleeps = []

    def monotonic():
        return clock["now"]

    def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(ocz.time, "monotonic", monotonic)
    monkeypatch.setattr(ocz.time, "sleep", sleep)

    ocz.fetch_zerodha_chain("NIFTY", quote_batch_size=50,
                            quote_spacing_seconds=0.25)
    first_fetch_calls = len(adapter.quote_calls)
    assert first_fetch_calls > 1
    # Every call after the first is separated by the configured gap.
    assert len(sleeps) == first_fetch_calls - 1
    assert all(abs(gap - 0.25) < 1e-9 for gap in sleeps)

    sleeps.clear()
    ocz.fetch_zerodha_chain("NIFTY", quote_batch_size=50,
                            quote_spacing_seconds=0.25)
    # The first call of a second fetch is paced too: limits are per API key.
    assert sleeps and abs(sleeps[0] - 0.25) < 1e-9


def test_zero_spacing_disables_pacing(monkeypatch):
    expiry = future_expiry()
    adapter = FakeAdapter([expiry])
    install(monkeypatch, adapter)
    slept = []
    monkeypatch.setattr(ocz.time, "sleep", lambda seconds: slept.append(seconds))

    ocz.fetch_zerodha_chain("NIFTY", quote_batch_size=50,
                            quote_spacing_seconds=0)

    assert slept == []


def test_throttled_batch_raises_typed_rate_limit(monkeypatch):
    from src.options_chain import ChainUnavailable

    expiry = future_expiry()
    adapter = FakeAdapter([expiry], fail_after=0)
    install(monkeypatch, adapter)

    with pytest.raises(ChainUnavailable) as excinfo:
        ocz.fetch_zerodha_chain("NIFTY", raise_errors=True)

    assert excinfo.value.reason == "rate_limited"
    assert excinfo.value.provider == "zerodha"


def test_throttled_batch_returns_none_for_direct_callers(monkeypatch):
    expiry = future_expiry()
    adapter = FakeAdapter([expiry], fail_after=0)
    install(monkeypatch, adapter)

    assert ocz.fetch_zerodha_chain("NIFTY") is None


def test_missing_spot_aborts_before_quoting(monkeypatch):
    adapter = FakeAdapter([future_expiry()])
    adapter.quote = lambda exchange, symbols: (
        {"NSE:NIFTY 50": {"last_price": 0.0}} if exchange == "NSE" else {})
    install(monkeypatch, adapter)

    assert ocz.fetch_zerodha_chain("NIFTY") is None


@pytest.mark.parametrize("configured,expected", [
    (None, 100),      # documented default
    (0, 1),           # clamped up
    (50, 50),
    (5000, 500),      # Kite maximum
    ("broken", 100),  # invalid -> default
])
def test_quote_batch_size_is_clamped(configured, expected):
    assert ocz._quote_batch_size(configured) == expected


@pytest.mark.parametrize("configured,expected", [
    (None, 0.25),
    (0, 0.0),
    (1.5, 1.5),
    ("broken", 0.25),
    (-3, 0.25),
])
def test_quote_spacing_is_validated(configured, expected, monkeypatch):
    monkeypatch.delenv("TRIO_ZERODHA_QUOTE_SPACING", raising=False)
    assert ocz._quote_spacing_seconds(configured) == expected


@pytest.mark.parametrize("message", [
    "Too many requests", "429 Too Many Requests", "rate limit exceeded",
    "Status429",
])
def test_rate_limit_classifier_accepts_kite_wording(message):
    assert ocz._is_rate_limited(RuntimeError(message))


def test_rate_limit_classifier_rejects_other_errors():
    assert not ocz._is_rate_limited(KeyError("tradingsymbol"))
