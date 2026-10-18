"""Conditional calendars use real watch calls, never live Twitch or a timer bypass."""

import copy
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from src.services.deadline_certificate import DurationBounds
from src.services.deadline_execution import DeadlineCalendarExecutor
from tests.test_deadline_forecast import NOW
from tests.test_deadline_planner import HORIZON, assumptions
from tests.test_deadline_planner import planning as planning_state


@pytest.fixture
def execution(monkeypatch):
    state = planning_state.__wrapped__(monkeypatch)
    runtime, urgent, _, walls = state
    twitch, high, _, _, ticks = runtime

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return walls[0].replace(tzinfo=None) if tz is None else walls[0].astimezone(tz)

    for module in ("src.models.campaign", "src.models.drop", "src.core.client"):
        monkeypatch.setattr(f"{module}.datetime", Clock)
    monkeypatch.setattr("src.models.channel.time", lambda: walls[0].timestamp())
    premises = assumptions(state)
    controller = DeadlineCalendarExecutor(clock=lambda: ticks[0], utc_clock=lambda: walls[0])
    twitch.print = MagicMock()
    twitch.watch = MagicMock(wraps=twitch.watch)
    twitch.stop_watching = MagicMock(wraps=twitch.stop_watching)

    def move(seconds, *, refresh=False):
        delta = NOW + timedelta(seconds=seconds) - walls[0]
        ticks[0] += delta.total_seconds()
        walls[0] += delta
        if refresh:
            for campaign in twitch.inventory:
                for drop in campaign.drops:
                    if drop.progress_observation is not None:
                        drop._progress_observation["received_at"] = walls[0].isoformat()

    return twitch, high, urgent, controller, premises, move


def reserve(state, **kwargs):
    twitch, _, _, executor, premises, _ = state
    return executor.reserve(twitch, horizon=HORIZON, assumptions=premises, enabled=True, **kwargs)


def step(state, **kwargs):
    return state[3].advance(state[0], enabled=True, **kwargs)


@pytest.mark.parametrize("enabled", [False, None, 0, 1, "yes"])
def test_disabled_execution_never_reads_client_or_clock(enabled):
    bad = MagicMock(side_effect=AssertionError("Must remain disconnected"))
    executor = DeadlineCalendarExecutor(clock=bad, utc_clock=bad)
    assert executor.reserve(object(), horizon=HORIZON, enabled=enabled).reason == "disabled"
    assert executor.advance(object(), enabled=enabled).reason == "disabled"
    bad.assert_not_called()


def test_reservation_funds_dispatch_without_starting_or_reordering_games(execution):
    twitch, _, _, executor, _, _ = execution
    result = reserve(execution)
    assert result.action == "reserved" and result.wake_at == NOW + timedelta(seconds=3)
    certificate = executor._reservation.certificate
    assert certificate.slots[0].watch_from == NOW + timedelta(seconds=15)
    assert [slot.channel_id for slot in certificate.slots] == ["20", "10"]
    assert twitch.settings.games_to_watch == ["High", "Urgent"]
    twitch.watch.assert_not_called()
    assert step(execution).action == "wait"
    assert step(execution).action == "wait"
    twitch.gui.channels.get_selection.assert_not_called()


@pytest.mark.parametrize("seconds,expected", [(2.999, "wait"), (3, "watch"), (4.9, "watch"), (5, "watch"), (5.001, "refused")])
def test_admission_has_a_funded_window_not_an_exact_timer_instant(execution, seconds, expected):
    reserve(execution)
    execution[5](seconds)
    result = step(execution)
    assert result.action == expected
    assert execution[0].watch.call_count == int(expected == "watch")
    if expected == "watch":
        assert execution[0].watching_channel.get_with_default(None) is execution[0].channels[20]


