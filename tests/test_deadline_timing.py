"""One immediate rescue must remain safe across every supplied timing alternative."""

from dataclasses import replace
from datetime import timedelta

import pytest

from src.services.deadline_forecast import (
    DeadlineForecaster,
    DropTimingOptions,
    MiningWindow,
    TimingUncertainty,
    ViewingWindow,
)
from tests.test_deadline_forecast import NOW, reward, snapshot


ZERO = timedelta(0)


def timing(state, *, progress=None, claims=None, switches=(ZERO,)):
    progress, claims = progress or {}, claims or {}
    return TimingUncertainty(tuple(DropTimingOptions(
        drop.id, progress.get(drop.id, (ZERO,)), claims.get(drop.id, (drop.claim_delay,)),
    ) for drop in state.drops if not drop.claimed), switch_delays=switches)


def analyze(state, options=None, **kwargs):
    return DeadlineForecaster(**kwargs).analyze_robust(
        state, timing(state) if options is None else options, enabled=True,
    )


def test_zero_uncertainty_matches_existing_conditional_analysis():
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    engine = DeadlineForecaster()
    assert analyze(state) == engine.analyze(state, enabled=True)


def test_rescue_must_survive_every_switch_delay_not_just_the_fastest():
    state = snapshot(reward("high", 1, 480, 10 + 15 / 3600), reward("urgent", 2, 120, 3))
    state = replace(state, current_channel_id="1")
    assert analyze(state).plan is not None
    result = analyze(state, timing(state, switches=(ZERO, timedelta(seconds=10))))
    assert result.plan is None


def test_robust_plan_accounts_for_outbound_and_return_switches():
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    state = replace(state, current_channel_id="1")
    result = analyze(state, timing(state, switches=(ZERO, timedelta(seconds=10))))
    assert result.plan.drop_id == "urgent"
    assert result.plan.earned_at == NOW + timedelta(hours=2, seconds=10)
    assert result.plan.protected_drop_ids == frozenset({"high"})


def test_partial_progress_can_make_the_rescue_unnecessary_in_one_scenario():
    state = snapshot(reward("high", 1, 240, 48), reward("urgent", 2, 120, 5))
    assert analyze(state).plan is not None
    options = timing(state, progress={"high": (ZERO, timedelta(hours=2))})
    result = analyze(state, options)
    assert result.plan is None
    assert "urgent" in result.baseline_earned_ids


def test_fractional_progress_is_separate_from_the_direct_minute_reading():
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    before = repr(state)
    result = analyze(state, timing(state, progress={"urgent": (timedelta(seconds=45),)}))
    assert result.plan.earned_at == NOW + timedelta(hours=2, seconds=-45)
    assert state.drops[1].observed_minutes == 0
    assert repr(state) == before


@pytest.mark.parametrize("slow_delay,expected", [(40, True), (60, False)])
def test_pending_prerequisite_claim_delays_are_enumerated(slow_delay, expected):
    parent = reward("parent", 2, 1, 4, wanted=False)
    target = replace(reward("urgent", 2, 1, 3, prerequisite_ids=("parent",)),
                     ends_at=NOW + timedelta(seconds=170))
    state = snapshot(reward("high", 1, 480, 48), parent, target)
    options = timing(state, claims={"parent": (timedelta(seconds=20), timedelta(seconds=slow_delay))})
    result = analyze(state, options)
    assert (result.plan is not None) is expected
    if expected:
        assert result.plan.earned_at == NOW + timedelta(seconds=160)


def test_common_later_candidate_is_found_when_scenarios_prefer_different_first_candidates():
    state = snapshot(
        reward("high", 1, 480, 9.5), reward("a", 2, 120, 3),
        reward("b", 3, 120, 4), reward("c", 3, 60, 6),
    )
    options = timing(state, progress={
        "a": (ZERO, timedelta(hours=1)), "b": (ZERO, timedelta(hours=1)),
    })
    result = analyze(state, options)
    assert result.plan.drop_id == "c"


