"""Focused offline tests for the read-only Zerodha signal artifact."""

from datetime import date, datetime, timedelta, timezone
import json

from src.zerodha_signal_capture import (
    NOT_READY,
    READY,
    capture_zerodha_signals,
)


IST = timezone(timedelta(hours=5, minutes=30))
OBSERVED = datetime(2026, 10, 15, 10, 0, tzinfo=IST)


def _instrument(token, symbol, underlying, option_type, strike, *, lot_size=65, expiry=date(2026, 10, 29)):
    return {
        "instrument_token": token,
        "tradingsymbol": symbol,
        "name": underlying,
        "expiry": expiry,
        "strike": strike,
        "lot_size": lot_size,
        "instrument_type": option_type,
        "exchange": "NFO",
        "segment": "NFO-OPT",
    }


class FakeMarketData:
    provider_name = "zerodha"

    def __init__(self):
        self.quote_calls = []
        self.rows = [
            _instrument("1", "NIFTY26OCT25000CE", "NIFTY", "CE", 25_000),
            _instrument("2", "NIFTY26OCT25000PE", "NIFTY", "PE", 25_000),
            _instrument("3", "RELIANCE26OCT2500CE", "RELIANCE", "CE", 2_500),
            _instrument("4", "RELIANCE26OCT2500PE", "RELIANCE", "PE", 2_500),
            # This must never become a signal: futures are not option rows.
            {**_instrument("5", "NIFTY26OCTFUT", "NIFTY", "FUT", 25_000)},
        ]

    def instruments(self, exchange):
        assert exchange == "NFO"
        return list(self.rows)

    def historical_symbol(self, symbol, timeframe, period):
        assert timeframe == "5m"
        assert period == "30d"
        # The 09:55 bar is completed at 10:00.  The 10:00 bar is forming and
        # must not be sent to the evaluator.
        close = 25_000 if symbol == "NIFTY" else 2_500
        return [
            {
                "date": "2026-10-15T09:55:00+05:30",
                "open": close - 10,
                "high": close + 10,
                "low": close - 20,
                "close": close,
                "volume": 1000,
            },
            {
                "date": "2026-10-15T10:00:00+05:30",
                "open": close,
                "high": close + 10,
                "low": close - 10,
                "close": close + 1,
                "volume": 1000,
            },
        ]

    def quote(self, exchange, symbols):
        assert exchange == "NFO"
        self.quote_calls.append(list(symbols))
        symbol = symbols[0]
        return {
            f"NFO:{symbol}": {
                "timestamp": "2026-10-15T10:00:00+05:30",
                "depth": {
                    "buy": [{"price": 100.0, "quantity": 5}],
                    "sell": [{"price": 101.0, "quantity": 5}],
                },
            }
        }


def _evaluator(candles, strategy_name, underlying):
    assert all(candle["underlying_symbol"] == underlying for candle in candles)
    timestamp = candles[-1]["timestamp"]
    side = "PE" if underlying == "RELIANCE" else "CE"
    strike = 2_500 if underlying == "RELIANCE" else 25_000
    return [{
        "signal_id": f"{underlying}-{strategy_name}",
        "signal_timestamp": timestamp,
        "underlying": underlying,
        "option_type": side,
        "underlying_price_at_signal": strike,
        "action": "BUY",
        "completed": True,
    }]


def test_capture_emits_exact_current_identity_and_provenance(tmp_path):
    adapter = FakeMarketData()
    output = tmp_path / "signals.json"
    report = capture_zerodha_signals(
        adapter,
        underlyings=["NIFTY", "RELIANCE"],
        strategy_names=["orb"],
        evaluator=_evaluator,
        as_of=OBSERVED,
        output_path=output,
    )

    assert report["status"] == READY
    assert report["orders_placed"] == 0
    assert len(report["signals"]) == 2
    assert {row["underlying"] for row in report["signals"]} == {"NIFTY", "RELIANCE"}
    assert {row["option_type"] for row in report["signals"]} == {"CE", "PE"}
    assert all(row["action"] == "BUY" for row in report["signals"])
    assert all(row["trading_symbol"].endswith(("CE", "PE")) for row in report["signals"])
    assert all(row["expiry"] == "2026-10-29" for row in report["signals"])
    assert all(row["lot_size"] == 65 for row in report["signals"])
    assert all(row["provider"] == "zerodha" for row in report["signals"])
    assert all(row["provenance"]["quote"]["observed_at"] == OBSERVED.isoformat() for row in report["signals"])
    assert all(row["quote_snapshot"]["bid"] == 100.0 for row in report["signals"])
    assert report["coverage"]["premium_synthesized"] is False
    assert json.loads(output.read_text(encoding="utf-8"))["signals"] == report["signals"]
    assert all(call[0] != "NIFTY26OCTFUT" for call in adapter.quote_calls)