@pytest.mark.parametrize("change", ["rank", "model", "stream", "epoch", "account", "manual", "selection", "refresh", "reading"])
def test_queued_proposal_cannot_revive_after_context_changes(execution, change):
    twitch, high, _, _, _, move = execution
    reserve(execution)
    if change == "rank":
        twitch.settings.games_to_watch.reverse()
    elif change == "model":
        twitch.inventory = list(twitch.inventory)
    elif change == "stream":
        twitch.channels[20]._stream = copy.copy(twitch.channels[20]._stream)
    elif change == "epoch":
        twitch._deadline_context_epoch = object()
    elif change == "account":
        twitch._auth_state.context_epoch = object()
    elif change == "manual":
        twitch._manual_target_game = high.game
    elif change == "selection":
        twitch.gui._selected_channel_id = 999
    elif change == "refresh":
        twitch._inventory_refresh_pending = True
    else:
        high.progress_revision += 1
    move(3)
    assert step(execution).action == "refused"
    assert step(execution).reason == "superseded"
    twitch.watch.assert_not_called()
    twitch.stop_watching.assert_not_called()


@pytest.mark.parametrize("unknown", ["coverage", "allocation", "setup", "progress"])
def test_unknown_premises_never_become_zero_cost_or_best_effort(execution, unknown):
    twitch, high, _, executor, premises, _ = execution
    if unknown == "coverage":
        premises = replace(premises, baseline_complete=False)
    elif unknown == "allocation":
        premises = replace(premises, viewing_windows=tuple(replace(view, allocation_known=False)
                                                         for view in premises.viewing_windows))
    elif unknown == "setup":
        premises = replace(premises, timing=replace(premises.timing, setup=None))
    else:
        high.timed_drops["reward"]._progress_observation = None
    result = executor.reserve(twitch, horizon=HORIZON, assumptions=premises, enabled=True)
    assert result.action == "refused" and result.reason.startswith("unknown_")
    twitch.watch.assert_not_called()


def test_fixed_calendar_handoffs_to_protected_rank_and_finishes(execution):
    twitch, high, urgent, executor, _, move = execution
    reserve(execution)
    certificate = executor._reservation.certificate
    first, protected = certificate.slots
    move(4)
    assert step(execution).action == "watch"
    move(90)
    assert step(execution).action == "wait"
    assert twitch.watch.call_count == 1
    # Explicit hypothetical successful settlement, not a live/public claim or
    # evidence that current automatic refresh/reentry has been integrated.
    urgent.timed_drops["urgent"].is_claimed = True
    move((first.until - NOW).total_seconds(), refresh=True)
    assert step(execution).action == "stop"
    assert twitch.watching_channel.get_with_default(None) is None
    move((protected.connect_at - NOW).total_seconds() - 1, refresh=True)
    assert step(execution).action == "wait"
    assert twitch.watch.call_count == 1
    move((protected.connect_at - NOW).total_seconds() + 1, refresh=True)
    assert step(execution).action == "watch"
    assert twitch.watching_channel.get_with_default(None) is twitch.channels[10]
    high.timed_drops["reward"].is_claimed = True
    move((protected.until - NOW).total_seconds(), refresh=True)
    assert step(execution).action == "stop"
    assert step(execution).action == "complete"
    assert step(execution).reason == "superseded"
    assert twitch.settings.games_to_watch == ["High", "Urgent"]
    assert [call.args[0].id for call in twitch.watch.call_args_list] == [20, 10]


def test_early_completion_cannot_pull_protected_work_forward(execution):
    _, _, urgent, executor, _, move = execution
    reserve(execution)
    first, protected = executor._reservation.certificate.slots
    move(3)
    assert step(execution).action == "watch"
    urgent.timed_drops["urgent"].is_claimed = True
    move(60)
    assert step(execution).action == "stop"
    move(61)
    assert step(execution).action == "wait"
    assert execution[0].watch.call_count == 1
    move((first.until - NOW).total_seconds(), refresh=True)
    result = step(execution)
    assert result.action == "wait" and result.wake_at == protected.connect_at
    assert execution[0].watch.call_count == 1


