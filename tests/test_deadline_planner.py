"""Bind conditional calendars to real inputs without enabling live scheduling."""

import asyncio
import copy
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.models.game import Game
from src.services.deadline_certificate import DropTimingBounds, DurationBounds, TimingBounds
from src.services.deadline_forecast import ViewingWindow
from src.services.deadline_planner import ConditionalDeadlinePlanner, PlanningAssumptions
from tests.test_deadline_channels import channel, index
from tests.test_deadline_channels import runtime as channel_runtime
from tests.test_deadline_forecast import NOW
from tests.test_deadline_inputs import campaign
from tests.test_drop_ignore_policy import _drop


HORIZON = NOW + timedelta(days=3)


@pytest.fixture
def planning(monkeypatch):
    runtime = channel_runtime.__wrapped__(monkeypatch)
    twitch, high, current, _, ticks = runtime
    raw = _drop("urgent", "Urgent", required_minutes=120)
    raw["self"] = {"dropInstanceID": "test-urgent-claim", "isClaimed": False, "currentMinutesWatched": 0}
    urgent = campaign(twitch, "two", "Urgent", deadline=4, drops=[raw])
    twitch.wanted_games.append(urgent.game)
    twitch.channels[20] = channel(twitch, 20, urgent.game)
    index(twitch)
    walls = [NOW]
    planner = ConditionalDeadlinePlanner(clock=lambda: ticks[0], utc_clock=lambda: walls[0])
    return runtime, urgent, planner, walls


def assumptions(planning):
    runtime, _, _, _ = planning
    twitch, _, _, guard, _ = runtime
    captured = guard.capture(twitch, now=NOW, horizon=HORIZON, enabled=True, include_channels=True)
    assert captured.reason == "captured", captured.reason
    bound = captured.bound
    views = []
    routes = list(bound.channels.candidates)
    watching = bound.channels.watching
    if watching is not None and not any(route.channel_id == watching.channel_id for route in routes):
        routes.append(replace(watching, channel_order=len(routes)))
    for route in routes:
        keys = frozenset(drop.id for drop in bound.inputs.drops if drop.game_id == route.game_id)
        if keys:
            views.append(ViewingWindow(route.channel_id, NOW, HORIZON, keys, True,
                                       route.channel_order, route.acl_based, route.mining_priority))
    timing = TimingBounds(tuple(DropTimingBounds(
        drop.id, DurationBounds(timedelta(minutes=drop.required_minutes - (drop.observed_minutes or 0)),
                                timedelta(minutes=drop.required_minutes - (drop.observed_minutes or 0))),
        DurationBounds(timedelta(0), timedelta(seconds=20)),
    ) for drop in bound.inputs.drops), DurationBounds(timedelta(0), timedelta(seconds=10)))
    # Explicit hypothetical uptime/allocation/claim bounds, not derived live guarantees.
    return PlanningAssumptions(tuple(views), timing, baseline_complete=True)


def plan(planning, premises, **kwargs):
    runtime, _, planner, _ = planning
    return planner.plan(runtime[0], horizon=HORIZON, assumptions=premises, enabled=True, **kwargs)


def test_original_ranked_deadline_example_returns_conditional_calendar_without_steering(planning, monkeypatch):
    runtime, urgent, planner, _ = planning
    twitch, high, current, _, _ = runtime
    watcher = MagicMock()
    monkeypatch.setattr(twitch._watch_service, "watch", watcher)
    before = [(drop.real_current_minutes, drop.extra_current_minutes, drop.is_claimed)
              for item in twitch.inventory for drop in item.drops]
    premises = assumptions(planning)

    result = plan(planning, premises)

    assert result.reason == "rescue"
    certificate = result.certificate
    assert certificate.target_id == '["two","urgent"]'
    assert certificate.protected_drop_ids == frozenset({'["one","reward"]'})
    assert [slot.channel_id for slot in certificate.slots] == ["20", "10"]
    assert certificate.slots[0].watch_from == NOW + timedelta(seconds=10)
    assert certificate.completions[0].earned_at == NOW + timedelta(hours=2, seconds=10)
    assert result.valid_until == NOW + timedelta(seconds=15)
    assert twitch.watching_channel.get_with_default(None) is current
    assert twitch.settings.games_to_watch == ["High", "Urgent"]
    assert before == [(drop.real_current_minutes, drop.extra_current_minutes, drop.is_claimed)
                      for item in twitch.inventory for drop in item.drops]
    watcher.assert_not_called()
    twitch.gui.channels.select.assert_not_called()
    twitch.gui.channels.get_selection.assert_not_called()
    assert "user_id" not in repr(result) and "42" not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.valid_until = HORIZON


