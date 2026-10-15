"""Production-entry-point wiring tests for the explicit options mode."""

def test_paper_trader_options_mode_delegates_to_strict_runtime(monkeypatch):
    import src.paper_trader as paper_trader

    calls = []

    class RuntimeSpy:
        def execute_plan(self, plan, opt_broker=None):
            calls.append((plan, opt_broker))
            return [{"order_id": "remote-1", "asset": "options"}]

    trader = object.__new__(paper_trader.PaperTrader)
    trader.options_only = True
    trader.opt_broker = object()
    trader._options_runtime = RuntimeSpy()
    trader._last_option_order_ids = []

    monkeypatch.setattr(paper_trader, "load_config", lambda: {
        "execution": {"remote_paper_required": True},
        "broker": {"provider": "megabull"},
    })

    plan = {"trading_mode": "options_intraday", "option_candidates": []}
    placed = trader.execute_option_plan(plan, opt_broker=trader.opt_broker)

    assert placed == [{"order_id": "remote-1", "asset": "options"}]
    assert calls == [(plan, trader.opt_broker)]
    assert trader._last_option_order_ids == ["remote-1"]


def test_options_only_equity_execute_path_is_a_noop(monkeypatch):
    import src.paper_trader as paper_trader

    trader = object.__new__(paper_trader.PaperTrader)
    trader.options_only = True
    events = []
    trader._log_event = lambda event, payload: events.append((event, payload))

    placed = trader.execute_plan({"candidates": [{
        "symbol": "RELIANCE.NS", "action": "BUY",
    }]})

    assert placed == []
    assert events == [("skip", {
        "symbol": "RELIANCE.NS",
        "asset": "equity",
        "reason": "options-only-mode",
    })]


def test_combined_mode_sends_only_nested_strict_plan_to_runtime(monkeypatch):
    import src.paper_trader as paper_trader

    calls = []

    class RuntimeSpy:
        def execute_plan(self, plan, opt_broker=None):
            calls.append(plan)
            return []

    trader = object.__new__(paper_trader.PaperTrader)
    trader.options_only = False
    trader.strict_options_mode = True
    trader.combined_mode = True
    trader.opt_broker = object()
    trader._options_runtime = RuntimeSpy()
    trader._last_option_order_ids = []
    monkeypatch.setattr(paper_trader, "load_config", lambda: {
        "execution": {"remote_paper_required": True},
        "broker": {"provider": "megabull"},
    })

    nested = {"trading_mode": "options_intraday", "option_candidates": []}
    trader.execute_option_plan({
        "trading_mode": "combined",
        "candidates": [{"symbol": "RELIANCE.NS"}],
        "options_intraday_plan": nested,
    }, opt_broker=trader.opt_broker)

    assert calls == [nested]


def test_default_mode_does_not_enable_options_runtime(monkeypatch):
    from src.options_intraday import options_intraday_enabled, resolve_trading_mode

    monkeypatch.delenv("TRIO_TRADING_MODE", raising=False)
    assert resolve_trading_mode({}) == "production"
    assert not options_intraday_enabled({})


def test_megabull_instrument_lookup_returns_complete_cached_contract(tmp_path):
    from src.broker.megabull import MegaBullBroker

    cache = tmp_path / "instruments.csv"
    cache.write_text(
        "instrumentToken,tradingSymbol,exchange,instrumentType,lotSize,"
        "expiry,strike,instrumentName\n"
        "12345,NIFTY26O3025000CE,NSE,OPTIDX,65,30-Oct-2026,25000,NIFTY\n",
        encoding="utf-8",
    )
    broker = MegaBullBroker(api_key="test", instrument_cache=cache)

    row = broker.instrument_for("NIFTY26O3025000CE")

    assert row["instrumentToken"] == "12345"
    assert row["lotSize"] == "65"
    assert row["expiry"] == "30-Oct-2026"


def test_options_premarket_artifact_is_next_session_preview(monkeypatch):
    from scripts import premarket

    monkeypatch.setattr(premarket, "next_trading_day_ist_str",
                        lambda: "2026-10-16")
    prepared = premarket.prepare_plan_for_premarket({
        "date": "2026-10-15",
        "trading_mode": "options_intraday",
        "option_candidates": [{"signal_timestamp": "2026-10-15T09:35:00+05:30"}],
    })

    assert prepared["date"] == "2026-10-16"
    assert prepared["execution_ready"] is False
    assert "rebuilt" in prepared["execution_blocked_reason"]
