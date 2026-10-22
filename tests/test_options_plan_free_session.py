"""Offline orchestration checks; every broker and entry scan is a test double."""

import argparse
import datetime as datetime_module
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.market_clock import IST


def _forbidden(*args, **kwargs):
    pytest.fail("options-only orchestration reached a forbidden plan/equity/network path")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    import src.alerts as alerts
    import src.data_fetcher as data_fetcher
    import src.execution_lifecycle as lifecycle
    import src.paper_trader as paper_trader
    import src.screener as screener
    import requests
    # Import modules with bound provider aliases before installing test doubles;
    # otherwise those aliases would retain a mock after fixture teardown.
    from scripts import premarket, session  # noqa: F401
    import main  # noqa: F401

    monkeypatch.setattr(requests.sessions.Session, "request", _forbidden)
    monkeypatch.setattr(data_fetcher, "fetch_market_data", _forbidden)
    monkeypatch.setattr(data_fetcher, "fetch_multiple", _forbidden)
    monkeypatch.setattr(paper_trader, "fetch_market_data", _forbidden)
    monkeypatch.setattr(screener, "screen", _forbidden)
    monkeypatch.setattr(lifecycle, "submit_candidate", _forbidden)
    monkeypatch.setattr(alerts, "handle_all_joins", lambda: [])
    monkeypatch.setattr(alerts, "send_telegram", lambda *args: None)
    monkeypatch.setattr(alerts, "send_options_telegram", lambda *args: None)
    monkeypatch.delenv("TRIO_TRADING_MODE", raising=False)


@pytest.fixture
def forbid_plans(monkeypatch):
    import main
    import src.plan as plans
    from scripts import premarket, session

    for name in ("load_plan", "save_plan", "mark_executed", "find_latest_plan",
                 "validate_plan_identity"):
        monkeypatch.setattr(plans, name, _forbidden)
    for name in ("load_plan", "mark_executed", "validate_plan_identity",
                 "_refresh_strict_options_subplan"):
        monkeypatch.setattr(session, name, _forbidden)
    monkeypatch.setattr(premarket, "build_plan", _forbidden)
    monkeypatch.setattr("src.options_intraday_plan.build_intraday_plan", _forbidden)
    monkeypatch.setattr(main, "_current_strict_options_plan", _forbidden)


class _Clock:
    def __init__(self, start):
        self.start = start
        self.elapsed = 0.0
        self.sleeps = []

    def now(self):
        return self.start + timedelta(seconds=self.elapsed)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.elapsed += seconds


class _Runtime:
    def __init__(self, clock):
        self.clock = clock
        self._state = {"positions": {}}
        self.scans = []
        self.ticks = []
        self.finalizes = []
        self.scan_action = lambda: None
        self.tick_action = lambda: None
        self.scanning = False

    def scan_and_execute(self, now=None):
        assert not self.scanning, "entry scans must never overlap"
        self.scanning = True
        self.scans.append((self.clock.elapsed, now))
        try:
            self.scan_action()
            return []
        finally:
            self.scanning = False

    def tick(self, now=None):
        self.ticks.append((self.clock.elapsed, now))
        self.tick_action()
        return []

    def finalize(self, now=None):
        self.finalizes.append(self.clock.now())
        return []