@pytest.mark.parametrize("failure", ["stale", "missed", "claim", "pending", "offline", "manual", "replacement"])
def test_unproven_later_handoff_refuses_instead_of_steering(execution, failure):
    twitch, _, urgent, executor, _, move = execution
    reserve(execution)
    first, protected = executor._reservation.certificate.slots
    move(3)
    assert step(execution).action == "watch"
    if failure != "claim":
        urgent.timed_drops["urgent"].is_claimed = True
    move((first.until - NOW).total_seconds(), refresh=True)
    assert step(execution).action == "stop"
    move((protected.connect_at - NOW).total_seconds() + (3 if failure == "missed" else 0),
         refresh=failure != "stale")
    if failure == "stale":
        twitch.inventory[0].timed_drops["reward"]._progress_observation["received_at"] = NOW.isoformat()
    elif failure == "pending":
        twitch._games_update_pending = True
    elif failure == "offline":
        twitch.channels[10]._stream = None
    elif failure == "manual":
        twitch._manual_target_channel = twitch.channels[10]
    elif failure == "replacement":
        twitch.inventory = list(twitch.inventory)
    result = step(execution)
    assert result.action == "refused"
    assert twitch.watch.call_count == 1
    assert executor._reservation is None


@pytest.mark.parametrize("clock", ["wall", "tick"])
def test_preflight_processing_cannot_overrun_admission_window(execution, monkeypatch, clock):
    reserve(execution)
    execution[5](4)
    executor = execution[3]
    original = executor._guard.validate
    calls = [0]

    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        calls[0] += 1
        if calls[0] == 2:
            if clock == "wall":
                executor._utc_clock = lambda: NOW + timedelta(seconds=6)
            else:
                base = executor._clock()
                executor._clock = lambda: base + 2
        return result

    monkeypatch.setattr(executor._guard, "validate", slow)
    assert step(execution).action == "refused"
    execution[0].watch.assert_not_called()


def test_cancel_releases_private_binding_without_stopping_existing_watch(execution):
    twitch, _, _, executor, _, _ = execution
    reserve(execution)
    old = executor._reservation.bound
    executor.cancel()
    assert executor._reservation is None and executor._guard._capture is None
    assert executor._guard.validate(old, twitch, now=NOW, enabled=True) == "superseded"
    twitch.watch.assert_not_called()
    twitch.stop_watching.assert_not_called()


def test_normal_watch_setup_metadata_is_not_a_new_stream(execution):
    twitch, _, _, _, _, move = execution
    reserve(execution)
    move(3)
    assert step(execution).action == "watch"
    twitch.channels[20]._watch_broadcast_id = twitch.channels[20]._stream.broadcast_id
    move(30)
    assert step(execution).action == "wait"
    assert twitch.watch.call_count == 1


@pytest.mark.parametrize("field,value", [
    ("dispatch_allowance", timedelta(0)), ("dispatch_allowance", timedelta(seconds=6)),
    ("dispatch_allowance", 1), ("dispatch_allowance", False),
    ("decision_delay", timedelta(0)), ("decision_delay", timedelta(seconds=13)),
    ("decision_delay", "later"),
])
def test_invalid_dispatch_budgets_do_not_inspect_runtime(execution, monkeypatch, field, value):
    executor = execution[3]
    fail = MagicMock(side_effect=AssertionError("Must reject before capture"))
    monkeypatch.setattr(executor._guard, "capture", fail)
    assert reserve(execution, **{field: value}).action == "refused"
    fail.assert_not_called()


def test_dispatch_allowance_is_charged_again_on_protected_connection(execution):
    reserve(execution, dispatch_allowance=timedelta(seconds=4))
    first, protected = execution[3]._reservation.certificate.slots
    assert first.watch_from - first.connect_at == timedelta(seconds=14)
    assert protected.watch_from - protected.connect_at == timedelta(seconds=14)
    assert protected.connect_at == first.until + timedelta(seconds=20)


