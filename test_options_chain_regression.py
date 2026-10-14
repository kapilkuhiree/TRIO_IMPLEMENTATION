"""Regression test for per-expiry NSE chain grouping."""

from unittest.mock import patch

import src.options_chain as options_chain


class FakeResponse:
    status_code = 200

    def __init__(self, expiry):
        self.expiry = expiry

    def raise_for_status(self):
        return None

    def json(self):
        # Deliberately omit expiryDate/usable expiryDates on rows.  This is the
        # response shape that previously caused every row to use the first
        # expiry in the cache.
        return {
            "records": {
                "underlyingValue": 23500,
                "expiryDates": ["13-Oct-2026", "19-Oct-2026", "27-Oct-2026"],
                "data": [{
                    "strikePrice": 23500,
                    "CE": {"lastPrice": 10, "buyPrice1": 9.9, "sellPrice1": 10.1,
                           "openInterest": 2000},
                    "PE": {"lastPrice": 10, "buyPrice1": 9.9, "sellPrice1": 10.1,
                           "openInterest": 2000},
                }],
            }
        }


class FakeSession:
    def get(self, url, timeout=None):
        expiry = ""
        if "expiry=" in url:
            expiry = url.split("expiry=", 1)[1]
        return FakeResponse(expiry)


def test_expiry_rows_are_grouped_by_requested_expiry():
    expiries = ["13-Oct-2026", "19-Oct-2026", "27-Oct-2026"]
    with patch.object(options_chain, "_session", return_value=FakeSession()), \
         patch.object(options_chain, "_warm_cookies", return_value=True), \
         patch.object(options_chain, "_expiries_for", return_value=expiries), \
         patch.object(options_chain, "_cache_fresh", return_value=None), \
         patch.object(options_chain, "_cache_write"):
        result = options_chain.fetch_chain("NIFTY", use_cache=True)
    assert result is not None
    assert all(result["by_expiry"].get(exp) for exp in expiries)
    assert result["by_expiry"]["19-Oct-2026"][0]["expiry"] == "19-Oct-2026"


if __name__ == "__main__":
    test_expiry_rows_are_grouped_by_requested_expiry()
    print("PASS: test_expiry_rows_are_grouped_by_requested_expiry")
