"""Offline tests for the read-only Zerodha market-data boundary."""

from datetime import date

from src.zerodha_market_data import ZerodhaMarketData


class _FakeKite:
    def profile(self):
        return {"user_id": "AB1234", "user_name": "Test", "broker": "ZERODHA",
                "exchanges": ["NSE", "NFO"], "access_token": "must-not-return"}

    def instruments(self, exchange):
        if exchange == "NSE":
            return [{"instrument_token": 256265, "tradingsymbol": "NIFTY 50",
                     "name": "NIFTY 50", "instrument_type": "EQ"},
                    {"instrument_token": 123, "tradingsymbol": "RELIANCE",
                     "name": "RELIANCE", "instrument_type": "EQ"}]
        return [{"instrument_token": 9001, "tradingsymbol": "NIFTY26OCT100CE",
                 "name": "NIFTY", "expiry": date(2026, 10, 29),
                 "strike": 100, "instrument_type": "CE"}]

    def historical_data(self, token, start, end, interval):
        assert token == 256265
        assert interval == "5minute"
        return [{"date": "2026-10-15T09:15:00+05:30", "open": 100,
                 "high": 101, "low": 99, "close": 100.5, "volume": 1000}]

    def quote(self, keys):
        assert keys == ["NFO:NIFTY26OCT100CE"]
        return {keys[0]: {
            "last_price": 10.0, "oi": 5000, "volume": 1000,
            "depth": {"buy": [{"price": 9.8}],
                      "sell": [{"price": 10.0}]},
        }}


def test_zerodha_adapter_is_read_only_and_normalizes_identity():
    adapter = ZerodhaMarketData(kite=_FakeKite())

    profile = adapter.profile()
    assert profile["user_id"] == "AB1234"
    assert "access_token" not in profile
    assert adapter.find_instrument("NSE", "NIFTY 50")["instrument_token"] == 256265
    assert adapter.historical_symbol("^NSEI", "5m")[0]["close"] == 100.5


def test_zerodha_option_identity_and_quote_are_read_only():
    adapter = ZerodhaMarketData(kite=_FakeKite())
    instrument = adapter.find_option("NIFTY", date(2026, 10, 29), 100, "CE")
    quote = adapter.option_quote(instrument)

    assert instrument["instrument_token"] == 9001
    assert quote["bid"] == 9.8
    assert quote["ask"] == 10.0
    assert quote["open_interest"] == 5000
