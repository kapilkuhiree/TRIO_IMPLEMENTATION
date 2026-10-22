"""Stock/index routing and uncached identity boundaries, entirely offline."""

from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from src import options_chain as chain


EXPIRY = "27-Oct-2026"
QUOTE_TIME = "19-Oct-2026 10:00:00"


def payload(symbol="RELIANCE"):
    return {"records": {"underlyingValue": 2500, "expiryDates": [EXPIRY],
                        "timestamp": QUOTE_TIME, "data": [{
        "expiryDate": EXPIRY, "strikePrice": 2500,
        "CE": {"underlying": symbol, "lastPrice": 25, "buyPrice1": 24,
               "sellPrice1": 26, "openInterest": 1000},
        "PE": {"underlying": symbol, "lastPrice": 24, "buyPrice1": 23,
               "sellPrice1": 25, "openInterest": 1100},
    }]}}


class Response:
    def __init__(self, data=None, status=200):
        self.data, self.status_code = data, status

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return deepcopy(self.data)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class Session:
    def __init__(self, data=None, v3_status=200, legacy_status=200, expiries=None):
        self.data = data if data is not None else payload()
        self.v3_status, self.legacy_status = v3_status, legacy_status
        self.expiries = [EXPIRY] if expiries is None else expiries
        self.calls, self.cookies, self.headers = [], {"fake": "cookie"}, {}

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        path = urlsplit(url).path
        if path == "/option-chain":
            return Response()
        if path == "/api/option-chain-contract-info":
            return Response({"expiryDates": self.expiries})
        assert path in {"/api/option-chain-v3", "/api/option-chain-equities",
                        "/api/option-chain-indices"}
        return Response(self.data, self.v3_status if path.endswith("-v3") else self.legacy_status)


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("external HTTP is forbidden")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(chain, "load_config", lambda: {"options": {"chain_cache_minutes": 15}})
    monkeypatch.setattr(chain, "CHAIN_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(chain, "CHAIN_DIAGNOSTIC_DIR", tmp_path / "diagnostics")
    monkeypatch.setattr(chain.time, "sleep", lambda *_: None)
    monkeypatch.setattr(chain, "RETRIES", 0)


def install(monkeypatch, session):
    # Exercise the actual cookie-warmup and expiry-list code with fake requests.
    monkeypatch.setattr(chain.requests, "Session", lambda: session)


@pytest.mark.parametrize("symbol,is_index", [
    ("NIFTY", True), ("BANKNIFTY", True), ("FINNIFTY", True),
    ("NIFTYNXT50", True), ("MIDCPNIFTY", True),
    ("RELIANCE", False), ("tcs.ns", False), ("M&M", False),
])
def test_actual_underlying_routes_to_its_endpoint(monkeypatch, symbol, is_index):
    expected = symbol.upper().removesuffix(".NS")
    session = Session(payload(expected))
    install(monkeypatch, session)

    result = chain.fetch_chain(symbol, use_cache=False)

    assert result is not None
    assert result["symbol"] == expected
    assert result["underlying"] == 2500
    assert result["source"] == "nse"
    assert result["quote_timestamp"] == QUOTE_TIME
    row = result["by_expiry"][EXPIRY][0]
    assert (row["CE"]["bidPrice"], row["CE"]["askPrice"]) == (24, 26)
    assert row["CE"]["quote_timestamp"] == QUOTE_TIME
    assert session.calls[0][0] == chain.NSE_HOME
    for url, kwargs in session.calls[1:]:
        query = parse_qs(urlsplit(url).query)
        assert query["symbol"] == [expected]
        assert kwargs == {"timeout": chain.TIMEOUT}
        if "-v3?" in url:
            assert query["type"] == ["Indices" if is_index else "Equity"]
        if "option-chain-indices" in url:
            assert is_index
        if "option-chain-equities" in url:
            assert not is_index
    if expected == "M&M":
        assert all("symbol=M%26M" in url for url, _ in session.calls[1:])


@pytest.mark.parametrize("symbol,legacy", [("RELIANCE", "equities"), ("NIFTY", "indices")])
def test_legacy_fallback_stays_on_same_underlying(monkeypatch, symbol, legacy):
    session = Session(payload(symbol), v3_status=404)
    install(monkeypatch, session)
    result = chain.fetch_chain(symbol, use_cache=False)
    assert result is not None
    assert session.calls[-1][0] == f"{chain.NSE_BASE}/api/option-chain-{legacy}?symbol={symbol}"
    assert result["symbol"] == symbol


@pytest.mark.parametrize("location", ["header", "records", "row", "CE", "PE"])
@pytest.mark.parametrize("use_cache", [False, True])
def test_explicit_wrong_symbol_is_never_relabelled(monkeypatch, location, use_cache):
    data = payload()
    row = data["records"]["data"][0]
    target = {"header": data, "records": data["records"], "row": row,
              "CE": row["CE"], "PE": row["PE"]}[location]
    target["symbol" if location in {"header", "records"} else "underlying"] = "NIFTY"
    session = Session(data)
    install(monkeypatch, session)
    assert chain.fetch_chain("RELIANCE", use_cache=use_cache) is None
    assert not chain._cache_path("RELIANCE").exists()


@pytest.mark.parametrize("missing_leg", ["CE", "PE", "both"])
def test_strict_quotes_need_identity_for_each_leg(monkeypatch, missing_leg):
    data = payload()
    row = data["records"]["data"][0]
    for side in ("CE", "PE") if missing_leg == "both" else (missing_leg,):
        row[side].pop("underlying")
    install(monkeypatch, Session(data))
    assert chain.fetch_chain("RELIANCE", use_cache=False) is None
    # Legacy/cache-enabled callers retain support for old identity-less NSE shapes.
    assert chain.fetch_chain("RELIANCE", use_cache=True) is not None


@pytest.mark.parametrize("location", ["header", "records", "row"])
def test_matching_header_or_row_can_identify_quotes(monkeypatch, location):
    data = payload()
    row = data["records"]["data"][0]
    for side in ("CE", "PE"):
        row[side].pop("underlying")
    {"header": data, "records": data["records"], "row": row}[location]["symbol"] = "RELIANCE"
    install(monkeypatch, Session(data))
    assert chain.fetch_chain("RELIANCE", use_cache=False)["symbol"] == "RELIANCE"


@pytest.mark.parametrize("failure", ["blocked", "http", "json", "empty", "wrong_identity"])
def test_strict_failure_never_reads_or_returns_existing_cache(monkeypatch, failure):
    cached = {"symbol": "RELIANCE", "source": "synthetic", "by_expiry": {EXPIRY: []}}
    chain._cache_write("RELIANCE", cached)
    data = {"json": ValueError("not JSON"), "empty": {},
            "wrong_identity": payload("NIFTY")}.get(failure, payload())
    status = {"blocked": 403, "http": 503}.get(failure, 200)
    install(monkeypatch, Session(data, v3_status=status, legacy_status=status))

    def forbidden_cache(*args, **kwargs):
        pytest.fail("strict fetch read cached data")

    monkeypatch.setattr(chain, "_cache_fresh", forbidden_cache)
    assert chain.fetch_chain("RELIANCE", use_cache=False) is None


def test_strict_success_ignores_cache_but_cache_enabled_mode_reuses_it(monkeypatch):
    chain._cache_write("RELIANCE", {"symbol": "RELIANCE", "underlying": 1, "source": "old"})
    session = Session()
    install(monkeypatch, session)
    current = chain.fetch_chain("RELIANCE", use_cache=False)
    assert current["source"] == "nse" and current["underlying"] == 2500
    calls = len(session.calls)
    cached = chain.fetch_chain("RELIANCE", use_cache=True)
    assert cached["underlying"] == 2500
    assert cached["retrieval"]["served_from_cache"] is True
    assert len(session.calls) == calls


def test_cache_with_wrong_symbol_is_refetched(monkeypatch):
    chain._cache_write("RELIANCE", {"symbol": "NIFTY", "underlying": 25000})
    install(monkeypatch, Session())
    assert chain.fetch_chain("RELIANCE", use_cache=True)["underlying"] == 2500


def test_no_expiry_info_uses_aggregate_equity_chain_without_guessing(monkeypatch):
    session = Session(expiries=[])
    install(monkeypatch, session)
    result = chain.fetch_chain("RELIANCE", use_cache=False)
    assert result["by_expiry"][EXPIRY]
    assert parse_qs(urlsplit(session.calls[-1][0]).query) == {
        "type": ["Equity"], "symbol": ["RELIANCE"]}