@pytest.mark.parametrize("field", ["progress_offsets", "claim_delays", "switch_delays"])
def test_unbounded_timing_uncertainty_is_not_assumed_zero(field):
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    options = timing(state)
    if field == "switch_delays":
        options = replace(options, switch_delays=None)
    else:
        options = replace(options, drops=(replace(options.drops[0], **{field: None}), options.drops[1]))
    assert analyze(state, options).reason == "unknown_timing"


def test_cartesian_limit_declines_without_sampling_or_partial_recommendations():
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    alternatives = tuple(timedelta(seconds=index) for index in range(9))
    options = timing(state, progress={"high": alternatives, "urgent": alternatives})
    assert analyze(state, options).reason == "budget_exceeded"


def test_scenarios_share_one_transition_budget():
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    options = timing(state, switches=(ZERO, timedelta(seconds=10)))
    assert analyze(state, options, max_steps=4).reason == "budget_exceeded"


@pytest.mark.parametrize("enabled,manual,reason", [
    (False, False, "disabled"), (True, True, "manual_override"),
])
def test_robust_entry_point_preserves_disabled_and_manual_gates(enabled, manual, reason):
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    result = DeadlineForecaster().analyze_robust(state, None, enabled=enabled, manual_override=manual)
    assert result.reason == reason and result.plan is None


@pytest.mark.parametrize("kind", ["duplicate", "foreign", "empty", "negative", "over_requirement", "non_duration", "missing"])
def test_malformed_timing_options_are_rejected(kind):
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    options = timing(state)
    if kind == "duplicate":
        options = replace(options, drops=options.drops + options.drops[:1])
    elif kind == "foreign":
        options = replace(options, drops=(replace(options.drops[0], drop_id="missing"), options.drops[1]))
    elif kind == "empty":
        options = replace(options, switch_delays=())
    elif kind == "negative":
        options = replace(options, switch_delays=(timedelta(seconds=-1),))
    elif kind == "over_requirement":
        options = replace(options, drops=(replace(options.drops[0], progress_offsets=(timedelta(days=365),)), options.drops[1]))
    elif kind == "non_duration":
        options = replace(options, switch_delays=(True,))
    elif kind == "missing":
        options = replace(options, drops=options.drops[:1])
    result = analyze(state, options)
    assert result.reason in {"invalid_snapshot", "unknown_timing"}
    assert result.plan is None


def test_options_order_and_duplicates_do_not_change_the_result():
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    options = timing(state, switches=(timedelta(seconds=10), ZERO, ZERO))
    reordered = replace(options, drops=tuple(reversed(options.drops)), switch_delays=(ZERO, timedelta(seconds=10)))
    assert analyze(state, options) == analyze(state, reordered)


def test_unlisted_timing_domains_do_not_block_selected_work():
    unlisted = replace(reward("unlisted", 4, 60, 48), observed_minutes=None, observed_at=None)
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4), unlisted)
    options = timing(state)
    options = replace(options, drops=tuple(
        replace(option, progress_offsets=None, claim_delays=None) if option.drop_id == "unlisted" else option
        for option in options.drops
    ))
    assert analyze(state, options).plan.drop_id == "urgent"


@pytest.mark.parametrize("method", ["analyze", "analyze_robust"])
@pytest.mark.parametrize("manual", [None, 0, "false"])
def test_unknown_manual_state_never_authorizes_a_rescue(method, manual):
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    function = getattr(DeadlineForecaster(), method)
    args = (state,) if method == "analyze" else (state, timing(state))
    assert function(*args, enabled=True, manual_override=manual).reason == "invalid_snapshot"


def test_different_required_initial_channels_across_scenarios_decline():
    parent = reward("parent", 2, 1, 4, wanted=False)
    target = reward("urgent", 2, 1, 4, prerequisite_ids=("parent",))
    state = snapshot(reward("high", 1, 480, 48), parent, target)
    state = replace(state, viewing_windows=(state.viewing_windows[0], *(
        ViewingWindow(drop.id, NOW, state.horizon, frozenset({drop.id}), allocation_known=True)
        for drop in (parent, target)
    )))
    options = timing(state, progress={"parent": (ZERO, timedelta(minutes=1))}, claims={"parent": (ZERO,)})
    assert analyze(state, options).plan is None


