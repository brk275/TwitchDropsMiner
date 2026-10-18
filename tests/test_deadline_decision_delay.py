"""Reserve calculation time without authorizing live calendar execution."""

import copy
from dataclasses import replace
from datetime import timedelta, timezone
from itertools import product
from unittest.mock import MagicMock

import pytest

from src.services.deadline_certificate import CertifiedDeadlineForecaster, DurationBounds
from src.services.deadline_forecast import MiningWindow
from tests.test_deadline_certificate import bounds
from tests.test_deadline_context import state as context_state
from tests.test_deadline_forecast import NOW, reward, snapshot
from tests.test_deadline_planner import HORIZON, assumptions
from tests.test_deadline_planner import planning as planner_state


@pytest.fixture
def delayed(monkeypatch):
    return planner_state.__wrapped__(monkeypatch)


def certify_at(state, stamp):
    return CertifiedDeadlineForecaster().certify(
        state, bounds(state), enabled=True, baseline_complete=True, not_before=stamp,
    )


def test_calendar_reserves_a_future_start_instead_of_rewriting_snapshot_time():
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    result = CertifiedDeadlineForecaster().certify(
        state, bounds(state), enabled=True, baseline_complete=True,
        not_before=NOW + timedelta(seconds=10),
    )
    assert result.reason == "rescue"
    assert result.certificate.slots[0].connect_at == NOW + timedelta(seconds=10)
    assert result.certificate.slots[0].watch_from == NOW + timedelta(seconds=20)
    assert state.now == NOW


def test_planner_reserves_decision_allowance_and_bounds_context(monkeypatch):
    state = planner_state.__wrapped__(monkeypatch)
    runtime, _, planner, _ = state
    result = planner.plan(runtime[0], horizon=HORIZON, assumptions=assumptions(state),
                          enabled=True, decision_delay=timedelta(seconds=10))
    assert result.reason == "rescue"
    assert result.certificate.slots[0].connect_at == NOW + timedelta(seconds=10)
    assert result.valid_until == NOW + timedelta(seconds=10)


def test_context_expires_at_the_earlier_explicit_deadline():
    twitch, _, _, guard, _ = context_state.__wrapped__()
    result = guard.capture(twitch, now=NOW, horizon=HORIZON, enabled=True,
                           expires_at=NOW + timedelta(seconds=3))
    assert result.reason == "captured"
    assert result.bound.valid_until == NOW + timedelta(seconds=3)
    assert guard.validate(result.bound, twitch, now=NOW + timedelta(seconds=3), enabled=True) == "expired"


@pytest.mark.parametrize("stamp", [None, NOW, NOW.astimezone(timezone(timedelta(hours=5, minutes=45)))])
def test_omitted_or_equal_reservation_keeps_existing_certificate(stamp):
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    assert certify_at(state, stamp) == certify_at(state, NOW)


@pytest.mark.parametrize("stamp", [
    False, 1, "tomorrow", NOW.replace(tzinfo=None), NOW - timedelta(microseconds=1),
    NOW + timedelta(days=3), NOW + timedelta(days=4),
])
def test_invalid_calendar_start_never_produces_a_partial_plan(stamp):
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    result = certify_at(state, stamp)
    assert result.reason == "invalid_snapshot" and result.certificate is None


def test_waiting_reduces_capacity_for_both_target_and_protected_rewards():
    urgent_end = NOW + timedelta(hours=2, seconds=10, microseconds=500_000)
    state = snapshot(reward("high", 1, 480, 48), replace(reward("urgent", 2, 120, 4), ends_at=urgent_end))
    assert certify_at(state, NOW).reason == "rescue"
    assert certify_at(state, NOW + timedelta(seconds=1)).reason == "no_safe_rescue"
    state = snapshot(replace(reward("high", 1, 480, 48), ends_at=NOW + timedelta(hours=10, seconds=45)),
                     reward("urgent", 2, 120, 4))
    assert certify_at(state, NOW).reason == "rescue"
    assert certify_at(state, NOW + timedelta(seconds=10)).reason == "no_safe_rescue"


def test_future_start_does_not_rebase_observation_age_or_baseline_blocker():
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    state = replace(state, drops=tuple(replace(drop, observed_at=NOW - timedelta(seconds=299))
                                       for drop in state.drops))
    before = repr(state)
    result = certify_at(state, NOW + timedelta(hours=1))
    assert result.reason == "rescue" and result.certificate.blocker_id == "high"
    assert result.certificate.slots[0].connect_at == NOW + timedelta(hours=1)
    assert repr(state) == before