def test_freshness_is_checked_after_final_preflight_not_just_at_entry(execution, monkeypatch):
    twitch, high, urgent, executor, _, move = execution
    reserve(execution)
    first, protected = executor._reservation.certificate.slots
    move(3)
    assert step(execution).action == "watch"
    urgent.timed_drops["urgent"].is_claimed = True
    move((first.until - NOW).total_seconds(), refresh=True)
    assert step(execution).action == "stop"
    seconds = (protected.connect_at - NOW).total_seconds()
    move(seconds, refresh=True)
    high.timed_drops["reward"]._progress_observation["received_at"] = (
        NOW + timedelta(seconds=seconds - 299.5)
    ).isoformat()
    original = executor._guard.validate

    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        move(seconds + 0.6)
        return result

    monkeypatch.setattr(executor._guard, "validate", slow)
    result = step(execution)
    assert result.reason == "unknown_progress" and result.action == "refused"
    assert twitch.watch.call_count == 1


@pytest.mark.parametrize("change", ["claim_queue", "closed_hours", "logout", "same_id_stream"])
def test_active_calendar_refuses_changed_lifecycle_and_never_revives_it(execution, change):
    twitch, _, _, executor, _, move = execution
    reserve(execution)
    move(3)
    assert step(execution).action == "watch"
    if change == "claim_queue":
        twitch.request_games_update()
    elif change == "closed_hours":
        twitch.is_mining_time = lambda: False
    elif change == "logout":
        twitch._auth_state._logged_in.clear()
    else:
        twitch.channels[20]._stream = copy.copy(twitch.channels[20]._stream)
    move(30)
    assert step(execution).action == "refused"
    assert executor._reservation is None
    twitch._games_update_pending = False
    assert step(execution).reason == "superseded"
    assert twitch.watch.call_count == 1


@pytest.mark.parametrize("faster", ["wall", "monotonic"])
def test_small_clock_differences_cannot_accumulate_across_checkpoints(execution, faster):
    _, _, _, executor, _, move = execution
    reserve(execution)
    move(3)
    assert step(execution).action == "watch"
    wall, tick = executor._utc_clock, executor._clock
    for seconds, drift in ((4, 0.6), (5, 1.2)):
        move(seconds)
        if faster == "wall":
            executor._utc_clock = lambda drift=drift: wall() + timedelta(seconds=drift)
        else:
            executor._clock = lambda drift=drift: tick() + drift
        result = step(execution)
        assert result.action == ("wait" if drift < 1 else "refused")
    assert result.reason == "clock_changed"
    assert executor._reservation is None


def test_final_validation_charges_total_monotonic_age_not_just_latest_interval(execution, monkeypatch):
    reserve(execution)
    execution[5](4.2)
    executor = execution[3]
    clock = executor._clock
    offset = [0.7]
    executor._clock = lambda: clock() + offset[0]
    executor._guard._clock = executor._clock
    original = executor._guard.validate
    calls = [0]

    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        calls[0] += 1
        if calls[0] == 2:
            offset[0] = 0.9
        return result

    monkeypatch.setattr(executor._guard, "validate", slow)
    result = step(execution)
    assert result.reason == "missed_slot" and result.action == "refused"
    execution[0].watch.assert_not_called()


def test_stable_allowed_clock_skew_is_not_a_wall_clock_reversal(execution):
    reserve(execution)
    _, _, _, executor, _, move = execution
    move(4.2)
    original = executor._clock
    executor._clock = lambda: original() + 0.7
    executor._guard._clock = executor._clock
    assert step(execution).action == "watch"
    move(4.3)
    assert step(execution).action == "wait"
    assert executor._reservation is not None