def test_mining_pause_requires_reconnection_without_credit_during_either_setup():
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    state = replace(state, current_channel_id="1", mining_windows=(
        MiningWindow(NOW, NOW + timedelta(hours=1)),
        MiningWindow(NOW + timedelta(hours=2), state.horizon),
    ))
    result = analyze(state, timing(state, switches=(timedelta(seconds=10),)))
    assert result.plan.earned_at == NOW + timedelta(hours=3, seconds=20)


def test_channel_change_mid_setup_does_not_credit_unfinished_setup_time():
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    urgent = state.viewing_windows[1]
    state = replace(state, current_channel_id="1", viewing_windows=(
        state.viewing_windows[0], replace(urgent, ends_at=NOW + timedelta(seconds=5)),
        ViewingWindow("2", NOW + timedelta(hours=1), state.horizon, urgent.drop_ids, allocation_known=True),
    ))
    result = analyze(state, timing(state, switches=(timedelta(seconds=10),)))
    assert result.plan.earned_at == NOW + timedelta(hours=3, seconds=10)


@pytest.mark.parametrize("kind", ["none", "drops_none", "option_none", "too_many_options", "too_many_values", "list_values", "overlong_claim"])
def test_timing_shape_and_count_limits_fail_closed(kind):
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    options = timing(state)
    if kind == "none":
        options = None
    elif kind == "drops_none":
        options = replace(options, drops=None)
    elif kind == "option_none":
        options = replace(options, drops=(None,))
    elif kind == "too_many_options":
        options = replace(options, drops=options.drops[:1] * 257)
    elif kind == "too_many_values":
        options = replace(options, switch_delays=(ZERO,) * 65)
    elif kind == "list_values":
        options = replace(options, switch_delays=[ZERO])
    elif kind == "overlong_claim":
        options = replace(options, drops=(replace(options.drops[0], claim_delays=(timedelta(days=2),)), options.drops[1]))
    result = DeadlineForecaster().analyze_robust(state, options, enabled=True)
    assert result.reason in {"invalid_snapshot", "budget_exceeded"}
    assert result.plan is None


@pytest.mark.parametrize("channel", [False, 1, ""])
def test_malformed_current_channel_is_not_treated_as_free_startup(channel):
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    state = replace(state, current_channel_id=channel)
    assert analyze(state).reason == "invalid_snapshot"


def test_already_claimed_ancestor_needs_no_progress_or_timing_alternatives():
    parent = replace(reward("parent", 2, 60, 4, claimed=True, wanted=False),
                     observed_minutes=None, observed_at=None)
    target = reward("urgent", 2, 60, 3, prerequisite_ids=("parent",))
    state = snapshot(reward("high", 1, 480, 48), parent, target)
    result = analyze(state)
    assert result.plan.drop_id == "urgent"
    assert result.plan.earned_at == NOW + timedelta(hours=1)


def test_incomplete_horizon_refusal_reports_the_union_of_all_baselines():
    state = snapshot(reward("high", 1, 4, 1), reward("urgent", 2, 2, 1))
    state = replace(state, horizon=NOW + timedelta(minutes=3))
    options = timing(state, progress={"high": (ZERO, timedelta(minutes=3))})
    result = analyze(state, options)
    assert result.reason == "incomplete_horizon" and result.plan is None
    assert result.baseline_earned_ids == frozenset({"high", "urgent"})


def test_paused_current_work_refusal_reports_the_union_of_all_baselines():
    state = snapshot(reward("high", 1, 60, 1.5), reward("urgent", 2, 1, 1.5))
    state = replace(state, mining_windows=(MiningWindow(NOW + timedelta(hours=1), state.horizon),))
    options = timing(state, progress={"high": (ZERO, timedelta(minutes=45))})
    result = analyze(state, options)
    assert result.reason == "no_ranked_work" and result.plan is None
    assert result.baseline_earned_ids == frozenset({"high", "urgent"})