def test_old_signal_and_missing_executable_quote_are_rejected_without_current_attachment():
    adapter = FakeMarketData()
    adapter.rows = [_instrument("1", "NIFTY26OCT25000CE", "NIFTY", "CE", 25_000)]
    quote_calls = adapter.quote_calls

    def old_signal_evaluator(candles, strategy_name, underlying):
        return [{
            "signal_timestamp": "2026-10-14T10:00:00+05:30",
            "underlying": underlying,
            "option_type": "CE",
            "underlying_price_at_signal": 25_000,
            "action": "BUY",
        }]

    report = capture_zerodha_signals(
        adapter,
        underlyings=["NIFTY"],
        strategies=["orb"],
        evaluator=old_signal_evaluator,
        as_of=OBSERVED,
    )

    assert report["status"] == NOT_READY
    assert report["signals"] == []
    assert report["orders_placed"] == 0
    assert report["diagnostics"]["counts"]["historical_signal_not_capturable_with_current_quote"] == 1
    assert quote_calls == []

    def missing_quote_evaluator(candles, strategy_name, underlying):
        return [{
            "signal_timestamp": candles[-1]["timestamp"],
            "underlying": underlying,
            "option_type": "CE",
            "underlying_price_at_signal": 25_000,
            "action": "BUY",
        }]

    class MissingQuote(FakeMarketData):
        def quote(self, exchange, symbols):
            self.quote_calls.append(list(symbols))
            return {f"NFO:{symbols[0]}": {"timestamp": "2026-10-15T10:00:00+05:30", "depth": {}}}

    missing = MissingQuote()
    missing_report = capture_zerodha_signals(
        missing,
        underlyings=["NIFTY"],
        strategies=["orb"],
        evaluator=missing_quote_evaluator,
        as_of=OBSERVED,
    )
    assert missing_report["status"] == NOT_READY
    assert missing_report["diagnostics"]["counts"]["missing_executable_bid_ask"] == 1
    assert missing_report["signals"] == []


def test_short_and_equity_contracts_never_become_buy_option_signals():
    adapter = FakeMarketData()

    def unsafe_evaluator(candles, strategy_name, underlying):
        return [
            {
                "signal_timestamp": candles[-1]["timestamp"],
                "underlying": underlying,
                "option_type": "PE",
                "underlying_price_at_signal": 25_000,
                "action": "SELL",
            },
            {
                "signal_timestamp": candles[-1]["timestamp"],
                "underlying": underlying,
                "option_type": "EQ",
                "underlying_price_at_signal": 25_000,
                "action": "BUY",
            },
        ]

    report = capture_zerodha_signals(
        adapter,
        underlyings=["NIFTY"],
        strategies=["orb"],
        evaluator=unsafe_evaluator,
        as_of=OBSERVED,
    )
    assert report["status"] == NOT_READY
    assert report["signals"] == []
    assert report["diagnostics"]["counts"]["rejected_non_buy_or_short_signal"] == 1
    assert report["diagnostics"]["counts"]["invalid_option_type"] == 1
    assert report["orders_placed"] == 0


def test_small_provider_clock_skew_is_allowed_but_large_skew_is_rejected():
    adapter = FakeMarketData()

    def evaluator(candles, strategy_name, underlying):
        return [{
            "signal_timestamp": candles[-1]["timestamp"],
            "underlying": underlying,
            "option_type": "CE",
            "underlying_price_at_signal": 25_000,
            "action": "BUY",
        }]

    original_quote = adapter.quote
    def slightly_ahead(exchange, symbols):
        payload = original_quote(exchange, symbols)
        payload[f"NFO:{symbols[0]}"]["timestamp"] = "2026-10-15T10:01:00+05:30"
        return payload
    adapter.quote = slightly_ahead
    accepted = capture_zerodha_signals(
        adapter, underlyings=["NIFTY"], strategy_names=["orb"],
        evaluator=evaluator, as_of=OBSERVED, max_provider_clock_skew_seconds=120)
    assert accepted["status"] == READY
    assert accepted["coverage"]["max_provider_clock_skew_seconds"] == 120.0

    rejected = capture_zerodha_signals(
        adapter, underlyings=["NIFTY"], strategy_names=["orb"],
        evaluator=evaluator, as_of=OBSERVED, max_provider_clock_skew_seconds=30)
    assert rejected["status"] == NOT_READY
    assert rejected["diagnostics"]["counts"]["future_provider_quote"] == 1


def test_demand_supply_filter_is_explicit_and_fail_closed(monkeypatch):
    adapter = FakeMarketData()
    def evaluator(candles, strategy_name, underlying):
        return [{"signal_timestamp": candles[-1]["timestamp"],
                 "underlying": underlying, "option_type": "CE",
                 "underlying_price_at_signal": 25_000, "action": "BUY"}]
    monkeypatch.setattr(
        "src.demand_supply.confirm_demand_supply",
        lambda *args, **kwargs: __import__("src.demand_supply", fromlist=["ZoneConfirmation"])
        .ZoneConfirmation(False, "bullish", reason="test-no-zone"),
    )
    report = capture_zerodha_signals(
        adapter, underlyings=["NIFTY"], strategy_names=["orb"], evaluator=evaluator,
        as_of=OBSERVED, require_demand_supply=True,
    )
    assert report["status"] == NOT_READY
    assert report["diagnostics"]["counts"]["demand_supply_not_confirmed"] == 1


def test_research_strategy_is_explicitly_available_but_not_a_default():
    from src.zerodha_signal_capture import DEFAULT_STRATEGIES, RESEARCH_STRATEGY, SUPPORTED_STRATEGIES
    assert RESEARCH_STRATEGY in SUPPORTED_STRATEGIES
    assert RESEARCH_STRATEGY not in DEFAULT_STRATEGIES