@pytest.mark.parametrize("enabled", [False, None, 0, 1, "yes"])
def test_disabled_planning_never_reads_runtime_or_clocks(planning, monkeypatch, enabled):
    planner = planning[2]
    fail = MagicMock(side_effect=AssertionError("Disabled planner must not inspect state"))
    monkeypatch.setattr(planner._guard, "capture", fail)
    monkeypatch.setattr(planner, "_utc_clock", fail)
    assert planner.plan(object(), horizon=HORIZON, enabled=enabled).reason == "disabled"
    fail.assert_not_called()


@pytest.mark.parametrize("coverage", [False, None, 1, "yes"])
def test_current_routes_never_invent_complete_future_coverage(planning, monkeypatch, coverage):
    premises = replace(assumptions(planning), baseline_complete=coverage)
    proof = MagicMock(side_effect=AssertionError("Unknown coverage"))
    monkeypatch.setattr(planning[2], "certify", proof)
    assert plan(planning, premises).reason == "unknown_coverage"
    assert plan(planning, None).reason == "unknown_coverage"
    proof.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("mining_priority", None), ("mining_priority", (0, 0)), ("acl_based", True),
    ("channel_order", 0), ("channel_id", "unknown"),
    ("drop_ids", frozenset({'["one","reward"]'})),
])
def test_immediate_window_cannot_override_actual_channel_inputs(planning, field, value):
    premises = assumptions(planning)
    urgent_view = replace(premises.viewing_windows[1], **{field: value})
    result = plan(planning, replace(premises, viewing_windows=(premises.viewing_windows[0], urgent_view)))
    assert result.reason == "checkpoint_mismatch" and result.certificate is None
    assert result.valid_until is None


@pytest.mark.parametrize("omit", [0, 1])
def test_omitted_current_watchable_route_is_not_complete_coverage(planning, omit):
    premises = assumptions(planning)
    result = plan(planning, replace(premises, viewing_windows=tuple(
        view for position, view in enumerate(premises.viewing_windows) if position != omit
    )))
    assert result.reason == "unknown_coverage" and result.certificate is None


@pytest.mark.parametrize("unknown", ["missing", "stale", "estimated"])
def test_display_counters_do_not_replace_fresh_direct_progress(planning, unknown):
    premises = assumptions(planning)
    drop = planning[1].timed_drops["urgent"]
    if unknown == "stale":
        drop._progress_observation["received_at"] = (NOW - timedelta(minutes=6)).isoformat()
    else:
        drop._progress_observation = None
        if unknown == "estimated":
            drop.real_current_minutes, drop.extra_current_minutes = 100, 5
    assert plan(planning, premises).reason == "unknown_progress"


@pytest.mark.parametrize("unknown", ["remaining", "claim_delay", "setup"])
def test_unknown_continuous_timing_is_not_assumed_zero(planning, unknown):
    premises = assumptions(planning)
    timing = premises.timing
    if unknown == "setup":
        timing = replace(timing, setup=None)
    else:
        timing = replace(timing, drops=(replace(timing.drops[0], **{unknown: None}), *timing.drops[1:]))
    assert plan(planning, replace(premises, timing=timing)).reason == "unknown_timing"


@pytest.mark.parametrize("bad", ["duplicate", "missing", "foreign"])
def test_timing_records_must_match_captured_drop_ids_exactly(planning, bad):
    premises = assumptions(planning)
    drops = premises.timing.drops
    if bad == "duplicate":
        drops = (*drops, drops[0])
    elif bad == "missing":
        drops = drops[:1]
    else:
        drops = (replace(drops[0], drop_id="not-this-campaign"), *drops[1:])
    assert plan(planning, replace(premises, timing=replace(premises.timing, drops=drops))).reason == "invalid_snapshot"


