"""
Tests for the shared exit policy (ladder state machine).
"""
import pytest
from src.exit_policy import (
    LadderState, ExitAction, decide_exit, apply_actions, ladder_config,
    r_multiple, _ratchet
)


def test_r_multiple_long():
    """R multiple calculation for long position."""
    state = LadderState(side="LONG", entry=100.0, initial_stop=90.0)
    # Price at 108 => (108-100)/10 = 0.8R
    assert r_multiple(state, 108.0) == 0.8
    # Price at 115 => 1.5R
    assert r_multiple(state, 115.0) == 1.5


def test_r_multiple_short():
    """R multiple calculation for short position."""
    state = LadderState(side="SHORT", entry=100.0, initial_stop=110.0)
    # Price at 92 => (100-92)/10 = 0.8R
    assert r_multiple(state, 92.0) == 0.8
    # Price at 85 => 1.5R
    assert r_multiple(state, 85.0) == 1.5


def test_ratchet_long():
    """Long trail only moves up."""
    assert _ratchet("LONG", 90.0, 95.0) == 95.0  # tightens
    assert _ratchet("LONG", 95.0, 90.0) == 95.0  # never loosens


def test_ratchet_short():
    """Short trail only moves down."""
    assert _ratchet("SHORT", 110.0, 105.0) == 105.0  # tightens
    assert _ratchet("SHORT", 105.0, 110.0) == 105.0  # never loosens


def test_decide_exit_t1_partial():
    """T1 fires at 0.8R -> 50% partial, stop to breakeven."""
    state = LadderState(
        side="LONG", entry=100.0, initial_stop=90.0, stop=90.0,
        target=130.0, quantity=10, original_quantity=10, t1_hit=False
    )
    actions = decide_exit(state, price=109.0, high=109.0, low=109.0)
    assert len(actions) == 1
    a = actions[0]
    assert a.kind == "partial"
    assert a.reason == "partial@T1-0.8R"
    assert a.fraction_of_original == 0.5
    assert a.quantity == 5
    assert a.new_stop == 100.0  # breakeven


def test_decide_exit_t1_not_fired_below_threshold():
    """T1 does not fire below 0.8R."""
    state = LadderState(
        side="LONG", entry=100.0, initial_stop=90.0, stop=90.0,
        target=130.0, quantity=10, original_quantity=10, t1_hit=False
    )
    actions = decide_exit(state, price=107.0, high=107.0, low=107.0)
    assert len(actions) == 0


def test_decide_exit_t1_one_unit_rule():
    """T1 with quantity=1 does not fire (qty would be 0)."""
    state = LadderState(
        side="LONG", entry=100.0, initial_stop=90.0, stop=90.0,
        target=130.0, quantity=1, original_quantity=1, t1_hit=False
    )
    actions = decide_exit(state, price=109.0, high=109.0, low=109.0)
    assert len(actions) == 0  # int(1 * 0.5) == 0


def test_decide_exit_t2_after_t1():
    """T2 fires at 1.5R after T1 hit -> 30% partial + trail."""
    state = LadderState(
        side="LONG", entry=100.0, initial_stop=90.0, stop=100.0,  # breakeven after T1
        target=130.0, quantity=5, original_quantity=10, t1_hit=True, t2_hit=False
    )
    cfg = {"risk_management": {"ladder": {"enabled_phase2": True, "t2_fraction": 0.3}}}
    actions = decide_exit(state, price=115.0, high=115.0, low=115.0, atr=2.0, cfg=cfg)
    assert len(actions) >= 1
    # Find the T2 partial
    t2_actions = [a for a in actions if "T2" in a.reason and a.kind == "partial"]
    assert len(t2_actions) == 1
    a = t2_actions[0]
    assert a.fraction_of_original == 0.3
    assert a.quantity == 3  # int(10 * 0.3)
    # new_stop should be ratcheted from trail
    assert a.new_stop is not None


def test_decide_exit_stop_hit():
    """Stop hit -> close remaining."""
    state = LadderState(
        side="LONG", entry=100.0, initial_stop=90.0, stop=90.0,
        target=130.0, quantity=10, original_quantity=10, t1_hit=False
    )
    actions = decide_exit(state, price=89.0, high=92.0, low=88.0)
    assert len(actions) == 1
    a = actions[0]
    assert a.kind == "close"
    assert a.reason == "stop-hit"
    assert a.quantity == 10


