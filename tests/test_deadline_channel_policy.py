"""Conditional baseline routing must retain eligible current streams on ties."""

from dataclasses import replace
from datetime import timedelta
from itertools import product
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.config import MAX_INT
from src.services.channel_service import ChannelService
from src.services.channel_switch_policy import ChannelSwitchPolicy
from src.services.deadline_forecast import (
    DeadlineForecaster,
    MiningWindow,
    ViewingWindow,
    _Budget,
    _Timing,
)
from src.services.watch_service import WatchService
from tests.test_deadline_certificate import certify
from tests.test_deadline_forecast import NOW, reward, snapshot
from tests.test_deadline_timing import timing
from tests.test_special_game_watch import OTHER_GAME, SPECIAL_EVENTS, TEST_GAME, _campaign, _channel


def test_healthy_same_priority_current_stream_does_not_gain_another_channels_rewards():
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    state = replace(state, current_channel_id="z", viewing_windows=(
        ViewingWindow("a", NOW, state.horizon, frozenset({"high", "urgent"}), True, 0),
        ViewingWindow("z", NOW, state.horizon, frozenset({"high"}), True, 1),
    ))
    result = DeadlineForecaster().analyze(state, enabled=True)
    assert result.baseline_earned_ids == frozenset({"high"})
    assert result.plan is not None and result.plan.drop_id == "urgent"
    assert result.plan.channel_id == "a" and result.plan.game_id == 2
    assert result.plan.protected_drop_ids == frozenset({"high"})


def test_unnecessary_same_rank_switch_does_not_invent_a_setup_cost_or_rescue():
    state = snapshot(reward("high", 1, 60, 48), reward("urgent", 2, 60, 2 + 9 / 60))
    high_window = state.viewing_windows[0]
    state = replace(state, current_channel_id="z", viewing_windows=(
        replace(high_window, channel_id="a", channel_order=0),
        replace(high_window, channel_id="z", channel_order=1),
        state.viewing_windows[1],
    ))
    result = DeadlineForecaster().analyze_robust(
        state, timing(state, switches=(timedelta(minutes=5),)), enabled=True,
    )
    assert result.baseline_earned_ids == frozenset({"high", "urgent"})
    assert result.plan is None


def test_shared_comparison_and_runtime_match_original_rule_for_432_cases():
    priorities = ((0, 0), (0, 1), (0, MAX_INT), (1, 0), (1, 1), (1, MAX_INT))
    client = MagicMock()
    client._channel_service.get_mining_priority.side_effect = lambda channel: channel.priority
    service = WatchService(client)
    cases = 0
    for proposed, current, acl, current_acl, eligibility in product(
        priorities, priorities, (False, True), (False, True), ("absent", "unwatchable", "healthy"),
    ):
        watched = SimpleNamespace(priority=current, acl_based=current_acl)
        client.watching_channel.get_with_default.return_value = None if eligibility == "absent" else watched
        service.can_watch = lambda channel, eligible=eligibility: eligible == "healthy"
        candidate = SimpleNamespace(priority=proposed, acl_based=acl)
        expected = (eligibility != "healthy" or proposed < current
                    or proposed == current and acl and not current_acl)
        assert service.should_switch(candidate) is expected
        assert ChannelSwitchPolicy.should_switch(
            proposed, acl, current_priority=current if eligibility == "healthy" else None,
            current_acl_based=current_acl,
        ) is expected
        cases += 1
    assert cases == 432


def simulate(state, *, setup=timedelta(0)):
    engine = DeadlineForecaster()
    prepared = engine._prepare(state)
    return engine._simulate(
        prepared, {drop.id: drop for drop in prepared.drops},
        {game: index for index, game in enumerate(prepared.ranked_game_ids)},
        _Budget(10_000), timing=_Timing({}, {}, setup),
    )