@pytest.mark.parametrize("phase", ["active", "stopping"])
def test_stale_evidence_retires_active_and_stop_checkpoints(execution, phase):
    twitch, _, urgent, executor, _, move = execution
    reserve(execution)
    first = executor._reservation.certificate.slots[0]
    move(3)
    assert step(execution).action == "watch"
    if phase == "stopping":
        urgent.timed_drops["urgent"].is_claimed = True
    move(301 if phase == "active" else (first.until - NOW).total_seconds())
    result = step(execution)
    assert result.reason == "unknown_progress" and result.action == "refused"
    assert executor._reservation is None
    twitch.stop_watching.assert_not_called()


def test_runtime_game_order_is_part_of_the_original_policy(execution):
    twitch, _, _, executor, _, move = execution
    reserve(execution)
    move(3)
    assert step(execution).action == "watch"
    twitch.wanted_games.reverse()
    move(4)
    assert step(execution).reason == "superseded"
    assert executor._reservation is None and twitch.watch.call_count == 1


def test_stop_after_next_admission_window_retires_without_action(execution):
    twitch, _, urgent, executor, _, move = execution
    reserve(execution)
    _, protected = executor._reservation.certificate.slots
    move(3)
    assert step(execution).action == "watch"
    urgent.timed_drops["urgent"].is_claimed = True
    move((protected.connect_at - NOW).total_seconds() + 3, refresh=True)
    assert step(execution).reason == "missed_slot"
    assert executor._reservation is None
    twitch.stop_watching.assert_not_called()


def test_stop_processing_can_use_booked_idle_gap_but_not_move_protected_work(execution, monkeypatch):
    twitch, _, urgent, executor, _, move = execution
    reserve(execution)
    first, protected = executor._reservation.certificate.slots
    move(3)
    assert step(execution).action == "watch"
    urgent.timed_drops["urgent"].is_claimed = True
    end = (first.until - NOW).total_seconds()
    move(end - 0.25, refresh=True)
    original = executor._guard.validate

    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        move(end + 0.25)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(executor._guard, "validate", slow)
        result = step(execution)
    assert result.action == "stop" and result.wake_at == protected.connect_at
    assert executor._reservation.slot == 1
    assert twitch.watch.call_count == 1
    move((protected.connect_at - NOW).total_seconds(), refresh=True)
    assert step(execution).action == "watch"


@pytest.mark.parametrize("work", ["validation", "fingerprint"])
def test_reservation_itself_accounts_for_final_processing(execution, monkeypatch, work):
    _, _, _, executor, _, move = execution
    if work == "validation":
        original = executor._guard._context
        calls = [0]

        def slow(*args, **kwargs):
            result = original(*args, **kwargs)
            calls[0] += 1
            if calls[0] == 3:
                move(6)
            return result

        monkeypatch.setattr(executor._guard, "_context", slow)
    else:
        original = executor._identity

        def slow(*args, **kwargs):
            result = original(*args, **kwargs)
            move(6)
            return result

        monkeypatch.setattr(executor, "_identity", slow)
    assert reserve(execution).action == "refused"
    assert executor._reservation is None


def test_fast_claim_holds_every_unused_fragment_then_returns_to_protected_work(execution):
    twitch, high, urgent, executor, premises, move = execution
    view = premises.viewing_windows[1]
    views = (premises.viewing_windows[0], replace(view, ends_at=NOW + timedelta(hours=1)),
             replace(view, starts_at=NOW + timedelta(hours=2)))
    timing = replace(premises.timing, drops=tuple(
        replace(item, remaining=DurationBounds(timedelta(minutes=20), timedelta(minutes=120)))
        if item.drop_id == '["two","urgent"]' else item for item in premises.timing.drops
    ))
    result = executor.reserve(twitch, horizon=HORIZON, enabled=True,
                              assumptions=replace(premises, viewing_windows=views, timing=timing))
    assert result.action == "reserved"
    first, second, protected = executor._reservation.certificate.slots
    move(3)
    assert step(execution).action == "watch"
    urgent.timed_drops["urgent"].is_claimed = True
    move(1800, refresh=True)
    assert step(execution).action == "stop"
    move((first.until - NOW).total_seconds(), refresh=True)
    assert step(execution).action == "wait"
    move((second.connect_at - NOW).total_seconds(), refresh=True)
    assert step(execution).action == "wait"
    assert twitch.watch.call_count == 1
    move((second.until - NOW).total_seconds(), refresh=True)
    assert step(execution).action == "wait"
    move((protected.connect_at - NOW).total_seconds(), refresh=True)
    assert step(execution).action == "watch"
    high.timed_drops["reward"].is_claimed = True
    move((protected.until - NOW).total_seconds(), refresh=True)
    assert step(execution).action == "stop"
    assert step(execution).action == "complete"
    assert [call.args[0].id for call in twitch.watch.call_args_list] == [20, 10]


