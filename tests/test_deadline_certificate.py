"""Continuous bounds require a fixed schedule, not endpoint-only simulation."""

from dataclasses import replace
from datetime import timedelta
from itertools import product

import pytest

from src.services.deadline_certificate import (
    CertifiedDeadlineForecaster,
    DropTimingBounds,
    DurationBounds,
    TimingBounds,
)
from src.services.deadline_forecast import MiningWindow, ViewingWindow
from tests.test_deadline_forecast import NOW, reward, snapshot


ZERO = timedelta(0)


def bounds(state, *, work=None, claims=None, setup=10):
    work, claims = work or {}, claims or {}
    return TimingBounds(tuple(DropTimingBounds(
        drop.id,
        work.get(drop.id, DurationBounds(
            timedelta(minutes=drop.required_minutes - (drop.observed_minutes or 0)),
            timedelta(minutes=drop.required_minutes - (drop.observed_minutes or 0)),
        )),
        claims.get(drop.id, DurationBounds(ZERO, timedelta(seconds=20))),
    ) for drop in state.drops if not drop.claimed), DurationBounds(ZERO, timedelta(seconds=setup)))


def certify(state, timing=None, **kwargs):
    return CertifiedDeadlineForecaster(**kwargs).certify(
        state, bounds(state) if timing is None else timing, enabled=True, baseline_complete=True,
    )


def test_two_hour_rescue_has_a_fixed_worst_case_return_schedule():
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    result = certify(state)
    plan = result.certificate
    assert result.reason == "rescue"
    assert plan.target_id == "urgent" and plan.blocker_id == "high"
    assert plan.protected_drop_ids == frozenset({"high"})
    assert [slot.drop_id for slot in plan.slots] == ["urgent", "high"]
    assert plan.slots[0].connect_at == NOW
    assert plan.slots[0].watch_from == NOW + timedelta(seconds=10)
    assert plan.slots[0].until == NOW + timedelta(hours=2, seconds=10)
    assert plan.completions[-1].claim_ready_at == NOW + timedelta(hours=10, seconds=60)


def test_every_independent_interior_setup_and_claim_delay_fits_reserved_slots():
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    plan = certify(state).certificate
    # Independent interior values, not one constant delay per complete execution.
    for outbound, returning, claim in product((0, 1, 4, 9, 10), (0, 3, 7, 10), (0, 1, 11, 20)):
        first, second = plan.slots
        assert first.connect_at + timedelta(seconds=outbound) <= first.watch_from
        urgent_done = first.watch_from + timedelta(hours=2)
        assert urgent_done + timedelta(seconds=claim) <= second.connect_at
        assert second.connect_at + timedelta(seconds=returning) <= second.watch_from
        assert second.watch_from + timedelta(hours=8) == second.until


def test_continuous_work_ranges_use_lower_bound_for_blocking_upper_for_slots():
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    options = bounds(state, work={
        "high": DurationBounds(timedelta(hours=7, minutes=59), timedelta(hours=8)),
        "urgent": DurationBounds(timedelta(minutes=119), timedelta(hours=2)),
    })
    assert certify(state, options).certificate is not None
    options = replace(options, drops=(
        replace(options.drops[0], remaining=DurationBounds(timedelta(hours=3), timedelta(hours=8))),
        options.drops[1],
    ))
    assert certify(state, options).certificate is None


def test_prerequisites_have_own_work_and_bounded_claim_waits():
    parent = reward("parent", 2, 30, 4, wanted=False)
    urgent = reward("urgent", 2, 90, 4, prerequisite_ids=("parent",))
    state = snapshot(reward("high", 1, 480, 48), parent, urgent)
    options = bounds(state, claims={"parent": DurationBounds(ZERO, timedelta(seconds=73))})
    plan = certify(state, options).certificate
    assert [slot.drop_id for slot in plan.slots] == ["parent", "urgent", "high"]
    assert plan.slots[1].connect_at == plan.completions[0].claim_ready_at
    assert plan.completions[0].claim_ready_at == NOW + timedelta(minutes=30, seconds=83)


def test_mining_pause_and_channel_reconnect_each_reserve_setup():
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 5))
    state = replace(state, mining_windows=(
        MiningWindow(NOW, NOW + timedelta(hours=1)),
        MiningWindow(NOW + timedelta(hours=2), state.horizon),
    ))
    plan = certify(state).certificate
    urgent_slots = [slot for slot in plan.slots if slot.drop_id == "urgent"]
    assert len(urgent_slots) == 2
    assert urgent_slots[1].connect_at == NOW + timedelta(hours=2)
    assert urgent_slots[1].watch_from == NOW + timedelta(hours=2, seconds=10)
    assert urgent_slots[-1].until == NOW + timedelta(hours=3, seconds=20)