@pytest.mark.parametrize("current_acl,candidate_acl,expected", [
    (False, False, "z"), (False, True, "a"), (True, False, "z"), (True, True, "z"),
])
def test_equal_priority_only_promotes_non_acl_current_to_acl(current_acl, candidate_acl, expected):
    state = snapshot(reward("a", 1, 60, 4), reward("z", 1, 60, 4), current_channel_id="z")
    state = replace(state, viewing_windows=(
        ViewingWindow("a", NOW, state.horizon, frozenset({"a"}), True, 0, candidate_acl),
        ViewingWindow("z", NOW, state.horizon, frozenset({"z"}), True, 1, current_acl),
    ))
    run = simulate(state)
    assert run.first.channel_id == expected
    assert run.earned[expected] == NOW + timedelta(hours=1)
    other = "z" if expected == "a" else "a"
    assert run.earned[other] == NOW + timedelta(hours=2, seconds=20)


@pytest.mark.parametrize("candidate_priority,current_priority,expected", [
    ((0, 0), (0, 1), "a"), ((0, MAX_INT), (1, 0), "a"),
    ((1, 0), (0, MAX_INT), "z"), ((0, MAX_INT), (0, MAX_INT), "z"),
])
def test_priority_tuples_precede_acl_and_credit_rank(candidate_priority, current_priority, expected):
    state = snapshot(reward("a", 2, 60, 4), reward("z", 1, 60, 4), current_channel_id="z")
    state = replace(state, viewing_windows=(
        ViewingWindow("a", NOW, state.horizon, frozenset({"a"}), True, 0, False, candidate_priority),
        ViewingWindow("z", NOW, state.horizon, frozenset({"z"}), True, 1, True, current_priority),
    ))
    assert simulate(state).first.channel_id == expected


@pytest.mark.parametrize("current", [None, "missing", "ended", "completed"])
def test_no_eligible_current_initially_uses_supplied_order_not_acl_sort(current):
    state = snapshot(reward("a", 1, 60, 4), reward("z", 1, 60, 4),
                     reward("finished", 1, 60, 4, claimed=True), current_channel_id=current)
    state = replace(state, viewing_windows=(
        ViewingWindow("z", NOW, state.horizon, frozenset({"z"}), True, 0, False),
        ViewingWindow("a", NOW, state.horizon, frozenset({"a"}), True, 1, True),
        ViewingWindow("ended", NOW - timedelta(hours=1), NOW, frozenset({"a"}), True),
        ViewingWindow("completed", NOW, state.horizon, frozenset({"finished"}), True),
    ))
    run = simulate(state, setup=timedelta(minutes=5))
    assert run.first.channel_id == "z"
    # At the next conditional decision, the ACL candidate can promote that stream.
    assert run.earned["a"] == NOW + timedelta(minutes=70)
    assert run.earned["z"] == NOW + timedelta(minutes=135, seconds=20)


def test_adjacent_window_acl_change_preempts_current_without_free_setup():
    state = snapshot(reward("a", 1, 60, 4), reward("z", 1, 60, 4), current_channel_id="z")
    boundary = NOW + timedelta(minutes=30)
    state = replace(state, viewing_windows=(
        ViewingWindow("a", NOW, boundary, frozenset({"a"}), True, 0),
        ViewingWindow("a", boundary, state.horizon, frozenset({"a"}), True, 0, True),
        ViewingWindow("z", NOW, state.horizon, frozenset({"z"}), True, 1),
    ))
    run = simulate(state, setup=timedelta(minutes=5))
    assert run.first.channel_id == "z"
    assert run.earned["a"] == NOW + timedelta(minutes=95)
    assert run.earned["z"] == NOW + timedelta(minutes=130, seconds=20)


def test_mining_pause_releases_current_preference_and_requires_reconnect_setup():
    state = snapshot(reward("a", 1, 60, 4), reward("z", 1, 60, 4), current_channel_id="z")
    state = replace(state, viewing_windows=(
        ViewingWindow("a", NOW, state.horizon, frozenset({"a"}), True, 0),
        ViewingWindow("z", NOW, state.horizon, frozenset({"z"}), True, 1),
    ), mining_windows=(
        MiningWindow(NOW, NOW + timedelta(minutes=30)),
        MiningWindow(NOW + timedelta(hours=1), state.horizon),
    ))
    run = simulate(state, setup=timedelta(minutes=5))
    assert run.earned == {"a": NOW + timedelta(minutes=125), "z": NOW + timedelta(minutes=160, seconds=20)}


