"""Static safety checks for the controlled scheduled paper mode."""

from pathlib import Path
import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_paper_workflows_parse_and_use_pinned_runtime_and_lock():
    for name, job in (("premarket.yml", "premarket"), ("session.yml", "session")):
        workflow = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))
        steps = workflow["jobs"][job]["steps"]
        setup = next(step for step in steps if step.get("uses", "").startswith("actions/setup-python@"))
        assert setup["with"]["python-version-file"] == ".python-version"
        assert setup["with"]["cache-dependency-path"] == "requirements-server.lock"
        install = next(step for step in steps if step.get("name") == "Install dependencies")
        assert "--require-hashes -r requirements-server.lock" in install["run"]


def test_options_overlay_defaults_to_nifty50_scan():
    overlay = yaml.safe_load(
        (ROOT / "config" / "options_intraday.yaml").read_text(encoding="utf-8")
    )
    assert overlay["options_intraday"]["scan_basket"] == "nifty50"


def test_scheduled_workflows_are_options_only():
    for name in ("premarket.yml", "session.yml"):
        text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        assert "TRIO_TRADING_MODE: options_intraday" in text
        assert "--trading-mode options_intraday" in text
        assert "--trading-mode combined" not in text


def test_session_persists_strict_options_state_path():
    text = (ROOT / ".github" / "workflows" / "session.yml").read_text(
        encoding="utf-8")
    assert "TRIO_OPTIONS_INTRADAY_STATE_PATH: output/runtime/options_intraday_state.json" in text


def test_state_is_committed_before_fetching_remote_main():
    """Regression for the failed run's dirty-output merge abort."""
    for name in ("premarket.yml", "session.yml"):
        text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        persist_start = text.index("git add -A")
        persist_end = text.index("git push origin HEAD:main", persist_start)
        block = text[persist_start:persist_end]
        assert block.index("git commit -m") < block.index("git fetch origin main")
        assert block.index("git fetch origin main") < block.index("git merge -X ours")