def test_future_start_cannot_manufacture_a_baseline_blocker():
    state = snapshot(reward("high", 1, 4, 48), replace(
        reward("urgent", 2, 1, 4), ends_at=NOW + timedelta(seconds=241),
    ))
    zero = DurationBounds(timedelta(0), timedelta(0))
    tenth = timedelta(milliseconds=100)
    timing = bounds(state, setup=0, work={"urgent": DurationBounds(tenth, tenth)},
                    claims={"high": zero, "urgent": zero})
    # In this hypothetical case, rank-only work can finish both by 240.1s.
    # Moving the proof's now forward would incorrectly turn 240s into a blocker.
    result = CertifiedDeadlineForecaster().certify(
        state, timing, enabled=True, baseline_complete=True, not_before=NOW + timedelta(seconds=2),
    )
    assert result.reason == "no_safe_rescue" and result.certificate is None


def test_delayed_start_does_not_assume_existing_connection_stays_ready():
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4), current_channel_id="2")
    assert certify_at(state, NOW).certificate.slots[0].watch_from == NOW
    result = certify_at(state, NOW + timedelta(seconds=2))
    assert result.certificate.slots[0].watch_from == NOW + timedelta(seconds=12)


@pytest.mark.parametrize("gap", ["mining", "channel"])
def test_first_slot_must_start_at_reserved_instant_not_an_unbooked_later_window(gap):
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    opening = NOW + timedelta(seconds=20)
    if gap == "mining":
        state = replace(state, mining_windows=(MiningWindow(opening, state.horizon),))
    else:
        state = replace(state, viewing_windows=(state.viewing_windows[0],
                                                replace(state.viewing_windows[1], starts_at=opening)))
    assert certify_at(state, NOW + timedelta(seconds=10)).reason == "no_safe_rescue"
    assert certify_at(state, opening).certificate.slots[0].connect_at == opening


def test_serial_deadline_oracle_charges_decision_setup_and_claim_waits():
    cases = 0
    for delay, setup, claim, slack in product((0, 1, 5, 15), (0, 3, 10), (0, 7, 20), (0, 1, 7, 30, 90)):
        state = snapshot(
            replace(reward("high", 1, 6, 48), ends_at=NOW + timedelta(minutes=8, seconds=slack)),
            reward("urgent", 2, 2, 4 / 60),
        )
        timing = bounds(state, setup=setup)
        timing = replace(timing, drops=tuple(replace(item, claim_delay=replace(
            item.claim_delay, upper=timedelta(seconds=claim),
        )) for item in timing.drops))
        result = CertifiedDeadlineForecaster().certify(
            state, timing, enabled=True, baseline_complete=True,
            not_before=NOW + timedelta(seconds=delay),
        )
        assert (result.certificate is not None) is (delay + 2 * setup + claim < slack)
        if result.certificate is not None:
            assert result.certificate.slots[0].connect_at == NOW + timedelta(seconds=delay)
            assert result.certificate.completions[1].earned_at == (
                NOW + timedelta(minutes=8, seconds=delay + 2 * setup + claim)
            )
        cases += 1
    assert cases == 180


@pytest.mark.parametrize("delay", [False, 1, "soon", timedelta(0), timedelta(microseconds=-1), timedelta(seconds=16)])
def test_planner_rejects_unknown_or_invalid_delay_before_reading_client(delayed, monkeypatch, delay):
    planner = delayed[2]
    capture = MagicMock(side_effect=AssertionError("Invalid allowance must not inspect client"))
    monkeypatch.setattr(planner._guard, "capture", capture)
    result = planner.plan(object(), horizon=HORIZON, enabled=True, decision_delay=delay)
    assert result.reason == "invalid_context" and result.certificate is None
    capture.assert_not_called()


@pytest.mark.parametrize("wall,tick,expected", [
    (2.999, 2.999, "rescue"), (3, 0, "expired"), (0, 3, "expired"),
    (3.001, 3.001, "expired"), (-1, 0, "expired"), (0, float("nan"), "invalid_context"),
])
def test_both_clocks_enforce_exclusive_decision_budget(delayed, monkeypatch, wall, tick, expected):
    runtime, _, planner, walls = delayed
    premises = assumptions(delayed)
    original = planner.certify

    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        walls[0] += timedelta(seconds=wall)
        runtime[4][0] += tick
        return result

    monkeypatch.setattr(planner, "certify", slow)
    result = planner.plan(runtime[0], horizon=HORIZON, assumptions=premises,
                          enabled=True, decision_delay=timedelta(seconds=3))
    assert result.reason == expected
    assert (result.certificate is not None) is (expected == "rescue")
    if result.certificate is not None:
        assert result.certificate.slots[0].connect_at == NOW + timedelta(seconds=3)
        assert result.valid_until == NOW + timedelta(seconds=3)


def test_allowance_cannot_outlive_known_model_boundary(delayed, monkeypatch):
    runtime, _, planner, _ = delayed
    premises = assumptions(delayed)
    runtime[1].ends_at = NOW + timedelta(seconds=2)
    proof = MagicMock(side_effect=AssertionError("Known context expiry precedes reservation"))
    monkeypatch.setattr(planner, "certify", proof)
    result = planner.plan(runtime[0], horizon=HORIZON, assumptions=premises,
                          enabled=True, decision_delay=timedelta(seconds=3))
    assert result.reason == "expired" and result.certificate is None
    proof.assert_not_called()


