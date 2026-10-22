"""Focused same-loop tests for equity plus strict remote-paper options."""

from types import SimpleNamespace

from src.plan import today_ist_str


class _StrictRuntimeSpy:
    def __init__(self):
        self._state = {"positions": {}}
        self.ticks = []
        self.finalizes = []

    def tick(self, now=None):
        self.ticks.append(now)
        return []

    def finalize(self, now=None):
        self.finalizes.append(now)
        return []

    def risk_positions(self):
        return {}


def _combined_trader(monkeypatch):
    from src.paper_trader import PaperTrader
    import src.execution_lifecycle as lifecycle

    trader = object.__new__(PaperTrader)
    trader.options_only = False
    trader.combined_mode = True
    trader.strict_options_mode = True
    trader.trading_mode = "combined"
    trader.broker = SimpleNamespace(positions={}, provider_name="paper")
    trader.opt_broker = object()
    trader._options_runtime = _StrictRuntimeSpy()
    trader._options_unresolved_exits = {}
    trader._last_plan_order_ids = []
    trader._last_option_order_ids = []
    trader.trade_log_path = None
    trader.events = []
    trader._log_event = lambda event, payload: trader.events.append((event, payload))
    trader._write_trade_log_csv = lambda: None
    trader.ensure_option_broker = lambda cfg=None: trader.opt_broker
    trader._get_options_runtime = lambda cfg=None: trader._options_runtime
    trader._session_open = lambda now=None: True
    trader._market_still_running = lambda now=None: False
    trader._maybe_eod_square_off = lambda now_ist=None: []
    trader._guard_open_positions = lambda: trader.events.append(("equity_guard", {}))
    trader._manage_open_positions = lambda: trader.events.append(("equity_manage", {}))
    # These tests own only the same-tick entry/exit handoff, not live discovery.
    trader._live_rescan = lambda slot: []
    trader.execute_plan = lambda plan: trader.events.append(("equity_entry", plan)) or []
    trader.execute_option_plan = lambda plan, opt_broker=None: (
        trader.events.append(("options_entry", plan)) or [])
    monkeypatch.setattr(lifecycle, "reconcile_broker", lambda _trader: {})
    monkeypatch.setattr("src.alerts.handle_all_joins", lambda: [])
    monkeypatch.setattr("src.alerts.send_telegram", lambda *args: None)
    monkeypatch.setattr("src.alerts.send_options_telegram", lambda *args: None)
    return trader


def test_combined_session_runs_both_entry_and_exit_lifecycles(monkeypatch):
    trader = _combined_trader(monkeypatch)
    plan = {
        "trading_mode": "combined",
        "candidates": [{"symbol": "RELIANCE.NS"}],
        "options_intraday_plan": {
            "trading_mode": "options_intraday",
            "date": today_ist_str(),
            "option_candidates": [],
        },
    }

    trader.run_session(plan=plan, interval_seconds=1, max_seconds=None)

    names = [event for event, _ in trader.events]
    assert "equity_entry" in names
    assert "options_entry" in names
    assert "equity_guard" in names
    assert "equity_manage" in names
    assert len(trader._options_runtime.ticks) == 1
    assert len(trader._options_runtime.finalizes) == 1


def test_combined_runtime_does_not_use_legacy_option_tick_hook(monkeypatch):
    trader = _combined_trader(monkeypatch)
    hook_calls = []
    plan = {
        "trading_mode": "combined",
        "candidates": [],
        "options_intraday_plan": {
            "trading_mode": "options_intraday",
            "date": today_ist_str(),
            "option_candidates": [],
        },
    }

    trader.run_session(
        plan=plan,
        interval_seconds=1,
        tick_hook=lambda: hook_calls.append(True),
    )

    assert hook_calls == []
