"""Conditional forecasts must preserve ranked work before proposing a rescue."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import product

import pytest

from src.services.deadline_forecast import (
    DeadlineForecaster,
    ForecastDrop,
    ForecastSnapshot,
    MiningWindow,
    ViewingWindow,
)


NOW = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)


def reward(key, game, minutes, deadline, **kwargs):
    return ForecastDrop(
        id=key, game_id=game, required_minutes=minutes, observed_minutes=0,
        observed_at=NOW, starts_at=NOW, ends_at=NOW + timedelta(hours=deadline),
        **kwargs,
    )


def snapshot(*drops, **kwargs):
    return ForecastSnapshot(
        now=NOW, horizon=NOW + timedelta(days=3), ranked_game_ids=(1, 2, 3),
        drops=tuple(drops),
        viewing_windows=tuple(ViewingWindow(
            channel_id=str(drop.game_id), starts_at=NOW, ends_at=NOW + timedelta(days=3),
            drop_ids=frozenset(item.id for item in drops if item.game_id == drop.game_id),
            allocation_known=True,
        ) for index, drop in enumerate(drops) if drop.game_id not in {d.game_id for d in drops[:index]}),
        mining_windows=(MiningWindow(NOW, NOW + timedelta(days=3)),),
        **kwargs,
    )


def analyze(state, **kwargs):
    return DeadlineForecaster(**kwargs).analyze(state, enabled=True)


def test_rescues_two_hour_reward_then_preserves_eight_hour_rank_one():
    state = snapshot(reward("rank-one", 1, 480, 48), reward("urgent", 2, 120, 4))

    result = analyze(state)

    assert result.reason == "rescue"
    assert result.plan.drop_id == "urgent"
    assert result.plan.game_id == 2 and result.plan.channel_id == "2"
    assert result.plan.earned_at == NOW + timedelta(hours=2)
    assert result.plan.protected_drop_ids == frozenset({"rank-one"})
    assert result.baseline_earned_ids == frozenset({"rank-one"})


def test_conflicting_detour_keeps_otherwise_earnable_higher_reward():
    state = snapshot(reward("rank-one", 1, 180, 4), reward("urgent", 2, 120, 3))

    result = analyze(state)

    assert result.reason == "no_safe_rescue" and result.plan is None


def test_no_detour_for_reward_already_earned_by_ranked_baseline():
    state = snapshot(reward("rank-one", 1, 60, 48), reward("not-urgent", 2, 120, 4))
    assert analyze(state).plan is None


@pytest.mark.parametrize("enabled,manual,reason", [
    (False, False, "disabled"), (False, True, "disabled"), (True, True, "manual_override"),
])
def test_disabled_and_manual_modes_do_not_plan(enabled, manual, reason):
    state = snapshot(reward("rank-one", 1, 480, 48), reward("urgent", 2, 120, 4))
    result = DeadlineForecaster().analyze(state, enabled=enabled, manual_override=manual)
    assert result.reason == reason and result.plan is None


@pytest.mark.parametrize("minutes,age", [(None, 0), (0, 6), (True, 0), (-1, 0), (121, 0)])
def test_unknown_stale_or_invalid_progress_never_becomes_confirmed_zero(minutes, age):
    urgent = replace(reward("urgent", 2, 120, 4), observed_minutes=minutes,
                     observed_at=NOW - timedelta(minutes=age))
    result = analyze(snapshot(reward("rank-one", 1, 480, 48), urgent))
    assert result.plan is None
    assert result.reason in {"unknown_progress", "invalid_snapshot"}


def test_future_observation_receipt_is_rejected():
    urgent = replace(reward("urgent", 2, 120, 4), observed_at=NOW + timedelta(seconds=1))
    assert analyze(snapshot(reward("rank-one", 1, 480, 48), urgent)).reason == "unknown_progress"


def test_uses_direct_remaining_minutes_not_the_entire_requirement():
    urgent = replace(reward("urgent", 2, 600, 4), observed_minutes=480)
    result = analyze(snapshot(reward("rank-one", 1, 480, 48), urgent))
    assert result.plan.earned_at == NOW + timedelta(hours=2)


def test_unknown_credit_allocation_refuses_rescue():
    state = snapshot(reward("rank-one", 1, 480, 48), reward("urgent", 2, 120, 4))
    state = replace(state, viewing_windows=tuple(
        replace(window, allocation_known=False) for window in state.viewing_windows
    ))
    assert analyze(state).reason == "unknown_allocation"


def test_explicit_shared_credit_is_not_double_counted_as_serial_work():
    state = snapshot(
        reward("rank-one-a", 1, 120, 4), reward("rank-one-b", 1, 120, 4),
        reward("urgent", 2, 60, 1.5),
    )
    result = analyze(state)
    assert result.plan.drop_id == "urgent"
    assert result.plan.protected_drop_ids == frozenset({"rank-one-a", "rank-one-b"})


def test_cross_category_shared_credit_can_make_a_rescue_unnecessary():
    state = snapshot(reward("rank-one", 1, 480, 48), reward("urgent", 2, 120, 4))
    state = replace(state, viewing_windows=(replace(
        state.viewing_windows[0], drop_ids=frozenset({"rank-one", "urgent"}),
    ),))
    assert analyze(state).plan is None


def test_prerequisite_claim_delay_is_included():
    prerequisite = reward("prerequisite", 2, 60, 4, wanted=False)
    urgent = reward("urgent", 2, 60, 2 + 10 / 3600, prerequisite_ids=("prerequisite",))
    result = analyze(snapshot(reward("rank-one", 1, 480, 48), prerequisite, urgent))
    assert result.plan is None


def test_prerequisite_chain_can_be_rescued_with_sufficient_time():
    prerequisite = reward("prerequisite", 2, 60, 4, wanted=False)
    urgent = reward("urgent", 2, 60, 3, prerequisite_ids=("prerequisite",))
    result = analyze(snapshot(reward("rank-one", 1, 480, 48), prerequisite, urgent))
    assert result.plan.drop_id == "urgent"
    assert result.plan.earned_at == NOW + timedelta(hours=2, seconds=20)


@pytest.mark.parametrize("kind", ["missing", "cycle"])
def test_invalid_dependency_graph_fails_closed(kind):
    urgent = reward("urgent", 2, 120, 4, prerequisite_ids=("missing" if kind == "missing" else "urgent",))
    assert analyze(snapshot(reward("rank-one", 1, 480, 48), urgent)).reason == "invalid_snapshot"


def test_already_claimed_prerequisite_needs_no_fresh_minutes_observation():
    prerequisite = replace(reward("prerequisite", 2, 60, 4, claimed=True, wanted=False),
                           observed_minutes=None, observed_at=None)
    urgent = reward("urgent", 2, 60, 3, prerequisite_ids=("prerequisite",))
    result = analyze(snapshot(reward("rank-one", 1, 480, 48), prerequisite, urgent))
    assert result.plan.earned_at == NOW + timedelta(hours=1)


def test_individual_start_window_prevents_early_rescue():
    urgent = replace(reward("urgent", 2, 120, 4), starts_at=NOW + timedelta(hours=1))
    assert analyze(snapshot(reward("rank-one", 1, 480, 48), urgent)).plan is None


@pytest.mark.parametrize("window_end", [1, 2])
def test_mining_hours_and_exclusive_deadline_block_infeasible_rescue(window_end):
    state = snapshot(reward("rank-one", 1, 480, 48), reward("urgent", 2, 120, 2))
    state = replace(state, mining_windows=(MiningWindow(NOW, NOW + timedelta(hours=window_end)),))
    assert analyze(state).plan is None


def test_mining_pause_is_not_counted_as_watch_time():
    state = snapshot(reward("rank-one", 1, 480, 48), reward("urgent", 2, 120, 3))
    state = replace(state, mining_windows=(
        MiningWindow(NOW, NOW + timedelta(hours=1)),
        MiningWindow(NOW + timedelta(hours=2), NOW + timedelta(days=2)),
    ))
    assert analyze(state).plan is None


def test_absent_eligible_channel_prevents_rescue():
    state = snapshot(reward("rank-one", 1, 480, 48), reward("urgent", 2, 120, 4))
    state = replace(state, viewing_windows=state.viewing_windows[:1])
    assert analyze(state).plan is None


def test_unlisted_game_cannot_be_promoted():
    state = snapshot(reward("rank-one", 1, 480, 48), reward("unlisted", 4, 120, 4))
    assert analyze(state).plan is None


def test_naive_timestamp_is_invalid_not_assumed_utc():
    state = snapshot(reward("rank-one", 1, 480, 48), reward("urgent", 2, 120, 4))
    assert analyze(replace(state, now=NOW.replace(tzinfo=None))).reason == "invalid_snapshot"


def test_forecast_budget_exhaustion_returns_no_partial_recommendation():
    state = snapshot(reward("rank-one", 1, 480, 48), reward("urgent", 2, 120, 4))
    assert analyze(state, max_steps=1).reason == "budget_exceeded"


def test_analysis_is_deterministic_and_does_not_mutate_snapshot():
    state = snapshot(reward("rank-one", 1, 480, 48), reward("urgent", 2, 120, 4))
    before = repr(state)
    assert analyze(state) == analyze(state)
    assert repr(state) == before


def test_two_reward_grid_matches_independent_serial_timing_oracle():
    for high, low, high_end, low_end in product((1, 2, 4), (1, 2, 3), (2, 4, 8, 48), (1, 2, 4, 8)):
        state = snapshot(reward("high", 1, high * 60, high_end), reward("low", 2, low * 60, low_end))
        baseline_high_earned = high < high_end
        baseline_low_earned = min(high, high_end) + low < low_end
        safe_detour = low < low_end and (not baseline_high_earned or low + high < high_end)
        expected = not baseline_low_earned and safe_detour
        assert (analyze(state).plan is not None) is expected, (high, low, high_end, low_end)


@pytest.mark.parametrize("second_deadline,expected", [(6, False), (7, True)])
def test_rescue_preserves_every_higher_ranked_reward_not_just_the_first(second_deadline, expected):
    state = snapshot(
        reward("first", 1, 60, 48), reward("second", 2, 180, second_deadline),
        reward("urgent", 3, 120, 3),
    )
    result = analyze(state)
    assert (result.plan is not None) is expected
    if expected:
        assert result.plan.protected_drop_ids == frozenset({"first", "second"})


def test_horizon_cannot_hide_the_loss_of_a_later_higher_reward():
    state = snapshot(reward("high", 1, 480, 9), reward("urgent", 2, 120, 4))
    state = replace(state, horizon=NOW + timedelta(hours=4))
    assert analyze(state).reason == "incomplete_horizon"


def test_earliest_feasible_deadline_breaks_candidate_ties_deterministically():
    state = snapshot(
        reward("first", 1, 480, 48), reward("second", 2, 120, 4),
        reward("third", 3, 60, 2),
    )
    assert analyze(state).plan.drop_id == "third"


def test_explicit_channel_availability_is_not_extended_to_finish_a_reward():
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    windows = tuple(replace(window, ends_at=NOW + timedelta(minutes=90))
                    if window.channel_id == "2" else window for window in state.viewing_windows)
    assert analyze(replace(state, viewing_windows=windows)).plan is None


def test_unknown_unlisted_progress_is_not_promoted_or_used_to_block_selected_work():
    unlisted = replace(reward("unlisted", 4, 60, 48), observed_minutes=None, observed_at=None)
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4), unlisted)
    assert analyze(state).plan.drop_id == "urgent"


def test_overlapping_mining_windows_are_unioned_not_double_counted():
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    windows = (MiningWindow(NOW, NOW + timedelta(minutes=90)),) * 2
    assert analyze(replace(state, mining_windows=windows)).plan is None


def test_equivalent_timezone_offsets_have_identical_results():
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    shifted = replace(state, now=NOW.astimezone(timezone(timedelta(hours=-4))))
    assert analyze(shifted) == analyze(state)


@pytest.mark.parametrize("kind", ["duplicate_drop", "duplicate_rank", "boolean_requirement", "negative_requirement", "unknown_drop", "overlap_channel"])
def test_malformed_snapshot_is_rejected(kind):
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    if kind == "duplicate_drop":
        state = replace(state, drops=state.drops + state.drops[:1])
    elif kind == "duplicate_rank":
        state = replace(state, ranked_game_ids=(1, 1, 2))
    elif kind in ("boolean_requirement", "negative_requirement"):
        high = replace(state.drops[0], required_minutes=True if kind == "boolean_requirement" else -1)
        state = replace(state, drops=(high, state.drops[1]))
    elif kind == "unknown_drop":
        state = replace(state, viewing_windows=(replace(state.viewing_windows[0], drop_ids=frozenset({"missing"})),))
    elif kind == "overlap_channel":
        state = replace(state, viewing_windows=state.viewing_windows + state.viewing_windows[:1])
    assert analyze(state).reason == "invalid_snapshot"


def test_unneeded_unwanted_work_does_not_delay_ranked_baseline():
    state = snapshot(
        reward("high", 1, 60, 48), reward("unwanted", 1, 600, 48, wanted=False),
        reward("low", 2, 120, 4),
    )
    assert analyze(state).plan is None


@pytest.mark.parametrize("first_channel", ["a", "b"])
def test_supplied_channel_preference_order_is_part_of_the_ranked_baseline(first_channel):
    state = snapshot(
        reward("a", 1, 60, 2), reward("b", 1, 180, 5), reward("urgent", 2, 60, 2),
    )
    state = replace(state, viewing_windows=tuple(ViewingWindow(
        channel_id=drop.id, starts_at=NOW, ends_at=state.horizon,
        drop_ids=frozenset({drop.id}), allocation_known=True,
        channel_order=0 if drop.id == first_channel else 1,
    ) for drop in state.drops))
    result = analyze(state)
    if first_channel == "a":
        assert result.baseline_earned_ids == frozenset({"a", "b"})
        assert result.plan is None
    else:
        assert result.baseline_earned_ids == frozenset({"b"})
        assert result.plan.drop_id == "urgent"


@pytest.mark.parametrize("max_steps", [True, 0, 100_001])
def test_invalid_transition_budget_cannot_disable_safety_limit(max_steps):
    with pytest.raises(ValueError):
        DeadlineForecaster(max_steps=max_steps)


@pytest.mark.parametrize("age", [timedelta(seconds=-1), timedelta(days=2), "5 minutes"])
def test_invalid_freshness_bound_is_rejected(age):
    with pytest.raises(ValueError):
        DeadlineForecaster(max_observation_age=age)


def test_empty_selected_work_has_no_recommendation():
    assert analyze(snapshot()).reason == "no_ranked_work"


def test_paused_mining_window_does_not_propose_a_switch_now():
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 60, 3))
    state = replace(state, mining_windows=(MiningWindow(NOW + timedelta(hours=1), state.horizon),))
    assert analyze(state).reason == "no_ranked_work"


def test_too_many_rescue_candidates_returns_no_partial_decision():
    state = snapshot(reward("high", 1, 480, 48), *(
        reward(str(index), 2, 60, 2) for index in range(33)
    ))
    assert analyze(state).reason == "budget_exceeded"


@pytest.mark.parametrize("kind", ["drops", "viewing_windows", "mining_windows"])
def test_snapshot_size_limits_apply_before_simulation(kind):
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    limit = 257 if kind == "drops" else 513
    state = replace(state, **{kind: getattr(state, kind)[:1] * limit})
    assert analyze(state).reason == "budget_exceeded"


@pytest.mark.parametrize("kind", ["horizon", "drop", "mining_window"])
def test_reversed_or_unbounded_intervals_are_invalid(kind):
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    if kind == "horizon":
        state = replace(state, horizon=NOW + timedelta(days=15))
    elif kind == "drop":
        state = replace(state, drops=(replace(state.drops[0], ends_at=NOW), state.drops[1]))
    elif kind == "mining_window":
        state = replace(state, mining_windows=(MiningWindow(state.horizon, NOW),))
    assert analyze(state).reason == "invalid_snapshot"


def test_pending_completed_prerequisite_is_not_treated_as_already_claimed():
    prerequisite = replace(reward("parent", 2, 60, 4, wanted=False), observed_minutes=60)
    target = reward("urgent", 2, 60, 3, prerequisite_ids=("parent", "parent"))
    state = snapshot(reward("high", 1, 480, 48), prerequisite, target)
    # The target cannot be watched at the instant the snapshot is taken.
    assert analyze(state).plan is None
    claimed_state = replace(state, drops=(state.drops[0], replace(prerequisite, claimed=True), target))
    assert analyze(claimed_state).plan.earned_at == NOW + timedelta(hours=1)


@pytest.mark.parametrize("field,value", [
    ("ranked_game_ids", (1, [])), ("ranked_game_ids", None),
    ("drops", None), ("drops", (None,)),
    ("viewing_windows", None), ("viewing_windows", (None,)),
    ("mining_windows", None), ("mining_windows", (None,)),
])
def test_malformed_structures_return_invalid_snapshot_instead_of_raising(field, value):
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    assert analyze(replace(state, **{field: value})).reason == "invalid_snapshot"


def test_missing_snapshot_is_invalid_when_analysis_is_enabled():
    assert analyze(None).reason == "invalid_snapshot"


@pytest.mark.parametrize("extreme", [
    datetime.min.replace(tzinfo=timezone(timedelta(hours=1))),
    datetime.max.replace(tzinfo=timezone(timedelta(hours=-1))),
])
def test_utc_conversion_overflow_declines_instead_of_crashing(extreme):
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    state = replace(state, drops=(replace(state.drops[0], starts_at=extreme), state.drops[1]))
    assert analyze(state).reason == "invalid_snapshot"