@pytest.mark.parametrize("manual", ["channel", "game", "selection"])
def test_manual_intent_refuses_planning_without_consuming_selection(planning, manual):
    premises = assumptions(planning)
    twitch, high, current, _, _ = planning[0]
    if manual == "channel":
        twitch._manual_target_channel = current
    elif manual == "game":
        twitch._manual_target_game = high.game
    else:
        twitch.gui._selected_channel_id = current.id
    result = plan(planning, premises)
    assert result.reason == "manual_override" and result.certificate is None
    twitch.gui.channels.get_selection.assert_not_called()


@pytest.mark.parametrize("change,expected", [
    ("settings", "superseded"), ("model", "superseded"), ("stream", "superseded"),
    ("progress", "superseded"), ("epoch", "superseded"), ("auth", "superseded"),
    ("manual", "manual_override"), ("refresh", "inactive"),
])
def test_model_or_policy_change_during_computation_discards_certificate(planning, monkeypatch, change, expected):
    runtime, _, planner, _ = planning
    twitch, high, current, _, _ = runtime
    premises = assumptions(planning)
    certify = planner.certify

    def changed(*args, **kwargs):
        result = certify(*args, **kwargs)
        assert result.reason == "rescue"
        if change == "settings":
            twitch.settings.games_to_watch.reverse()
        elif change == "model":
            twitch.inventory = list(twitch.inventory)
        elif change == "stream":
            current._stream = copy.copy(current._stream)
        elif change == "progress":
            high.progress_revision += 1
        elif change == "epoch":
            twitch._deadline_context_epoch = object()
        elif change == "auth":
            twitch._auth_state.context_epoch = object()
        elif change == "manual":
            twitch._manual_target_game = high.game
        else:
            twitch._inventory_refresh_pending = True
        return result

    monkeypatch.setattr(planner, "certify", changed)
    result = plan(planning, premises)
    assert result.reason == expected and result.certificate is None and result.valid_until is None


@pytest.mark.parametrize("wall,tick,expected", [
    (14, 14, "rescue"), (15, 0, "expired"), (0, 15, "expired"),
    (-1, 0, "expired"), (0, -1, "expired"), (0, float("nan"), "invalid_context"),
])
def test_computation_time_is_charged_to_original_context(planning, monkeypatch, wall, tick, expected):
    runtime, _, planner, walls = planning
    premises = assumptions(planning)
    certify = planner.certify

    def slow(*args, **kwargs):
        result = certify(*args, **kwargs)
        walls[0] += timedelta(seconds=wall)
        runtime[4][0] += tick
        return result

    monkeypatch.setattr(planner, "certify", slow)
    result = plan(planning, premises)
    assert result.reason == expected
    assert (result.certificate is not None) is (expected == "rescue")


def test_even_an_unsuccessful_forecast_is_revalidated(planning, monkeypatch):
    runtime, _, planner, _ = planning
    high = runtime[1]
    high.ends_at = NOW + timedelta(hours=9)
    premises = assumptions(planning)
    certify = planner.certify

    def refused(*args, **kwargs):
        result = certify(*args, **kwargs)
        assert result.reason == "no_safe_rescue"
        runtime[0]._deadline_context_epoch = object()
        return result

    monkeypatch.setattr(planner, "certify", refused)
    assert plan(planning, premises).reason == "superseded"


def test_watched_channel_does_not_establish_readiness_or_waive_setup(planning):
    runtime, _, _, _ = planning
    twitch = runtime[0]
    twitch.watching_channel.set(twitch.channels[20])
    result = plan(planning, assumptions(planning))
    assert result.reason == "rescue"
    assert result.certificate.slots[0].channel_id == "20"
    assert result.certificate.slots[0].watch_from == NOW + timedelta(seconds=10)


def test_untracked_current_is_checked_and_same_id_replacement_is_not_conflated(planning):
    runtime, _, _, _ = planning
    twitch, high, current, _, _ = runtime
    del twitch.channels[current.id]
    result = plan(planning, assumptions(planning))
    assert result.reason == "rescue"
    twitch.channels[current.id] = channel(twitch, current.id, Game({"id": "2", "name": "Urgent"}))
    result = plan(planning, assumptions(planning))
    assert result.reason == "checkpoint_mismatch" and result.certificate is None