def test_all_higher_and_equal_ranked_wanted_rewards_are_protected():
    state = snapshot(
        reward("high", 1, 480, 48), reward("same-rank", 2, 60, 36),
        reward("urgent", 2, 120, 4),
    )
    plan = certify(state).certificate
    assert plan.target_id == "urgent"
    assert plan.protected_drop_ids == frozenset({"high", "same-rank"})
    assert {item.drop_id for item in plan.completions} == {"high", "urgent", "same-rank"}


@pytest.mark.parametrize("kind", ["rank_conflict", "shared_credit", "blocker_expires", "not_ready", "zero_work"])
def test_certificate_requires_proof_that_ranked_work_would_miss_target(kind):
    high = reward("high", 1, 480, 48)
    urgent = reward("urgent", 2, 120, 4)
    if kind == "rank_conflict":
        high = replace(high, ends_at=NOW + timedelta(hours=9))
    elif kind == "blocker_expires":
        high = replace(high, ends_at=NOW + timedelta(hours=3))
    elif kind == "not_ready":
        high = replace(high, starts_at=NOW + timedelta(seconds=1))
    state = snapshot(high, urgent)
    if kind == "shared_credit":
        state = replace(state, viewing_windows=(replace(
            state.viewing_windows[0], drop_ids=frozenset({"high", "urgent"}),
        ),))
    options = bounds(state)
    if kind == "zero_work":
        options = replace(options, drops=(options.drops[0], replace(
            options.drops[1], remaining=DurationBounds(ZERO, timedelta(hours=2)),
        )))
    assert certify(state, options).certificate is None


def test_any_higher_channel_that_can_also_credit_target_defeats_blocking_proof():
    state = snapshot(
        reward("high", 1, 480, 48), reward("other-high", 1, 60, 48),
        reward("urgent", 2, 120, 4),
    )
    shared = ViewingWindow("shared", NOW, state.horizon,
                           frozenset({"other-high", "urgent"}), allocation_known=True)
    state = replace(state, viewing_windows=(*state.viewing_windows, shared))
    assert certify(state).certificate is None


@pytest.mark.parametrize("field", ["remaining", "claim_delay", "setup"])
def test_unknown_bounds_are_not_assumed_zero(field):
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    options = bounds(state)
    if field == "setup":
        options = replace(options, setup=None)
    else:
        options = replace(options, drops=(replace(options.drops[0], **{field: None}), options.drops[1]))
    result = certify(state, options)
    assert result.reason == "unknown_timing" and result.certificate is None


@pytest.mark.parametrize("value", [None, 0, "false"])
def test_unknown_manual_override_is_not_automatic_mode(value):
    result = CertifiedDeadlineForecaster().certify(None, None, enabled=True, manual_override=value)
    assert result.reason == "invalid_snapshot"


@pytest.mark.parametrize("enabled,manual,reason", [(False, False, "disabled"), (True, True, "manual_override")])
def test_disabled_and_manual_gates_precede_input_processing(enabled, manual, reason):
    assert CertifiedDeadlineForecaster().certify(
        None, None, enabled=enabled, manual_override=manual,
    ).reason == reason


@pytest.mark.parametrize("bad", [True, (), DurationBounds(timedelta(seconds=-1), ZERO),
                                  DurationBounds(timedelta(seconds=2), timedelta(seconds=1))])
def test_malformed_bounds_fail_closed(bad):
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    options = replace(bounds(state), setup=bad)
    assert certify(state, options).reason == "invalid_snapshot"


def test_budget_exhaustion_returns_no_partial_certificate():
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    result = certify(state, max_steps=1)
    assert result.reason == "budget_exceeded" and result.certificate is None


def test_inputs_are_not_changed_and_current_channel_only_avoids_first_setup():
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4), current_channel_id="2")
    options = bounds(state)
    before = repr((state, options))
    plan = certify(state, options).certificate
    assert plan.slots[0].watch_from == NOW
    assert plan.slots[1].watch_from - plan.slots[1].connect_at == timedelta(seconds=10)
    assert repr((state, options)) == before


@pytest.mark.parametrize("kind", ["none", "list", "duplicate", "foreign", "missing", "too_many", "bad_item"])
def test_timing_domain_shapes_and_ids_are_bounded(kind):
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    options = bounds(state)
    if kind == "none":
        options = None
    elif kind == "list":
        options = replace(options, drops=list(options.drops))
    elif kind == "duplicate":
        options = replace(options, drops=options.drops + options.drops[:1])
    elif kind == "foreign":
        options = replace(options, drops=(replace(options.drops[0], drop_id="foreign"),))
    elif kind == "missing":
        options = replace(options, drops=options.drops[:1])
    elif kind == "too_many":
        options = replace(options, drops=options.drops * 129)
    else:
        options = replace(options, drops=(None,))
    result = certify(state, options) if options is not None else CertifiedDeadlineForecaster().certify(
        state, options, enabled=True, baseline_complete=True,
    )
    assert result.certificate is None
    assert result.reason in {"invalid_snapshot", "unknown_timing", "budget_exceeded"}


