"""Session single-instance lock and planner diagnostics.

The 2026-11-23 post-mortem found two operational gaps:

* two options sessions ran 13:16-15:30 against one state file, and
* the planner logged a candidate count but never which sleeve produced it.

Both are covered here so a regression is caught offline.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from src.options_intraday_plan import (  # noqa: E402
    _LAST_STRATEGY_DIAG,
    _log_strategy_diagnostics,
    _strategy_diag_summary,
)
from src.session_lock import (  # noqa: E402
    options_session_lock_path,
    options_session_owner_path,
    single_instance_lock,
)


# ---------------------------------------------------------------------------
# Single-instance lock
# ---------------------------------------------------------------------------

_CHILD = """
import sys
sys.path.insert(0, {root!r})
from pathlib import Path
from src.session_lock import SessionLockError, single_instance_lock

try:
    with single_instance_lock(Path({path!r})):
        print("ACQUIRED")
except SessionLockError as exc:
    print("BLOCKED: " + str(exc))
"""


def _run_child(tmp_path: Path, lock_path: Path) -> str:
    script = tmp_path / "child_session.py"
    script.write_text(
        _CHILD.format(root=str(ROOT), path=str(lock_path)), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip().splitlines()[-1]


def test_second_session_is_refused_then_allowed_after_release(tmp_path):
    lock_path = tmp_path / "session.lock"

    with single_instance_lock(lock_path):
        assert _run_child(tmp_path, lock_path).startswith("BLOCKED:")

    # Released: a fresh process must be able to take it.
    assert _run_child(tmp_path, lock_path) == "ACQUIRED"


def test_owner_sidecar_records_the_holder_pid(tmp_path):
    lock_path = tmp_path / "session.lock"

    with single_instance_lock(lock_path):
        owner = options_session_owner_path(lock_path).read_text(
            encoding="utf-8").splitlines()

    assert owner[0].strip() == str(os.getpid())
    # Second line is an ISO-8601 UTC timestamp, useful in a post-mortem.
    assert "T" in owner[1]


def test_lock_error_names_the_holding_pid(tmp_path):
    lock_path = tmp_path / "session.lock"

    with single_instance_lock(lock_path):
        output = _run_child(tmp_path, lock_path)

    assert output.startswith("BLOCKED:")
    assert "refusing to start a second options session" in output
    assert str(os.getpid()) in output


def test_lock_without_owner_file_still_refuses(tmp_path):
    lock_path = tmp_path / "session.lock"

    with single_instance_lock(lock_path):
        options_session_owner_path(lock_path).unlink()
        output = _run_child(tmp_path, lock_path)

    assert output.startswith("BLOCKED:")
    assert "held by pid" not in output


def test_lock_path_defaults_next_to_the_state_file(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIO_OPTIONS_INTRADAY_STATE_PATH",
                       str(tmp_path / "custom" / "state.json"))
    assert options_session_lock_path() == tmp_path / "custom" / "session.lock"

    monkeypatch.delenv("TRIO_OPTIONS_INTRADAY_STATE_PATH")
    monkeypatch.setenv("TRIO_OPTIONS_INTRADAY_STATE_DIR", str(tmp_path / "dir"))
    assert options_session_lock_path() == tmp_path / "dir" / "session.lock"


# ---------------------------------------------------------------------------
# Planner per-sleeve diagnostics
# ---------------------------------------------------------------------------


def test_diag_summary_names_every_sleeve_and_signal_count():
    summary = _strategy_diag_summary({
        "orb": {"status": "signal", "signals": 2},
        "orb_retest": {"status": "no_signal", "signals": 0},
        "orb_smt": {"status": "rejected", "reason": "smt_no_candles"},
    })
    assert "orb=signal(2)" in summary
    assert "orb_retest=no_signal" in summary
    assert "orb_smt=rejected:smt_no_candles" in summary


def test_diag_line_is_logged_once_per_change(caplog):
    _LAST_STRATEGY_DIAG.clear()
    diag = {"orb": {"status": "signal", "signals": 1}}
    with caplog.at_level("INFO"):
        _log_strategy_diagnostics("NIFTY", diag)
        _log_strategy_diagnostics("NIFTY", diag)
        _log_strategy_diagnostics(
            "NIFTY", {"orb": {"status": "no_signal", "signals": 0}})
    lines = [r.message for r in caplog.records if "PLANNER strategies" in r.message]
    assert len(lines) == 2
    assert "orb=signal(1)" in lines[0]
    assert "orb=no_signal" in lines[1]


def test_diag_cache_is_per_underlying(caplog):
    _LAST_STRATEGY_DIAG.clear()
    diag = {"orb": {"status": "signal", "signals": 1}}
    with caplog.at_level("INFO"):
        _log_strategy_diagnostics("NIFTY", diag)
        _log_strategy_diagnostics("BANKNIFTY", diag)
    lines = [r.message for r in caplog.records if "PLANNER strategies" in r.message]
    assert len(lines) == 2


def test_planner_logs_diagnostics_before_the_no_signal_return():
    source = (ROOT / "src" / "options_intraday_plan.py").read_text(encoding="utf-8")
    logged = source.index("_log_strategy_diagnostics(underlying, strategy_diag)")
    early_return = source.index('failure_reason"] = "no_strategy_signal"')
    assert logged < early_return