@pytest.mark.parametrize("strict", [True, False])
def test_explicit_special_category_priority_matches_real_runtime_comparison(strict):
    client = MagicMock()
    client.settings.drop_name_blacklist = []
    client.settings.priority_list_only = strict
    client.settings.games_to_watch = ["Special Events"]
    special = _campaign(client, SPECIAL_EVENTS)
    ordinary = _campaign(client, OTHER_GAME, acl=False)
    client.inventory = [special, ordinary]
    client.wanted_games = [special.game, ordinary.game]
    service = client._channel_service = ChannelService(client)
    current = _channel(client, TEST_GAME)
    candidate = _channel(client, OTHER_GAME, id=101)
    client.watching_channel.get_with_default.return_value = current
    watcher = WatchService(client)
    assert watcher.can_watch(current) and watcher.can_watch(candidate)
    current_priority, candidate_priority = service.get_mining_priority(current), service.get_mining_priority(candidate)
    assert current_priority == (0, MAX_INT)
    assert candidate_priority == (0 if strict else 1, 1)
    assert watcher.should_switch(candidate) is strict

    state = snapshot(reward("special", int(SPECIAL_EVENTS["id"]), 60, 4), reward("ordinary", 2, 60, 4),
                     current_channel_id="100")
    state = replace(state, ranked_game_ids=(int(SPECIAL_EVENTS["id"]), 2), viewing_windows=(
        ViewingWindow("100", NOW, state.horizon, frozenset({"special"}), True, 0, True, current_priority),
        ViewingWindow("101", NOW, state.horizon, frozenset({"ordinary"}), True, 1, True, candidate_priority),
    ))
    assert simulate(state).first.channel_id == ("101" if strict else "100")
    assert certify(state).reason == "unknown_coverage"


@pytest.mark.parametrize("field,value", [
    ("acl_based", None), ("acl_based", 0), ("acl_based", 1), ("acl_based", "false"),
    ("mining_priority", ()), ("mining_priority", (0,)), ("mining_priority", (0, 0, 0)),
    ("mining_priority", [0, 0]), ("mining_priority", "0,0"),
    ("mining_priority", (True, 0)), ("mining_priority", (0, False)),
    ("mining_priority", (2, 0)), ("mining_priority", (-1, 0)),
    ("mining_priority", (0, -1)), ("mining_priority", (0, MAX_INT + 1)),
])
def test_malformed_routing_metadata_declines_all_enabled_engines(field, value):
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    state = replace(state, viewing_windows=(replace(state.viewing_windows[0], **{field: value}),
                                            state.viewing_windows[1]))
    engine = DeadlineForecaster()
    assert engine.analyze(state, enabled=True).reason == "invalid_snapshot"
    assert engine.analyze_robust(state, timing(state), enabled=True).reason == "invalid_snapshot"
    assert certify(state).reason == "invalid_snapshot"
    assert engine.analyze(state).reason == "disabled"
    assert engine.analyze(state, enabled=True, manual_override=True).reason == "manual_override"


def test_canonical_explicit_priority_preserves_certificate_and_input_immutability():
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    expected = certify(state)
    state = replace(state, viewing_windows=tuple(
        replace(window, acl_based=True, mining_priority=(0, index))
        for index, window in enumerate(state.viewing_windows)
    ))
    before = repr(state)
    assert certify(state) == expected
    assert DeadlineForecaster().analyze(state, enabled=True).plan is not None
    assert repr(state) == before


@pytest.mark.parametrize("priority,mixed", [((1, 0), False), ((0, 1), False), ((0, 0), True)])
def test_certificate_declines_routing_not_supported_by_rank_blocker_proof(priority, mixed):
    state = snapshot(reward("high", 1, 480, 48), reward("urgent", 2, 120, 4))
    window = replace(state.viewing_windows[0], mining_priority=priority)
    if mixed:
        window = replace(window, drop_ids=frozenset({"high", "urgent"}))
    state = replace(state, viewing_windows=(window, state.viewing_windows[1]))
    result = certify(state)
    assert result.reason == "unknown_coverage" and result.certificate is None
