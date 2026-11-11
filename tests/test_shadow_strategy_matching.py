"""Shadow sleeve matching must be spelling-insensitive.

The planner lowercases its display name (``MeanReversion``), config keys use
snake_case (``mean_reversion``) and ORB-Retest arrives hyphenated.  Before this
normalisation the shipped ``shadow_strategies: [mean_reversion]`` never matched
the planner's ``meanreversion``, so the sleeve was silently tradable.
"""

from __future__ import annotations

from src.options_live_scan import (
    _candidate_strategy_key,
    _candidate_strategy_name,
    _shadow_strategy_names,
)


def test_shipped_style_config_matches_planner_style_names():
    shadow = _shadow_strategy_names(
        {"shadow_strategies": ["mean_reversion", "opening_range_reversion"]}
    )
    assert shadow == {"meanreversion", "openingrangereversion"}

    assert _candidate_strategy_key({"strategy": "MeanReversion"}) in shadow
    assert _candidate_strategy_key({"strategy": "OpeningRangeReversion"}) in shadow


def test_orb_sleeves_are_never_shadowed():
    shadow = _shadow_strategy_names(
        {"shadow_strategies": ["mean_reversion", "opening_range_reversion"]}
    )
    assert _candidate_strategy_key({"strategy": "ORB"}) not in shadow
    assert _candidate_strategy_key({"strategy": "ORB-Retest"}) not in shadow


def test_smc_display_name_matches_the_snake_case_shadow_key():
    shadow = _shadow_strategy_names(
        {"shadow_strategies": ["smc_liquidity"]}
    )
    assert shadow == {"smcliquidity"}
    assert _candidate_strategy_key({"strategy": "SMC-Liquidity"}) in shadow
    assert _candidate_strategy_name({"strategy": "SMC-Liquidity"}) == "smc_liquidity"


def test_shadow_names_accept_a_comma_separated_string():
    shadow = _shadow_strategy_names(
        {"shadow_strategies": "mean_reversion, opening_range_reversion"}
    )
    assert shadow == {"meanreversion", "openingrangereversion"}


def test_records_use_canonical_snake_case_names():
    assert _candidate_strategy_name({"strategy": "MeanReversion"}) == "mean_reversion"
    assert (
        _candidate_strategy_name({"strategy": "OpeningRangeReversion"})
        == "opening_range_reversion"
    )
    assert _candidate_strategy_name({"strategy": "ORB-Retest"}) == "orb_retest"


def test_unknown_sleeves_fall_back_to_their_normalised_key():
    assert _candidate_strategy_name({"strategy": "Something New"}) == "somethingnew"


def test_setup_field_is_used_when_strategy_is_absent():
    assert _candidate_strategy_key({"setup": "OpeningRangeReversion"}) == (
        "openingrangereversion"
    )