def test_remaining_bounds_cannot_contradict_the_direct_minute_reading():
    high = replace(reward("high", 1, 480, 48), observed_minutes=470)
    state = snapshot(high, reward("urgent", 2, 120, 4))
    options = bounds(state, work={"high": DurationBounds(timedelta(hours=8), timedelta(hours=8))})
    assert certify(state, options).reason == "invalid_snapshot"


@pytest.mark.parametrize("offset,expected", [(-1, False), (0, False), (1, True)])
def test_watching_must_finish_strictly_before_individual_deadline(offset, expected):
    urgent = replace(reward("urgent", 2, 120, 4),
                     ends_at=NOW + timedelta(hours=2, seconds=10, microseconds=offset))
    state = snapshot(reward("high", 1, 480, 48), urgent)
    assert (certify(state).certificate is not None) is expected


def test_expired_or_unavailable_protected_work_does_not_get_a_partial_certificate():
    state = snapshot(
        reward("high", 1, 480, 48), reward("urgent", 2, 120, 4),
        replace(reward("future-high", 1, 120, 48), starts_at=NOW + timedelta(days=4)),
    )
    assert certify(state).certificate is None


def test_claim_settlement_past_horizon_is_not_reported_as_ready_at_horizon():
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    state = replace(state, horizon=NOW + timedelta(hours=10, seconds=50))
    assert certify(state).certificate is None


def test_channel_window_too_short_for_setup_cannot_earn_credit():
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    state = replace(state, viewing_windows=(state.viewing_windows[0], replace(
        state.viewing_windows[1], ends_at=NOW + timedelta(seconds=10),
    )))
    assert certify(state).certificate is None


def test_no_immediate_work_does_not_authorize_a_future_channel_switch_now():
    urgent = replace(reward("urgent", 2, 120, 4), starts_at=NOW + timedelta(minutes=1))
    state = snapshot(reward("high", 1, 480, 48), urgent)
    assert certify(state).certificate is None


def test_unselected_unknown_work_does_not_require_fake_bounds():
    state = snapshot(
        reward("high", 1, 480, 48), reward("urgent", 2, 120, 4),
        replace(reward("unselected", 4, 60, 48), observed_minutes=None, observed_at=None),
    )
    options = replace(bounds(state), drops=bounds(state).drops[:2])
    assert certify(state, options).certificate is not None


def test_small_serial_oracle_checks_actual_interior_work_against_fixed_calendars():
    accepted = refused = 0
    for high_minutes, urgent_minutes, urgent_end, high_end in product(
        (4, 6, 8), (1, 2), (2, 3, 4), (8, 12),
    ):
        high = reward("high", 1, high_minutes, high_end / 60)
        urgent = reward("urgent", 2, urgent_minutes, urgent_end / 60)
        state = snapshot(high, urgent)
        options = bounds(state, setup=1, work={
            "high": DurationBounds(timedelta(minutes=high_minutes - 1), timedelta(minutes=high_minutes)),
            "urgent": DurationBounds(timedelta(minutes=urgent_minutes, seconds=-30), timedelta(minutes=urgent_minutes)),
        }, claims={
            key: DurationBounds(ZERO, timedelta(seconds=2)) for key in ("high", "urgent")
        })
        result = certify(state, options)
        if result.certificate is None:
            refused += 1
            continue
        accepted += 1
        first, second = result.certificate.slots
        assert [first.drop_id, second.drop_id] == ["urgent", "high"]
        # Interior values are replayed on the fixed calendar, not a greedy detour.
        for high_offset, urgent_offset in product((0, 0.3, 1), (0, 0.2, 0.5)):
            high_work = timedelta(minutes=high_minutes - high_offset)
            urgent_work = timedelta(minutes=urgent_minutes - urgent_offset)
            baseline_start = min(high_work, timedelta(minutes=high_end))
            assert baseline_start + urgent_work >= timedelta(minutes=urgent_end)
            urgent_done = first.watch_from + urgent_work
            high_done = second.watch_from + high_work
            assert urgent_done < urgent.ends_at
            assert urgent_done + timedelta(seconds=2) <= second.connect_at
            assert high_done < high.ends_at
            assert high_done <= second.until
    assert accepted > 5 and refused > 5


@pytest.mark.parametrize("coverage", [False, None, 0, 1, "true"])
def test_unknown_or_nonboolean_baseline_coverage_is_not_certified(coverage):
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    result = CertifiedDeadlineForecaster().certify(
        state, bounds(state), enabled=True, baseline_complete=coverage,
    )
    assert result.reason == "unknown_coverage" and result.certificate is None


def test_complete_baseline_coverage_requires_explicit_opt_in():
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    assert CertifiedDeadlineForecaster().certify(state, bounds(state), enabled=True).reason == "unknown_coverage"