def test_fallback_only_work_cannot_disappear_from_baseline(planning):
    runtime, _, _, _ = planning
    twitch = runtime[0]
    fallback = campaign(twitch, "fallback", "Other", drops=[_drop("fallback", "Fallback")])
    fallback.game = Game({"id": "3", "name": "Other"})
    twitch.wanted_games.append(fallback.game)
    twitch.settings.priority_list_only = False
    twitch.channels[30] = channel(twitch, 30, fallback.game)
    index(twitch)
    result = plan(planning, assumptions(planning))
    assert result.reason == "unknown_coverage" and result.certificate is None


def test_cancellation_propagates_and_a_later_call_recaptures(planning, monkeypatch):
    planner = planning[2]
    premises = assumptions(planning)
    original = planner.certify
    monkeypatch.setattr(planner, "certify", MagicMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        plan(planning, premises)
    monkeypatch.setattr(planner, "certify", original)
    assert plan(planning, premises).reason == "rescue"


def test_conflicting_short_deadline_does_not_sacrifice_rank_one(planning):
    planning[0][1].ends_at = NOW + timedelta(hours=9)
    result = plan(planning, assumptions(planning))
    assert result.reason == "no_safe_rescue" and result.certificate is None


def add_locked_chain(planning):
    runtime, old_urgent, _, _ = planning
    twitch = runtime[0]
    twitch.inventory.remove(old_urgent)
    raw_parent = _drop("parent", "Parent", required_minutes=60, has_benefit=False)
    raw_target = _drop("target", "Target", required_minutes=60, preconditions=("parent",))
    for raw in (raw_parent, raw_target):
        raw["self"] = {"dropInstanceID": "test-claim", "isClaimed": False, "currentMinutesWatched": 0}
    urgent = campaign(twitch, "two", "Urgent", deadline=4, drops=[raw_parent, raw_target])
    index(twitch)
    return urgent


def test_locked_target_reserves_parent_claim_delay_then_returns_to_rank_one(planning):
    urgent = add_locked_chain(planning)
    assert not urgent.timed_drops["target"].preconditions_met

    result = plan(planning, assumptions(planning))

    assert result.reason == "rescue"
    certificate = result.certificate
    assert certificate.target_id == '["two","target"]'
    assert [slot.drop_id for slot in certificate.slots] == [
        '["two","parent"]', '["two","target"]', '["one","reward"]',
    ]
    assert certificate.slots[1].connect_at == certificate.completions[0].claim_ready_at
    assert certificate.completions[0].claim_ready_at - certificate.completions[0].earned_at == timedelta(seconds=20)
    assert all(not drop.is_claimed for drop in urgent.drops)


@pytest.mark.parametrize("parent_seconds", [0, 1, 60])
def test_hypothetical_settled_parent_cannot_make_locked_child_the_first_real_slot(planning, parent_seconds):
    urgent = add_locked_chain(planning)
    premises = assumptions(planning)
    duration = timedelta(seconds=parent_seconds)
    timing = replace(premises.timing, drops=tuple(
        replace(item, remaining=DurationBounds(duration, duration),
                claim_delay=DurationBounds(timedelta(0), timedelta(0)))
        if item.drop_id == '["two","parent"]' else item
        for item in premises.timing.drops
    ))

    result = plan(planning, replace(premises, timing=timing))

    if parent_seconds == 0:
        assert result.reason == "checkpoint_mismatch" and result.certificate is None
    else:
        assert result.reason == "rescue"
        assert result.certificate.slots[0].drop_id == '["two","parent"]'
    assert not urgent.timed_drops["target"].preconditions_met
    assert all(not drop.is_claimed for drop in urgent.drops)


@pytest.mark.parametrize("allocation", [False, None, 1, "yes"])
def test_eligibility_alone_does_not_establish_credit_allocation(planning, allocation):
    premises = assumptions(planning)
    views = (replace(premises.viewing_windows[0], allocation_known=allocation), premises.viewing_windows[1])
    result = plan(planning, replace(premises, viewing_windows=views))
    assert result.reason == "unknown_allocation" and result.certificate is None


def test_unknown_timezone_is_not_utc_and_nominal_hours_can_prevent_rescue(planning):
    twitch = planning[0][0]
    twitch.settings.mining_hours = {"mode": "range", "start": "12:00", "end": "13:00"}
    assert plan(planning, None).reason == "unknown_timezone"
    # Construct the same explicit hypotheses using always-open projection, then
    # enforce the actual one-hour-per-day rule when the planner captures again.
    twitch.settings.mining_hours["mode"] = "always"
    premises = assumptions(planning)
    twitch.settings.mining_hours["mode"] = "range"
    assert plan(planning, premises, local_timezone=timezone.utc).reason == "no_safe_rescue"


def test_drop_boundary_clips_context_and_expired_refusal_hides_no_certificate(planning, monkeypatch):
    runtime, urgent, planner, walls = planning
    urgent.ends_at = NOW + timedelta(seconds=5)
    premises = assumptions(planning)
    original = planner.certify

    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        assert result.certificate is None
        walls[0] += timedelta(seconds=5)
        runtime[4][0] += 5
        return result

    monkeypatch.setattr(planner, "certify", slow)
    result = plan(planning, premises)
    assert result.reason == "expired" and result.certificate is None


def test_future_untracked_windows_remain_explicit_hypotheses_not_current_eligibility(planning):
    premises = assumptions(planning)
    future = replace(premises.viewing_windows[0], channel_id="future-hypothesis",
                     starts_at=NOW + timedelta(hours=1), channel_order=9)
    assert plan(planning, replace(premises, viewing_windows=(*premises.viewing_windows, future))).reason == "rescue"
    immediate = replace(future, starts_at=NOW)
    result = plan(planning, replace(premises, viewing_windows=(*premises.viewing_windows, immediate)))
    assert result.reason == "checkpoint_mismatch" and result.certificate is None


def test_oversized_window_domain_is_not_silently_truncated(planning):
    premises = assumptions(planning)
    result = plan(planning, replace(premises, viewing_windows=(premises.viewing_windows[0],) * 513))
    assert result.reason == "budget_exceeded" and result.certificate is None


def test_capture_processing_and_final_refusal_exceptions_are_revalidated(planning, monkeypatch):
    runtime, _, planner, _ = planning
    premises = assumptions(planning)

    def failed(*args, **kwargs):
        runtime[0]._deadline_context_epoch = object()
        raise ValueError("Synthetic invalid conditional input")

    monkeypatch.setattr(planner, "certify", failed)
    assert plan(planning, premises).reason == "superseded"


def test_slow_current_channel_capture_consumes_budget_before_certificate_work(planning, monkeypatch):
    runtime, _, planner, _ = planning
    premises = assumptions(planning)
    capture = planner._guard._channels.capture
    proof = MagicMock(side_effect=AssertionError("Expired capture must not reach proof"))

    def slow(*args, **kwargs):
        result = capture(*args, **kwargs)
        runtime[4][0] += 15
        return result

    monkeypatch.setattr(planner._guard._channels, "capture", slow)
    monkeypatch.setattr(planner, "certify", proof)
    assert plan(planning, premises).reason == "expired"
    proof.assert_not_called()


@pytest.mark.parametrize("blocker", ["offline", "drops_disabled", "ban", "acl", "estimate_cap"])
def test_current_runtime_eligibility_cannot_be_granted_by_old_assumptions(planning, blocker):
    runtime, urgent, _, _ = planning
    premises = assumptions(planning)
    twitch = runtime[0]
    current = twitch.channels[20]
    if blocker == "offline":
        current._stream = None
    elif blocker == "drops_disabled":
        current._stream.drops_enabled = False
    elif blocker == "ban":
        current._banned = True
    elif blocker == "acl":
        urgent.allowed_channels = [channel(twitch, 99, urgent.game)]
    else:
        urgent.timed_drops["urgent"].extra_current_minutes = 100
    result = plan(planning, premises)
    assert result.reason == "checkpoint_mismatch" and result.certificate is None


def test_special_cross_category_route_does_not_bypass_rank_canonical_certificate_limit(planning):
    runtime, urgent, _, _ = planning
    twitch, high, _, _, _ = runtime
    urgent.game = Game({"id": "509663", "name": "Urgent"})
    twitch.wanted_games = [high.game, urgent.game]
    special_channel = twitch.channels[20]
    special_channel._stream.game = high.game
    urgent.allowed_channels = [special_channel]
    premises = assumptions(planning)
    views = (premises.viewing_windows[0], replace(
        premises.viewing_windows[1], drop_ids=frozenset({'["one","reward"]', '["two","urgent"]'}),
    ))
    result = plan(planning, replace(premises, viewing_windows=views))
    assert result.reason == "unknown_coverage" and result.certificate is None