@pytest.mark.parametrize("edge", ["initial", "protected", "work_end"])
@pytest.mark.parametrize("faster", ["wall", "monotonic"])
def test_not_before_and_work_end_require_both_clocks(execution, edge, faster):
    twitch, _, urgent, executor, _, move = execution
    reserve(execution)
    first, protected = executor._reservation.certificate.slots
    boundary = first.connect_at
    if edge != "initial":
        move(5)
        assert step(execution).action == "watch"
        if edge == "protected":
            urgent.timed_drops["urgent"].is_claimed = True
            move((first.until - NOW).total_seconds(), refresh=True)
            assert step(execution).action == "stop"
            boundary = protected.connect_at
        else:
            boundary = first.until
    move((boundary - NOW).total_seconds() - 0.5, refresh=edge != "initial")
    if edge == "work_end":
        urgent.timed_drops["urgent"].update_minutes(119, source="websocket")
    if faster == "wall":
        clock = executor._utc_clock
        executor._utc_clock = lambda: clock() + timedelta(seconds=0.7)
    else:
        clock = executor._clock
        executor._clock = lambda: clock() + 0.7
        executor._guard._clock = executor._clock
    watches, stops = twitch.watch.call_count, twitch.stop_watching.call_count
    result = step(execution)
    assert result.action == "wait"
    assert result.wake_at >= boundary and result.wake_at >= executor._utc_clock()
    assert twitch.watch.call_count == watches and twitch.stop_watching.call_count == stops


def test_reserve_rejects_prevalidation_monotonic_reversal(execution, monkeypatch):
    _, _, _, executor, _, move = execution
    identity, validate, clock = executor._identity, executor._guard.validate, executor._clock

    def slow_identity(*args, **kwargs):
        result = identity(*args, **kwargs)
        move(4)
        return result

    def reversed_clock(*args, **kwargs):
        executor._clock = lambda: clock() - 0.5
        executor._guard._clock = executor._clock
        return validate(*args, **kwargs)

    monkeypatch.setattr(executor, "_identity", slow_identity)
    monkeypatch.setattr(executor._guard, "validate", reversed_clock)
    assert reserve(execution).reason == "clock_changed"
    assert executor._reservation is None


@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize("shift", [-0.5, 0.5])
def test_capture_cannot_forget_entry_clock_even_after_recovery(execution, monkeypatch, shift, restore):
    original = execution[3]
    offset = [0.0]
    executor = DeadlineCalendarExecutor(clock=lambda: original._clock() + offset[0],
                                        utc_clock=original._utc_clock)
    state = (*execution[:3], executor, *execution[4:])
    capture = executor._guard.capture

    def shifted_capture(*args, **kwargs):
        offset[0] = shift
        result = capture(*args, **kwargs)
        if restore:
            offset[0] = 0.0
        return result

    monkeypatch.setattr(executor._guard, "capture", shifted_capture)
    result = reserve(state)
    reversed_clock = shift < 0 or restore
    assert result.action == ("refused" if reversed_clock else "reserved")
    if reversed_clock:
        assert result.reason == "clock_changed"
        assert executor._reservation is None and executor._guard._capture is None