def test_future_slot_does_not_grant_current_channel_eligibility(delayed):
    runtime, _, planner, _ = delayed
    premises = assumptions(delayed)
    runtime[0].channels[20]._stream = None
    premises = replace(premises, viewing_windows=(premises.viewing_windows[0],
                                                  replace(premises.viewing_windows[1], starts_at=NOW + timedelta(seconds=3))))
    result = planner.plan(runtime[0], horizon=HORIZON, assumptions=premises,
                          enabled=True, decision_delay=timedelta(seconds=3))
    assert result.reason == "checkpoint_mismatch" and result.certificate is None


def test_reserved_plan_is_still_bound_to_exact_model_identity(delayed, monkeypatch):
    runtime, _, planner, _ = delayed
    premises = assumptions(delayed)
    original = planner.certify

    def replaced(*args, **kwargs):
        result = original(*args, **kwargs)
        runtime[2]._stream = copy.copy(runtime[2]._stream)
        return result

    monkeypatch.setattr(planner, "certify", replaced)
    result = planner.plan(runtime[0], horizon=HORIZON, assumptions=premises,
                          enabled=True, decision_delay=timedelta(seconds=3))
    assert result.reason == "superseded" and result.certificate is None


@pytest.mark.parametrize("deadline,expected", [
    (NOW, "expired"), (NOW - timedelta(seconds=1), "expired"),
    (NOW.replace(tzinfo=None), "invalid_snapshot"), ("tomorrow", "invalid_snapshot"),
])
def test_invalid_deadline_retires_previous_binding(deadline, expected):
    twitch, _, _, guard, _ = context_state.__wrapped__()
    old = guard.capture(twitch, now=NOW, horizon=HORIZON, enabled=True).bound
    result = guard.capture(twitch, now=NOW, horizon=HORIZON, enabled=True, expires_at=deadline)
    assert result.reason == expected and result.bound is None
    assert guard.validate(old, twitch, now=NOW, enabled=True) == "superseded"


def test_explicit_deadline_cannot_extend_configured_context_age():
    twitch, _, _, guard, _ = context_state.__wrapped__()
    result = guard.capture(twitch, now=NOW, horizon=HORIZON, enabled=True,
                           expires_at=NOW + timedelta(minutes=1))
    assert result.bound.valid_until == NOW + timedelta(seconds=15)


def test_capture_processing_also_consumes_explicit_deadline(delayed, monkeypatch):
    runtime, _, planner, _ = delayed
    premises = assumptions(delayed)
    original = planner._guard._channels.capture
    proof = MagicMock(side_effect=AssertionError("No decision allowance remains"))

    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        runtime[4][0] += 3
        return result

    monkeypatch.setattr(planner._guard._channels, "capture", slow)
    monkeypatch.setattr(planner, "certify", proof)
    result = planner.plan(runtime[0], horizon=HORIZON, assumptions=premises,
                          enabled=True, decision_delay=timedelta(seconds=3))
    assert result.reason == "expired" and result.certificate is None
    proof.assert_not_called()


@pytest.mark.parametrize("lag,delay,expected", [
    (299, 1, "rescue"), (299, 1.001, "unknown_progress"),
    (300, 1, "unknown_progress"), (0, 15, "rescue"),
])
def test_delayed_planner_rejects_readings_that_will_age_out_before_start(delayed, lag, delay, expected):
    runtime, _, planner, _ = delayed
    for item in runtime[0].inventory:
        for drop in item.drops:
            drop._progress_observation["received_at"] = (NOW - timedelta(seconds=lag)).isoformat()
    result = planner.plan(runtime[0], horizon=HORIZON, assumptions=assumptions(delayed),
                          enabled=True, decision_delay=timedelta(seconds=delay))
    assert result.reason == expected
    assert (result.certificate is not None) is (expected == "rescue")


@pytest.mark.parametrize("enabled,manual,coverage,expected", [
    (False, False, True, "disabled"), (True, True, True, "manual_override"),
    (True, False, False, "unknown_coverage"),
])
def test_calendar_opt_in_gates_precede_invalid_reservation(enabled, manual, coverage, expected):
    result = CertifiedDeadlineForecaster().certify(
        object(), object(), enabled=enabled, manual_override=manual,
        baseline_complete=coverage, not_before=object(),
    )
    assert result.reason == expected and result.certificate is None


def test_disabled_planner_does_not_validate_delay_or_read_clocks(delayed, monkeypatch):
    planner = delayed[2]
    clock = MagicMock(side_effect=AssertionError("Disabled"))
    monkeypatch.setattr(planner, "_utc_clock", clock)
    result = planner.plan(object(), horizon=object(), decision_delay=object())
    assert result.reason == "disabled" and result.certificate is None
    clock.assert_not_called()