def test_decide_exit_target_hit():
    """Target hit -> close remaining (after T1 partial, phase2=shadow).
    T1 at 0.8R fires first (5 units). phase2=off -> T2 becomes shadow log.
    Remaining 5 hit target.
    """
    state = LadderState(
        side="LONG", entry=100.0, initial_stop=90.0, stop=90.0,
        target=120.0, quantity=10, original_quantity=10, t1_hit=False
    )
    cfg = {"risk_management": {"ladder": {"enabled_phase2": False}}}
    actions = decide_exit(state, price=120.0, high=120.0, low=118.0, cfg=cfg)
    # 3 actions: T1 partial, T2 shadow log, Target close on remainder
    assert len(actions) == 3
    # First action: T1 partial
    t1_action = actions[0]
    assert t1_action.kind == "partial"
    assert t1_action.reason == "partial@T1-0.8R"
    assert t1_action.quantity == 5
    # Second action: T2 shadow log (phase2 off)
    t2_shadow = actions[1]
    assert t2_shadow.kind == "shadow"
    assert t2_shadow.reason == "ladder-T2"
    # Third action: target hit on remaining quantity
    tgt_action = actions[2]
    assert tgt_action.kind == "close"
    assert tgt_action.reason == "target-hit"
    assert tgt_action.quantity == 5


def test_decide_exit_eod_squareoff():
    """Hard flat -> close everything regardless of state."""
    state = LadderState(
        side="LONG", entry=100.0, initial_stop=90.0, stop=90.0,
        target=120.0, quantity=10, original_quantity=10, t1_hit=False
    )
    actions = decide_exit(state, price=109.0, hard_flat=True)
    assert len(actions) == 1
    a = actions[0]
    assert a.kind == "close"
    assert a.reason == "eod-squareoff"
    assert a.quantity == 10


def test_decide_exit_stop_before_target_same_bar():
    """When both stop and target hit same bar, stop wins (conservative)."""
    state = LadderState(
        side="LONG", entry=100.0, initial_stop=90.0, stop=90.0,
        target=105.0, quantity=10, original_quantity=10, t1_hit=False
    )
    # Bar gapped: low <= stop, high >= target
    actions = decide_exit(state, price=105.0, high=105.0, low=89.0)
    assert len(actions) == 1
    assert actions[0].reason == "stop-hit"


def test_apply_actions_t1():
    """Apply T1 action updates state correctly."""
    state = LadderState(
        side="LONG", entry=100.0, initial_stop=90.0, stop=90.0,
        target=130.0, quantity=10, original_quantity=10, t1_hit=False
    )
    actions = [ExitAction(
        kind="partial", reason="partial@T1-0.8R",
        fraction_of_original=0.5, quantity=5, new_stop=100.0
    )]
    realized = apply_actions(state, actions, 109.0)
    assert realized == (109.0 - 100.0) * 5  # 45
    assert state.quantity == 5
    assert state.t1_hit is True
    assert state.stop == 100.0


def test_apply_actions_close():
    """Apply close action clears position."""
    state = LadderState(
        side="LONG", entry=100.0, initial_stop=90.0, stop=100.0,
        target=130.0, quantity=5, original_quantity=10, t1_hit=True
    )
    actions = [ExitAction(kind="close", reason="stop-hit", quantity=5)]
    realized = apply_actions(state, actions, 99.0)
    assert realized == (99.0 - 100.0) * 5  # -5
    assert state.quantity == 0


def test_ladder_config_defaults():
    """Config resolution with missing keys falls back to defaults."""
    cfg = {}
    knobs = ladder_config(cfg)
    assert knobs["t1_at_r"] == 0.8
    assert knobs["t2_at_r"] == 1.5
    assert knobs["t1_fraction"] == 0.5
    assert knobs["t2_fraction"] == 0.3
    assert knobs["phase2"] is False
    assert knobs["trail_mult"] == 3.0


def test_ladder_config_override():
    """Config override works."""
    cfg = {"risk_management": {
        "partial_at_r": 1.0,
        "partial_fraction": 0.33,
        "ladder": {"t2_at_r": 2.0, "t2_fraction": 0.25, "enabled_phase2": True,
                   "trailing_after_t2": {"atr_multiplier": 2.0}}
    }}
    knobs = ladder_config(cfg)
    assert knobs["t1_at_r"] == 1.0
    assert knobs["t1_fraction"] == 0.33
    assert knobs["t2_at_r"] == 2.0
    assert knobs["t2_fraction"] == 0.25
    assert knobs["phase2"] is True
    assert knobs["trail_mult"] == 2.0