"""Offline discovery acceptance tests; no orders or live provider requests."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import socket
import sys

import pandas as pd
import pytest

from src import data_fetcher
from src import options_live_scan as scan


IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 10, 14, 9, 35, tzinfo=IST)
EXPIRY = "16-Oct-2026"
_LIVE_METADATA_HOOK = scan.option_instruments
_BATCH_HOOK = scan.fetch_multiple


def _cfg(symbols=("NIFTY",)):
    return {
        "options_intraday": {
            "scan_underlyings": list(symbols), "strategies": ["orb"],
            "min_oi": 2000, "max_spread_pct": 3.0,
            "max_daily_loss_pct": 0.7, "risk_per_trade_pct": 0.25,
            "max_open_positions": 2,
        },
        "options": {"underlying": "NIFTY", "lot_size": 65, "min_oi": 3000},
        "risk_management": {"capital": 500000, "max_daily_loss_pct": 1.0},
        "demand_supply": {"enabled": False},
    }


def _candles(symbol="NIFTY", *, breakout=True):
    instrument_type = "index" if symbol in scan.INDEX_MARKET_SYMBOLS else "stock"
    return [
        {"timestamp": f"2026-10-14T{stamp}:00+05:30", "open": 100,
         "high": high, "low": low, "close": close, "volume": 1000,
         "underlying_symbol": symbol, "instrument_type": instrument_type,
         "exchange": "NSE"}
        for stamp, high, low, close in [
            ("09:15", 101, 99, 100), ("09:20", 101, 99, 100),
            ("09:25", 101, 99, 100),
            ("09:30", 103 if breakout else 101, 99, 102 if breakout else 100),
        ]
    ]


def _metadata(symbol="NIFTY", *, lot=17, expiry=EXPIRY, strike=100, kind="CE"):
    return {
        "instrumentName": symbol,
        "instrumentType": "OPTIDX" if symbol in scan.INDEX_MARKET_SYMBOLS else "OPTSTK",
        "tradingSymbol": f"{symbol}26OCT{strike}{kind}", "lotSize": lot,
        "expiry": expiry, "strike": strike, "exchange": "NSE",
        "instrumentToken": f"{symbol}-{expiry}-{strike}-{kind}",
    }


def _chain(symbol="NIFTY", *, timestamp="2026-10-14T09:34:00+05:30"):
    quote = {"bidPrice": 9.8, "askPrice": 10.0, "openInterest": 5000,
             "volume": 1000, "quote_timestamp": timestamp, "lot_size": 999}
    return {"symbol": symbol, "underlying": 102, "expiries": [EXPIRY],
            "retrieved_at": NOW.isoformat(), "source": "test",
            "by_expiry": {EXPIRY: [
                {"strike": 100, "expiry": EXPIRY, "underlying_symbol": symbol,
                 "exchange": "NSE", "CE": deepcopy(quote), "PE": deepcopy(quote)}
            ]}}


def _candidate(symbol, score=100, *, strike=100, kind="CE", lot=17):
    return {"symbol": symbol, "underlying": symbol, "signal_underlying": symbol,
            "option_underlying": symbol, "rank_score": score,
            "option_legs": {"underlying": symbol, "lot_size": lot, "expiry": EXPIRY,
                            "legs": [{"strike": strike, "kind": kind, "side": "BUY"}]}}


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("unexpected real network/provider/order operation")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(scan, "fetch_multiple", forbidden)
    monkeypatch.setattr(scan, "fetch_chain", forbidden)
    monkeypatch.setattr(scan, "option_instruments", forbidden)
    monkeypatch.delenv("TRIO_RESEARCH_STRATEGIES", raising=False)
    monkeypatch.delenv("TRIO_DEMAND_SUPPLY", raising=False)
    monkeypatch.delenv("TRIO_INTRADAY_DATA_PATH", raising=False)
    monkeypatch.delenv("TRIO_MARKET_DATA_PROVIDER", raising=False)


@pytest.fixture
def signal_boundary(monkeypatch):
    """Control signals while retaining real identity/completion validation."""
    calls = []

    def evaluate(candles, strategy, *, underlying_symbol, contract_specs, index):
        calls.append((underlying_symbol, deepcopy(candles), deepcopy(contract_specs)))
        assert index == len(candles) - 1
        assert all(row["underlying_symbol"] == underlying_symbol for row in candles)
        assert all(datetime.fromisoformat(row["timestamp"]) + timedelta(minutes=5) <= NOW
                   for row in candles)
        return [object()] if candles[-1]["close"] > 101 else []

    monkeypatch.setattr(scan.CandidatePipeline, "evaluate_strategy_signals_pure", evaluate)
    return calls


@pytest.mark.parametrize("basket,stocks", [("nifty50", 50), ("nifty100", 100)])
def test_basket_unions_configured_indices_and_deduplicates_stocks(basket, stocks):
    cfg = {"options_intraday": {"scan_basket": basket},
           "instrument_universe": [{"symbol": "reliance.ns", "type": "stock"},
                                   {"symbol": "NIFTY", "type": "index"},
                                   {"symbol": "CUSTOM.NS", "type": "stock"}]}
    universe = scan.resolve_scan_universe(cfg)
    assert len(universe) == stocks + 4 + 1
    assert universe.count("RELIANCE") == 1
    assert universe.count("NIFTY") == 1
    assert {"NIFTY", "BANKNIFTY", "FINNIFTY", "NIFTYNXT50", "CUSTOM"} <= set(universe)
    assert all(not symbol.endswith(".NS") for symbol in universe)


def test_default_is_all_100_stocks_plus_indices_and_explicit_universe_overrides():
    universe = scan.resolve_scan_universe({})
    assert len(universe) == 104
    assert len(set(universe)) == 104
    assert scan.resolve_scan_universe(_cfg(["reliance.ns", "RELIANCE", "^NSEI", "NIFTY.NS"])) == [
        "RELIANCE", "NIFTY"]


def test_all_100_stocks_are_fetched_in_one_short_5m_batch_even_if_not_listed(monkeypatch):
    calls = []
    cfg = _cfg()
    cfg["options_intraday"].pop("scan_underlyings")
    # This regression explicitly exercises the wider opt-in basket; the
    # repository's live overlay defaults to the safer NIFTY50 scan.
    cfg["options_intraday"]["scan_basket"] = "nifty100"

    def batch(symbols, *, timeframe, period):
        calls.append((symbols, timeframe, period))
        return {symbol: _candles(scan._symbol(symbol)) for symbol in symbols}

    monkeypatch.setattr(data_fetcher, "fetch_multiple", batch)
    # Use the production late-bound batch hook, not the test network guard.
    monkeypatch.setattr(scan, "fetch_multiple", _BATCH_HOOK)
    result = scan.build_live_options_scan(cfg, now=NOW, instrument_rows=[])

    assert len(calls) == 1
    symbols, timeframe, period = calls[0]
    assert len(symbols) == 104
    assert len([symbol for symbol in symbols if scan._symbol(symbol) not in scan.INDEX_MARKET_SYMBOLS]) == 100
    assert timeframe == "5m" and period == "5d"
    assert result["diagnostics"]["requested"] == 104
    assert result["diagnostics"]["data_ok"] == 104
    assert result["diagnostics"]["not_listed"] == 104
    assert result["snapshots"] == []


def test_fresh_chains_only_for_listed_underlyings_with_a_signal(monkeypatch, signal_boundary):
    symbols = ["RELIANCE", "TCS", "INFY", "HDFCBANK"]
    data = {symbol: _candles(symbol, breakout=symbol != "TCS") for symbol in symbols}
    metadata = [_metadata(symbol) for symbol in symbols if symbol != "INFY"]
    metadata[-1]["expiry"] = "13-Oct-2026"  # expired HDFCBANK listing
    chain_calls, plans = [], []

    def chain(symbol, *, use_cache):
        chain_calls.append((symbol, use_cache))
        return _chain(symbol)

    def plan(cfg, *, now, candles, chain):
        symbol = cfg["options_intraday"]["underlying"]
        plans.append((symbol, deepcopy(cfg), deepcopy(chain)))
        return {"option_candidates": [_candidate(symbol)], "option_chain": chain}

    monkeypatch.setattr(scan.planner, "build_intraday_plan", plan)
    result = scan.build_live_options_scan(_cfg(symbols), now=NOW, instrument_rows=metadata,
                                         market_data=data, chain_provider=chain)
    assert chain_calls == [("RELIANCE", False)]
    assert [call[0] for call in signal_boundary] == ["RELIANCE", "TCS"]
    assert result["approved_underlyings"] == ["RELIANCE", "TCS"]
    assert result["diagnostics"]["data_ok"] == 4
    assert result["diagnostics"]["no_signal"] == 1
    assert result["diagnostics"]["not_listed"] == 2
    assert result["diagnostics"]["selected"] == 1
    assert plans[0][0] == "RELIANCE"
    assert plans[0][1]["options_intraday"]["instrument_type"] == "stock"
    assert plans[0][1]["options_intraday"]["market_symbol"] == "RELIANCE.NS"


@pytest.mark.parametrize("bad", ["old_date", "stale", "gap", "nan", "wrong_underlying", "wrong_type"])
def test_invalid_or_stale_data_never_yields_a_snapshot_or_chain_request(bad):
    candles = _candles()
    now = NOW
    if bad == "old_date":
        for row in candles:
            row["timestamp"] = row["timestamp"].replace("2026-10-14", "2026-10-13")
    elif bad == "stale":
        now += timedelta(minutes=16)
    elif bad == "gap":
        candles.pop(1)
    elif bad == "nan":
        candles[-1]["high"] = float("nan")
    elif bad == "wrong_underlying":
        candles[-1]["underlying_symbol"] = "RELIANCE"
    else:
        candles[-1]["instrument_type"] = "stock"
    result = scan.build_live_options_scan(_cfg(), now=now, instrument_rows=[_metadata()],
                                         market_data={"NIFTY": candles})
    assert result["snapshots"] == []
    assert result["diagnostics"]["data_ok"] == 0
    assert result["diagnostics"]["selected"] == 0
    assert any(row["stage"] == "candles" and row["reason"] == "invalid_or_stale_intraday_data"
               for row in result["diagnostics"]["rejections"])


def test_only_completed_bars_reach_signal_and_planner(monkeypatch, signal_boundary):
    rows = _candles()
    forming = deepcopy(rows[-1])
    forming.update(timestamp=NOW.isoformat(), close=10000, high=10001)
    rows.append(forming)
    received = []

    def plan(cfg, *, now, candles, chain):
        received.extend(candles)
        return {"option_candidates": [_candidate("NIFTY")]}

    monkeypatch.setattr(scan.planner, "build_intraday_plan", plan)
    result = scan.build_live_options_scan(_cfg(), now=NOW, instrument_rows=[_metadata()],
                                         market_data={"NIFTY": rows},
                                         chain_provider=lambda *args, **kwargs: _chain())
    assert result["diagnostics"]["selected"] == 1
    assert len(received) == 4
    assert received[-1]["close"] == 102
    assert len(signal_boundary[0][1]) == 4


@pytest.mark.parametrize("provider_stage", ["candles", "metadata", "chain"])
def test_provider_exception_never_falls_back_to_default_or_saved_plan(monkeypatch, signal_boundary, provider_stage):
    def broken(*args, **kwargs):
        raise RuntimeError(f"{provider_stage} down")

    monkeypatch.setattr(scan.planner, "build_intraday_plan",
                        lambda *args, **kwargs: pytest.fail("planner fallback called"))
    inputs = {"market_data": {"NIFTY": _candles()}, "instrument_rows": [_metadata()],
              "chain_provider": lambda *args, **kwargs: _chain()}
    inputs[{"candles": "market_data", "metadata": "instrument_rows", "chain": "chain_provider"}[provider_stage]] = broken
    result = scan.build_live_options_scan(_cfg(), now=NOW, **inputs)
    assert result["snapshots"] == []
    assert result["diagnostics"]["fetch_errors"] >= 1
    assert any(f"{provider_stage} down" in row["reason"] for row in result["diagnostics"]["rejections"])


def test_missing_injected_symbol_is_not_replaced_by_an_index_view(signal_boundary):
    result = scan.build_live_options_scan(_cfg(["RELIANCE"]), now=NOW,
                                         instrument_rows=[_metadata("RELIANCE")],
                                         market_data={"NIFTY": _candles()})
    assert result["snapshots"] == []
    assert result["diagnostics"]["fetch_errors"] == 1
    assert not signal_boundary


def test_wrong_market_container_identity_is_rejected_before_normalising(signal_boundary):
    data = SimpleNamespace(symbol="NIFTY", timeframe="5m", ohlcv=_candles("RELIANCE"))
    result = scan.build_live_options_scan(_cfg(["RELIANCE"]), now=NOW,
                                         instrument_rows=[_metadata("RELIANCE")],
                                         market_data={"RELIANCE.NS": data})
    assert result["snapshots"] == []
    assert result["diagnostics"]["rejections"][0]["reason"] == "market_data_underlying_mismatch"
    assert not signal_boundary


@pytest.mark.parametrize("mutation", [
    {"expiry": "13-Oct-2026"}, {"expiry": "invalid"}, {"instrumentType": "FUTSTK"},
    {"exchange": "BSE"}, {"lotSize": 0}, {"lotSize": 1.5}, {"instrumentToken": ""},
    {"tradingSymbol": "RELIANCE"}, {"option_type": "PE"},
    {"valid_until": "2026-10-13"}, {"valid_from": "2026-10-15"},
])
def test_incomplete_expired_or_non_option_metadata_is_not_listing_approval(mutation):
    metadata = {**_metadata("RELIANCE"), **mutation}
    result = scan.build_live_options_scan(_cfg(["RELIANCE"]), now=NOW,
                                         instrument_rows=[metadata],
                                         market_data={"RELIANCE": _candles("RELIANCE")})
    assert result["approved_underlyings"] == []
    assert result["diagnostics"]["not_listed"] == 1
    assert result["diagnostics"]["metadata_rejections"]
    assert result["snapshots"] == []


def test_today_expiry_is_current_but_conflicting_metadata_is_never_guessed():
    row = _metadata("RELIANCE", expiry=NOW.date())
    cfg = _cfg(["RELIANCE"])
    data = {"RELIANCE": _candles("RELIANCE", breakout=False)}
    result = scan.build_live_options_scan(cfg, now=NOW, instrument_rows=[row], market_data=data)
    assert result["approved_underlyings"] == ["RELIANCE"]
    result = scan.build_live_options_scan(cfg, now=NOW,
                                         instrument_rows=[row, {**row, "lotSize": 19}], market_data=data)
    assert result["approved_underlyings"] == []
    assert result["diagnostics"]["metadata_rejections"] == {"conflicting_contract_metadata": 1}


def test_exact_lots_are_enriched_by_expiry_strike_and_side(monkeypatch, signal_boundary):
    metadata = [_metadata("RELIANCE", lot=17),
                _metadata("RELIANCE", lot=19, kind="PE"),
                _metadata("RELIANCE", lot=23, strike=105),
                _metadata("RELIANCE", lot=29, expiry="23-Oct-2026")]
    chain = _chain("RELIANCE")
    chain["expiries"].append("23-Oct-2026")
    extra = deepcopy(chain["by_expiry"][EXPIRY][0])
    extra["strike"] = 105
    chain["by_expiry"][EXPIRY].append(extra)
    extra = deepcopy(chain["by_expiry"][EXPIRY][0])
    extra["expiry"] = "23-Oct-2026"
    chain["by_expiry"]["23-Oct-2026"] = [extra]
    original = deepcopy(chain)
    captured = []

    def plan(cfg, *, now, candles, chain):
        captured.append(chain)
        return {"option_candidates": []}

    monkeypatch.setattr(scan.planner, "build_intraday_plan", plan)
    result = scan.build_live_options_scan(_cfg(["RELIANCE"]), now=NOW, instrument_rows=metadata,
                                         market_data={"RELIANCE": _candles("RELIANCE")},
                                         chain_provider=lambda *args, **kwargs: chain)
    by_expiry = captured[0]["by_expiry"]
    assert by_expiry[EXPIRY][0]["CE"]["lot_size"] == 17
    assert by_expiry[EXPIRY][0]["PE"]["lot_size"] == 19
    assert by_expiry[EXPIRY][1]["CE"]["lot_size"] == 23
    assert "PE" not in by_expiry[EXPIRY][1]
    assert by_expiry["23-Oct-2026"][0]["CE"]["lot_size"] == 29
    assert "PE" not in by_expiry["23-Oct-2026"][0]
    assert chain == original
    assert result["diagnostics"]["contract_rejection"] == 1


def test_zerodha_normalized_current_rows_are_supported(monkeypatch, signal_boundary):
    row = {"name": "RELIANCE", "instrument_type": "CE", "segment": "NFO-OPT",
           "exchange": "NFO", "tradingsymbol": "RELIANCE26OCT100CE", "lot_size": 31,
           "expiry": NOW.date() + timedelta(days=2), "strike": 100, "instrument_token": 123}
    received = []

    def plan(cfg, *, now, candles, chain):
        received.append(chain["by_expiry"][EXPIRY][0]["CE"])
        return {"option_candidates": [_candidate("RELIANCE", lot=31)]}

    monkeypatch.setattr(scan.planner, "build_intraday_plan", plan)
    result = scan.build_live_options_scan(_cfg(["RELIANCE"]), now=NOW, instrument_rows=[row],
                                         market_data={"RELIANCE": _candles("RELIANCE")},
                                         chain_provider=lambda *args, **kwargs: _chain("RELIANCE"))
    assert result["diagnostics"]["selected"] == 1
    assert received[0]["instrument_token"] == 123
    assert received[0]["lot_size"] == 31


def test_megabull_display_name_resolves_only_the_actual_stock_root(monkeypatch, signal_boundary):
    row = _metadata("RELIANCE")
    row["instrumentName"] = "RELIANCE 16 OCT 100 CE"
    monkeypatch.setattr(scan.planner, "build_intraday_plan", lambda *args, **kwargs: {
        "option_candidates": [_candidate("RELIANCE")]})
    result = scan.build_live_options_scan(_cfg(["RELIANCE", "NIFTY"]), now=NOW,
                                         instrument_rows=[row],
                                         market_data={"RELIANCE": _candles("RELIANCE"), "NIFTY": _candles()},
                                         chain_provider=lambda symbol, **kwargs: _chain(symbol))
    assert result["approved_underlyings"] == ["RELIANCE"]
    assert result["snapshots"][0]["option_candidates"][0]["symbol"] == "RELIANCE"


def test_explicit_empty_universe_has_no_provider_calls():
    result = scan.build_live_options_scan(_cfg([]), now=NOW)
    assert result["snapshots"] == []
    assert result["diagnostics"]["requested"] == 0


def test_default_metadata_hook_uses_only_public_option_instruments(monkeypatch):
    from src.broker import megabull
    calls = []
    fake = SimpleNamespace(option_instruments=lambda: calls.append("metadata") or [_metadata()])
    monkeypatch.setattr(megabull, "MegaBullBroker", lambda: fake)
    monkeypatch.setattr(scan, "option_instruments", _LIVE_METADATA_HOOK)
    result = scan.build_live_options_scan(_cfg(), now=NOW,
                                         market_data={"NIFTY": _candles(breakout=False)})
    assert calls == ["metadata"]
    assert result["approved_underlyings"] == ["NIFTY"]


@pytest.mark.parametrize("bad", ["wrong_symbol", "wrong_row", "unlisted_strike", "cached", "wrong_candidate"])
def test_wrong_chain_or_planner_identity_is_never_mapped_to_stock(monkeypatch, signal_boundary, bad):
    chain = _chain("RELIANCE")
    if bad == "wrong_symbol":
        chain["symbol"] = "NIFTY"
    elif bad == "wrong_row":
        chain["by_expiry"][EXPIRY][0]["underlying_symbol"] = "NIFTY"
    elif bad == "unlisted_strike":
        chain["by_expiry"][EXPIRY][0]["strike"] = 200
    elif bad == "cached":
        chain["retrieval"] = {"served_from_cache": True}
    monkeypatch.setattr(scan.planner, "build_intraday_plan", lambda *args, **kwargs: {
        "option_candidates": [_candidate("NIFTY" if bad == "wrong_candidate" else "RELIANCE")]})
    result = scan.build_live_options_scan(_cfg(["RELIANCE"]), now=NOW,
                                         instrument_rows=[_metadata("RELIANCE")],
                                         market_data={"RELIANCE": _candles("RELIANCE")},
                                         chain_provider=lambda *args, **kwargs: chain)
    assert result["snapshots"] == []
    assert result["diagnostics"]["contract_rejection"] == 1


def test_all_candidates_ranked_globally_and_same_contract_deduplicated(monkeypatch, signal_boundary):
    symbols = ["RELIANCE", "TCS", "INFY", "SBIN", "LT"]
    scores = dict(zip(symbols, [30, 90, 60, 80, 40]))

    def plan(cfg, *, now, candles, chain):
        symbol = cfg["options_intraday"]["underlying"]
        candidate = _candidate(symbol, scores[symbol])
        return {"option_candidates": [candidate, {**deepcopy(candidate), "rank_score": scores[symbol] - 10}],
                "option_chain": chain}

    monkeypatch.setattr(scan.planner, "build_intraday_plan", plan)
    result = scan.build_live_options_scan(_cfg(symbols), now=NOW,
                                         instrument_rows=[_metadata(symbol) for symbol in symbols],
                                         market_data={symbol: _candles(symbol) for symbol in symbols},
                                         chain_provider=lambda symbol, **kwargs: _chain(symbol))
    candidates = [candidate for snapshot in result["snapshots"] for candidate in snapshot["option_candidates"]]
    assert [candidate["symbol"] for candidate in candidates] == ["TCS", "SBIN", "INFY", "LT", "RELIANCE"]
    assert [candidate["rank"] for candidate in candidates] == [1, 2, 3, 4, 5]
    assert [candidate["rank_score"] for candidate in candidates] == [90, 80, 60, 40, 30]
    assert result["diagnostics"]["selected"] == 5  # no top-three/execution cap
    assert result["diagnostics"]["duplicate_contracts"] == 5


def test_per_underlying_cfg_keeps_risk_filters_and_demand_supply_unchanged(monkeypatch, signal_boundary):
    cfg = _cfg(["RELIANCE"])
    cfg["demand_supply"] = {"enabled": True, "min_score": 0.9}
    cfg["options_intraday"]["contract_specs"] = {"NIFTY": {"lot_size": 65}}
    before = deepcopy(cfg)
    captured = []

    def plan(view, *, now, candles, chain):
        captured.append(deepcopy(view))
        view["risk_management"]["capital"] = 0  # must be a private view
        return {"option_candidates": [_candidate("RELIANCE")]}

    monkeypatch.setattr(scan.planner, "build_intraday_plan", plan)
    result = scan.build_live_options_scan(cfg, now=NOW, instrument_rows=[_metadata("RELIANCE")],
                                         market_data={"RELIANCE": _candles("RELIANCE")},
                                         chain_provider=lambda *args, **kwargs: _chain("RELIANCE"))
    assert result["diagnostics"]["selected"] == 1
    assert cfg == before
    view = captured[0]
    assert view["risk_management"] == cfg["risk_management"]
    assert view["options"] == cfg["options"]
    assert view["demand_supply"] == cfg["demand_supply"]
    for key in ("min_oi", "max_spread_pct", "risk_per_trade_pct", "max_daily_loss_pct", "max_open_positions"):
        assert view["options_intraday"][key] == cfg["options_intraday"][key]
    mode = view["options_intraday"]
    assert mode["underlying"] == "RELIANCE" and mode["underlyings"] == ["RELIANCE"]
    assert mode["contract_specs"]["RELIANCE"]["lot_size"] == 17
    assert mode["contract_specs"]["RELIANCE"]["provenance"]


def test_actual_pure_orb_and_planner_use_current_metadata_without_risk_singleton(monkeypatch):
    import candidate_pipeline

    def forbidden(*args, **kwargs):
        pytest.fail("stateful pipeline/risk singleton consulted")

    monkeypatch.setattr(candidate_pipeline.CandidatePipeline, "__init__", forbidden)
    monkeypatch.setattr(candidate_pipeline, "get_experimental_risk_state", forbidden)
    monkeypatch.setattr(candidate_pipeline, "init_experimental_risk", forbidden)
    result = scan.build_live_options_scan(_cfg(), now=NOW, instrument_rows=[_metadata()],
                                         market_data={"^NSEI": _candles()},
                                         chain_provider=lambda *args, **kwargs: _chain())
    assert result["diagnostics"]["selected"] == 1
    candidate = result["snapshots"][0]["option_candidates"][0]
    assert candidate["underlying"] == "NIFTY"
    assert candidate["option_legs"]["lot_size"] == 17
    assert candidate["signal_timestamp"] == NOW.isoformat()


@pytest.mark.parametrize("timestamp", ["2026-10-14T09:00:00+05:30", None])
def test_actual_planner_rejects_stale_or_missing_exchange_quotes(timestamp):
    result = scan.build_live_options_scan(_cfg(), now=NOW, instrument_rows=[_metadata()],
                                         market_data={"NIFTY": _candles()},
                                         chain_provider=lambda *args, **kwargs: _chain(timestamp=timestamp))
    assert result["snapshots"] == []
    assert result["diagnostics"]["contract_rejection"] == 1
    assert result["diagnostics"]["per_underlying"]["NIFTY"]["planner"]["failure_reason"] == "no_fresh_live_quotes"


def test_fetch_multiple_period_and_intraday_nan_gaps_are_preserved(monkeypatch):
    symbols = [f"STOCK{index}.NS" for index in range(6)]
    frame = pd.DataFrame({"Open": [100, 100], "High": [101, float("nan")],
                          "Low": [99, 99], "Close": [100, 100], "Volume": [1000, float("nan")]},
                         index=pd.date_range("2026-10-14T09:15:00+05:30", periods=2, freq="5min"))
    batch = pd.concat({symbol: frame for symbol in symbols}, axis=1)
    calls = []
    monkeypatch.setattr(data_fetcher, "load_config", lambda: {"data_fetcher": {"source": "yfinance"}})
    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(download=lambda **kwargs: calls.append(kwargs) or batch))
    result = data_fetcher.fetch_multiple(symbols, timeframe="5m", period="5d")
    assert calls[0]["period"] == "5d"
    assert len(result) == 6
    assert pd.isna(result[symbols[0]].ohlcv.iloc[1]["High"])
    assert pd.isna(result[symbols[0]].ohlcv.iloc[1]["Volume"])


def test_fetch_multiple_small_basket_forwards_explicit_period_but_keeps_default(monkeypatch):
    calls = []
    monkeypatch.setattr(data_fetcher, "load_config", lambda: {})
    monkeypatch.setattr(data_fetcher, "fetch_market_data", lambda *args, **kwargs: calls.append((args, kwargs)) or object())
    data_fetcher.fetch_multiple(["RELIANCE.NS"], timeframe="5m", period="5d")
    data_fetcher.fetch_multiple(["RELIANCE.NS"], timeframe="5m")
    assert calls == [(("RELIANCE.NS", "5m"), {"period": "5d"}), (("RELIANCE.NS", "5m"), {})]


def test_single_yfinance_intraday_fetch_does_not_forward_fill(monkeypatch):
    frame = pd.DataFrame({"Open": [100, 100], "High": [101, float("nan")],
                          "Low": [99, 99], "Close": [100, 100], "Volume": [1000, float("nan")]})
    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(
        Ticker=lambda symbol: SimpleNamespace(history=lambda **kwargs: frame.copy())))
    result = data_fetcher._fetch_yfinance("RELIANCE.NS", "5m", "5d")
    assert pd.isna(result.iloc[1]["High"])
    assert pd.isna(result.iloc[1]["Volume"])


def test_slow_scan_checks_quotes_at_observation_time_not_frozen_start(monkeypatch):
    times = iter([0, 180, 180])
    monkeypatch.setattr(scan, "time", SimpleNamespace(monotonic=lambda: next(times)))
    result = scan.build_live_options_scan(
        _cfg(), now=NOW, instrument_rows=[_metadata()],
        market_data={"NIFTY": _candles()},
        chain_provider=lambda *args, **kwargs: _chain(timestamp="2026-10-14T09:37:00+05:30"))
    assert result["diagnostics"]["selected"] == 1
    candidate = result["snapshots"][0]["option_candidates"][0]
    assert candidate["option_diagnostic"]["quote_age_seconds"] == 60