def _trader(monkeypatch, *, start=None, duration=600, config=None):
    import src.paper_trader as paper_trader

    clock = _Clock(start or datetime(2026, 10, 19, 11, 5, tzinfo=IST))

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now().astimezone(tz) if tz else clock.now().replace(tzinfo=None)

    monkeypatch.setattr(datetime_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(paper_trader, "time", SimpleNamespace(
        monotonic=lambda: clock.elapsed, sleep=clock.sleep))
    monkeypatch.setattr(paper_trader, "load_config", lambda: config or {})

    trader = object.__new__(paper_trader.PaperTrader)
    trader.options_only = True
    trader.combined_mode = False
    trader.strict_options_mode = True
    trader.trading_mode = "options_intraday"
    trader.broker = SimpleNamespace(positions={}, place_order=_forbidden)
    trader.opt_broker = SimpleNamespace(positions={}, place_order=_forbidden)
    trader._options_runtime = _Runtime(clock)
    trader._options_unresolved_exits = {}
    trader._get_options_runtime = Mock(return_value=trader._options_runtime)
    trader.ensure_option_broker = Mock(return_value=trader.opt_broker)
    trader._market_still_running = lambda now_ist=None: clock.elapsed < duration
    trader.events = []
    trader._log_event = lambda event, payload: trader.events.append((event, payload))
    trader._write_trade_log_csv = Mock()
    for name in ("execute_plan", "execute_option_plan", "_live_rescan", "scan_once",
                 "rehydrate_orders", "_guard_open_positions", "_manage_open_positions",
                 "_maybe_eod_square_off"):
        setattr(trader, name, _forbidden)
    return trader, clock, trader._options_runtime


def test_startup_without_plan_waits_for_can_enter_then_scans_on_configured_interval(
        monkeypatch, forbid_plans):
    trader, clock, runtime = _trader(
        monkeypatch, start=datetime(2026, 10, 19, 9, 19, tzinfo=IST), duration=480,
        config={"options_intraday": {"scan_interval_seconds": 180}})

    trader.run_session(plan=None)

    assert [elapsed for elapsed, _ in runtime.scans] == [60, 240, 420]
    assert [now.strftime("%H:%M") for _, now in runtime.scans] == ["09:20", "09:23", "09:26"]
    assert [elapsed for elapsed, _ in runtime.ticks] == list(range(0, 481, 60))
    assert runtime.finalizes == [clock.now()]
    trader.ensure_option_broker.assert_called_once()
    trader._write_trade_log_csv.assert_called_once()


def test_default_interval_ignores_cached_plan_and_fixed_hourly_catchup(
        monkeypatch, forbid_plans):
    trader, _, runtime = _trader(monkeypatch, duration=660)
    stale_plan = {"date": "2026-10-01", "executed": True,
                  "execution_ready": False, "candidates": [{"symbol": "RELIANCE.NS"}]}

    trader.run_session(plan=stale_plan, tick_hook=_forbidden)

    assert [elapsed for elapsed, _ in runtime.scans] == [0, 300, 600]
    assert [elapsed for elapsed, _ in runtime.ticks] == list(range(0, 661, 60))
    assert stale_plan == {"date": "2026-10-01", "executed": True,
                          "execution_ready": False,
                          "candidates": [{"symbol": "RELIANCE.NS"}]}


def test_failed_fresh_fetch_keeps_management_ticks_and_finalization(
        monkeypatch, forbid_plans):
    trader, _, runtime = _trader(monkeypatch)
    fetch = Mock(side_effect=RuntimeError("fresh universe fetch failed"))
    runtime.scan_action = fetch

    trader.run_session(plan=None)

    assert fetch.call_count == 3
    assert [elapsed for elapsed, _ in runtime.scans] == [0, 300, 600]
    assert [elapsed for elapsed, _ in runtime.ticks] == list(range(0, 601, 60))
    assert len(runtime.finalizes) == 1


def test_management_only_still_ticks_and_finalizes(monkeypatch, forbid_plans):
    trader, _, runtime = _trader(monkeypatch, duration=360)

    trader.run_session(plan=None, management_only=True)

    assert runtime.scans == []
    assert [elapsed for elapsed, _ in runtime.ticks] == list(range(0, 361, 60))
    assert len(runtime.finalizes) == 1


def test_entries_stop_at_cutoff_but_ticks_continue_through_hard_flat(
        monkeypatch, forbid_plans):
    trader, _, runtime = _trader(
        monkeypatch, start=datetime(2026, 10, 19, 14, 59, tzinfo=IST), duration=1860)

    trader.run_session()

    assert [elapsed for elapsed, _ in runtime.scans] == [0]
    assert len(runtime.ticks) == 32
    assert any(now.strftime("%H:%M") == "15:10" for _, now in runtime.ticks)
    assert runtime.finalizes[0].strftime("%H:%M") == "15:30"


@pytest.mark.parametrize("start, config", [
    (datetime(2026, 10, 17, 11, tzinfo=IST), {}),
    (datetime(2026, 10, 19, 11, tzinfo=IST), {"market": {
        "entry_start": "09:20", "holidays": ["2026-10-19"]}}),
    (datetime(2026, 10, 19, 11, tzinfo=IST), {"market": {
        "entry_start": "invalid"}}),
])
def test_closed_or_invalid_clock_disables_entries_but_manages(
        monkeypatch, forbid_plans, start, config):
    trader, _, runtime = _trader(monkeypatch, start=start, config=config, duration=60)

    trader.run_session()

    assert runtime.scans == []
    assert len(runtime.ticks) == 2
    assert len(runtime.finalizes) == 1


def test_scan_work_does_not_replace_management_cadence_or_delay_entry_interval(
        monkeypatch, forbid_plans):
    trader, clock, runtime = _trader(monkeypatch, duration=660)
    runtime.scan_action = lambda: setattr(clock, "elapsed", clock.elapsed + 20)

    trader.run_session()

    assert [elapsed for elapsed, _ in runtime.scans] == [0, 300, 600]
    assert [elapsed for elapsed, _ in runtime.ticks] == list(range(0, 661, 60))
    assert runtime.ticks[0][1] == clock.start
    assert clock.sleeps[:2] == [40, 60]


def test_max_seconds_finalizes_even_when_scan_and_tick_fail(monkeypatch, forbid_plans):
    trader, _, runtime = _trader(monkeypatch, duration=float("inf"))
    runtime.scan_action = Mock(side_effect=RuntimeError("feed unavailable"))
    runtime.tick_action = Mock(side_effect=RuntimeError("exit not yet confirmed"))

    trader.run_session(max_seconds=120)

    assert len(runtime.scans) == 1
    assert [elapsed for elapsed, _ in runtime.ticks] == [0, 60, 120]
    assert len(runtime.finalizes) == 1


def test_keyboard_interrupt_finalizes(monkeypatch, forbid_plans):
    trader, clock, runtime = _trader(monkeypatch)
    clock.sleep = Mock(side_effect=KeyboardInterrupt)
    import src.paper_trader as paper_trader
    monkeypatch.setattr(paper_trader.time, "sleep", clock.sleep)

    with pytest.raises(KeyboardInterrupt):
        trader.run_session()

    assert len(runtime.ticks) == 1
    assert len(runtime.finalizes) == 1


def test_unconfirmed_finalization_keeps_unresolved_state(monkeypatch, forbid_plans):
    trader, _, runtime = _trader(monkeypatch, duration=0)
    runtime.finalize = Mock(side_effect=RuntimeError("remote holding remains"))

    trader.run_session()

    assert trader._options_unresolved_exits == {"strict_options": "remote holding remains"}
    trader._write_trade_log_csv.assert_called_once()


def _entrypoint_trader(options_only=True, strict=True):
    runtime = SimpleNamespace(finalize=Mock())
    trader = SimpleNamespace(
        options_only=options_only, strict_options_mode=strict,
        ensure_option_broker=Mock(), run_session=Mock(), run=Mock(),
        _get_options_runtime=Mock(return_value=runtime),
        _options_unresolved_exits={}, _last_plan_order_ids=[], _last_option_order_ids=[])
    return trader, runtime


@pytest.mark.parametrize("flags, management_only", [
    ([], False),
    (["--allow-stale-plan"], False),
    (["--no-plan", "--allow-stale-plan"], True),
    (["--date", "2026-10-18", "--allow-stale-plan"], True),
    (["--date", "2026-10-20", "--allow-stale-plan"], True),
])
def test_scheduled_options_entrypoint_never_touches_plans(
        monkeypatch, forbid_plans, flags, management_only):
    from scripts import session

    trader, runtime = _entrypoint_trader()
    factory = Mock(return_value=trader)
    monkeypatch.setattr(session, "PaperTrader", factory)
    monkeypatch.setattr(session, "load_config", lambda: {})
    monkeypatch.setattr(session, "today_ist_str", lambda: "2026-10-19")
    monkeypatch.setattr("sys.argv", ["session.py", "--trading-mode", "options_intraday",
                                    "--max-seconds", "600", *flags])

    session.main()

    assert factory.call_args.kwargs["trading_mode"] == "options_intraday"
    trader.ensure_option_broker.assert_called_once_with({})
    trader.run_session.assert_called_once_with(
        plan=None, interval_seconds=60, max_seconds=600, management_only=management_only)
    runtime.finalize.assert_called_once_with()
    trader.run.assert_not_called()


def test_scheduled_runner_finalizes_when_session_raises(monkeypatch, forbid_plans):
    from scripts import session

    trader, runtime = _entrypoint_trader()
    trader.run_session.side_effect = RuntimeError("session interrupted")
    monkeypatch.setattr(session, "PaperTrader", Mock(return_value=trader))
    monkeypatch.setattr(session, "load_config", lambda: {})
    monkeypatch.setattr("sys.argv", ["session.py", "--trading-mode", "options_intraday"])

    with pytest.raises(RuntimeError, match="session interrupted"):
        session.main()

    runtime.finalize.assert_called_once_with()


def test_direct_options_cli_runs_without_loading_or_rebuilding_plan(
        monkeypatch, forbid_plans):
    import main
    import src.paper_trader as paper_trader

    trader, _ = _entrypoint_trader()
    monkeypatch.setattr(paper_trader, "PaperTrader", Mock(return_value=trader))
    monkeypatch.setattr("src.utils.load_config", lambda: {})
    monkeypatch.setattr(main, "load_env", lambda: None)
    monkeypatch.setattr(main, "setup_logging", lambda: None)
    monkeypatch.setattr("sys.argv", ["main.py", "paper", "--trading-mode",
                                    "options_intraday", "--max-iterations", "2"])

    main.main()

    trader.run_session.assert_called_once_with(
        plan=None, interval_seconds=60, max_seconds=120)
    trader.ensure_option_broker.assert_called_once_with({})
    trader.run.assert_not_called()


@pytest.mark.parametrize("mode", ["production", "combined"])
def test_scheduled_non_options_modes_keep_existing_plan_lifecycle(monkeypatch, mode):
    import src.plan as plans
    from scripts import session

    trader, runtime = _entrypoint_trader(options_only=False, strict=mode == "combined")
    plan = {"date": "2026-10-19", "trading_mode": mode, "executed": False,
            "candidates": [], "options_intraday_plan": {
                "date": "2026-10-19", "execution_ready": True}}
    load = Mock(return_value=plan)
    mark = Mock()
    monkeypatch.setattr(session, "PaperTrader", Mock(return_value=trader))
    monkeypatch.setattr(session, "load_config", lambda: {"options": {"enabled": False}})
    monkeypatch.setattr(session, "today_ist_str", lambda: "2026-10-19")
    monkeypatch.setattr(session, "load_plan", load)
    monkeypatch.setattr(session, "mark_executed", mark)
    monkeypatch.setattr(plans, "save_plan", _forbidden)
    monkeypatch.setattr("scripts.premarket.build_plan", _forbidden)
    monkeypatch.setattr("sys.argv", ["session.py", "--trading-mode", mode])

    session.main()

    load.assert_called_once_with("2026-10-19")
    assert trader.run_session.call_args.kwargs["plan"] is plan
    assert "management_only" not in trader.run_session.call_args.kwargs
    mark.assert_called_once_with(plan, [], date="2026-10-19")
    assert runtime.finalize.call_count == (1 if mode == "combined" else 0)


@pytest.mark.parametrize("strict", [False, True])
def test_direct_non_options_modes_keep_existing_cli_flow(monkeypatch, strict):
    import main
    import src.paper_trader as paper_trader

    trader, _ = _entrypoint_trader(options_only=False, strict=strict)
    plan = {"date": "2026-10-19"}
    load = Mock(return_value=plan)
    current = Mock(return_value=plan)
    monkeypatch.setattr(paper_trader, "PaperTrader", Mock(return_value=trader))
    monkeypatch.setattr("src.utils.load_config", lambda: {})
    monkeypatch.setattr("src.plan.load_plan", load)
    monkeypatch.setattr("src.plan.today_ist_str", lambda: "2026-10-19")
    monkeypatch.setattr(main, "_current_strict_options_plan", current)

    main.cmd_paper(argparse.Namespace(
        symbols=[], interval=None, timeframe="15m", max_iterations=2,
        trading_mode="combined" if strict else "production"))

    if strict:
        load.assert_called_once_with("2026-10-19")
        current.assert_called_once_with({}, plan, session_date="2026-10-19")
        trader.run_session.assert_called_once_with(
            plan=plan, interval_seconds=300, max_seconds=600)
        trader.run.assert_not_called()
    else:
        load.assert_not_called()
        trader.run.assert_called_once_with(interval_seconds=300, max_iterations=2)
        trader.run_session.assert_not_called()
