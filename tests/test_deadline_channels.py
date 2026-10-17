"""Real point-in-time channel inputs must not become future-availability claims."""

import asyncio
from collections import OrderedDict
from dataclasses import FrozenInstanceError, asdict
from datetime import datetime, timedelta
from itertools import product
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import MAX_INT, State
from src.models.channel import Channel, Stream
from src.models.game import Game
from src.services.channel_service import ChannelService
from src.services.deadline_channels import DeadlineChannelAdapter, ProjectedChannel
from src.services.watch_service import WatchService
from tests.test_deadline_context import state as context_state
from tests.test_deadline_forecast import NOW
from tests.test_deadline_inputs import campaign
from tests.test_drop_ignore_policy import _drop


@pytest.fixture
def runtime(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.replace(tzinfo=None) if tz is None else NOW.astimezone(tz)

    for module in ("src.models.campaign", "src.models.drop", "src.core.client"):
        monkeypatch.setattr(f"{module}.datetime", Clock)
    monkeypatch.setattr("src.models.channel.time", lambda: NOW.timestamp())
    state = context_state.__wrapped__()
    twitch, _, channel, _, _ = state
    twitch._channel_service = ChannelService(twitch)
    twitch._watch_service = WatchService(twitch)
    channel._stream.drops_enabled = True
    return state


def capture(runtime, **kwargs):
    twitch, _, _, guard, _ = runtime
    return guard.capture(twitch, now=NOW, horizon=NOW + timedelta(days=3), enabled=True,
                         **{"include_channels": True, **kwargs})


def channel(twitch, key, game, *, acl=False, online=True):
    result = Channel(twitch, id=key, login=f"channel{key}", acl_based=acl)
    if online:
        result._stream = Stream(result, id=key, game={"id": str(game.id), "name": game.name},
                                viewers=20, title="Live")
        result._stream.drops_enabled = True
    return result


def index(twitch):
    twitch._campaigns = {item.id: item for item in twitch.inventory}
    twitch._drops = {drop.id: drop for item in twitch.inventory for drop in item.drops}


def test_capture_is_opt_in_read_only_and_separates_eligibility_from_credit(runtime, monkeypatch):
    twitch, item, current, guard, _ = runtime
    watch = MagicMock()
    monkeypatch.setattr(twitch._watch_service, "watch", watch)
    assert capture(runtime, include_channels=False).bound.channels is None
    result = capture(runtime)
    assert result.reason == "captured"
    routes = result.bound.channels
    assert routes.baseline_complete is False and routes.allocation_known is False
    assert routes.captured_at == NOW
    assert routes.automatic_action == "keep" and routes.automatic_channel_id == str(current.id)
    assert routes.candidates[0].channel_order == 0
    assert routes.watching.channel_order is None
    assert routes.candidates[0].eligible_drop_ids == frozenset({result.bound.inputs.drops[0].id})
    assert routes.watching == ProjectedChannel(
        str(current.id), None, 1, False, (0, 0), True, routes.candidates[0].eligible_drop_ids,
    )
    assert guard.validate(result.bound, twitch, now=NOW, enabled=True) == "current"
    assert twitch.watching_channel.get_with_default(None) is current
    assert not item.timed_drops["reward"].is_claimed
    assert twitch.settings.games_to_watch == ["High", "Urgent"]
    watch.assert_not_called()
    twitch.gui.channels.get_selection.assert_not_called()
    twitch.gui.channels.select.assert_not_called()
    assert "user_id" not in asdict(routes) and "token" not in repr(routes)
    with pytest.raises(FrozenInstanceError):
        routes.automatic_action = "switch"


@pytest.mark.parametrize("enabled", [False, None, 1, "yes"])
def test_disabled_capture_never_calls_channel_projection(runtime, monkeypatch, enabled):
    twitch, _, _, guard, _ = runtime
    projector = MagicMock(side_effect=AssertionError("Must remain default off"))
    monkeypatch.setattr(guard._channels, "capture", projector)
    result = guard.capture(twitch, now=NOW, horizon=NOW + timedelta(days=3),
                           enabled=enabled, include_channels=True)
    assert result.reason == "disabled" and result.bound is None
    projector.assert_not_called()


@pytest.mark.parametrize("include", [None, 0, 1, "yes"])
def test_unknown_channel_opt_in_is_rejected_and_retires_previous_context(runtime, include):
    twitch, _, _, guard, _ = runtime
    old = capture(runtime).bound
    result = capture(runtime, include_channels=include)
    assert result.reason == "invalid_context" and result.bound is None
    assert guard.validate(old, twitch, now=NOW, enabled=True) == "superseded"


@pytest.mark.parametrize("blocker", ["offline", "drops_disabled", "ban", "unwanted", "acl", "ignored", "unlinked", "expired"])
def test_real_watchability_guards_are_not_replaced_by_category_matching(runtime, blocker):
    twitch, item, current, _, _ = runtime
    if blocker == "offline":
        current._stream = None
    elif blocker == "drops_disabled":
        current._stream.drops_enabled = False
    elif blocker == "ban":
        current._banned = True
    elif blocker == "unwanted":
        twitch.wanted_games = []
    elif blocker == "acl":
        item.allowed_channels = [channel(twitch, 99, item.game)]
    elif blocker == "ignored":
        twitch.settings.drop_name_blacklist = ["Reward"]
    elif blocker == "unlinked":
        item.linked = False
    elif blocker == "expired":
        item.ends_at = NOW
    result = capture(runtime)
    assert result.reason == "captured"
    routes = result.bound.channels
    assert routes.candidates[0].watchable is False
    assert routes.candidates[0].eligible_drop_ids == frozenset()
    assert routes.automatic_action == "idle" and routes.automatic_channel_id is None


def test_locked_ancestors_and_sibling_eligibility_do_not_imply_shared_credit(runtime):
    twitch, _, current, _, _ = runtime
    twitch.inventory.clear()
    item = campaign(twitch, "chain", "High", drops=[
        _drop("parent", "Parent", has_benefit=False),
        _drop("child", "Child", preconditions=("parent",)),
        _drop("sibling", "Sibling"),
    ])
    index(twitch)
    twitch.wanted_games = [item.game]
    before = capture(runtime).bound
    eligible = before.channels.watching.eligible_drop_ids
    assert len(before.inputs.drops) == 3 and len(eligible) == 2
    assert {drop.id for drop in before.inputs.drops if not drop.prerequisite_ids} == eligible
    assert before.channels.allocation_known is False
    item.timed_drops["parent"].is_claimed = True
    after = capture(runtime).bound
    assert len(after.inputs.drops) == 2
    assert after.channels.watching.eligible_drop_ids == frozenset(drop.id for drop in after.inputs.drops)
    assert current is twitch.watching_channel.get_with_default(None)


def test_fallback_is_visible_to_baseline_even_when_it_has_no_selected_reward(runtime):
    twitch, item, current, _, _ = runtime
    fallback = campaign(twitch, "fallback", "Urgent", drops=[_drop("fallback", "Fallback")])
    index(twitch)
    twitch.settings.games_to_watch = ["High"]
    twitch.settings.priority_list_only = False
    twitch.wanted_games = [item.game, fallback.game]
    current._stream = None
    other = channel(twitch, 20, fallback.game)
    twitch.channels[other.id] = other
    routes = capture(runtime).bound.channels
    assert routes.candidates[1].watchable is True
    assert routes.candidates[1].eligible_drop_ids == frozenset()
    assert routes.candidates[1].mining_priority == (1, 1)
    assert routes.automatic_action == "switch" and routes.automatic_channel_id == "20"
    assert routes.baseline_complete is False


@pytest.mark.parametrize("special", [509663, 509672])
@pytest.mark.parametrize("strict", [True, False])
def test_special_acl_uses_streamed_category_priority_and_real_selected_tier(runtime, special, strict):
    twitch, item, current, _, _ = runtime
    item.game = Game({"id": str(special), "name": "Special"})
    twitch.settings.games_to_watch = ["Special"]
    twitch.settings.priority_list_only = strict
    item.allowed_channels = [current]
    current.acl_based = True
    current._stream.drops_enabled = False
    twitch.wanted_games = [item.game]
    routes = capture(runtime).bound.channels
    assert routes.watching.watchable is True
    assert routes.watching.game_id == 1
    assert routes.watching.mining_priority == (0, MAX_INT)
    assert len(routes.watching.eligible_drop_ids) == 1
    assert routes.automatic_action == "keep"


@pytest.mark.parametrize("candidate_acl,current_acl", list(product((False, True), repeat=2)))
def test_ties_keep_actual_tracked_order_and_current_acl_precedence(runtime, candidate_acl, current_acl):
    twitch, item, current, _, _ = runtime
    current.acl_based = current_acl
    other = channel(twitch, 9, item.game, acl=candidate_acl)
    twitch.channels = OrderedDict(((other.id, other), (current.id, current)))
    routes = capture(runtime).bound.channels
    expected = candidate_acl and not current_acl
    assert [row.channel_id for row in routes.candidates] == ["9", "10"]
    assert routes.automatic_action == ("switch" if expected else "keep")
    assert routes.automatic_channel_id == ("9" if expected else "10")


def test_current_outside_tracked_table_is_not_omitted_or_automatically_replaced(runtime):
    twitch, item, current, _, _ = runtime
    other = channel(twitch, 9, item.game)
    twitch.channels = OrderedDict(((other.id, other),))
    routes = capture(runtime).bound.channels
    assert [row.channel_id for row in routes.candidates] == ["9"]
    assert routes.watching.channel_id == "10"
    assert routes.automatic_action == "keep" and routes.automatic_channel_id == "10"
    assert twitch.watching_channel.get_with_default(None) is current


def test_special_acl_without_a_streamed_category_can_still_be_eligible(runtime):
    twitch, item, current, _, _ = runtime
    item.game = Game({"id": "509663", "name": "Special"})
    item.allowed_channels = [current]
    twitch.settings.games_to_watch = ["Special"]
    twitch.wanted_games = [item.game]
    current._stream.game = None
    current._stream.drops_enabled = False
    routes = capture(runtime).bound.channels
    assert routes.watching.watchable is True and routes.watching.game_id is None
    assert routes.watching.mining_priority == (0, MAX_INT)
    assert len(routes.watching.eligible_drop_ids) == 1


@pytest.mark.parametrize("manual", ["channel", "game", "both", "selection"])
def test_manual_targeting_refuses_before_channel_capture_without_consuming_intent(runtime, monkeypatch, manual):
    twitch, item, current, guard, _ = runtime
    if manual in ("channel", "both"):
        twitch._manual_target_channel = current
    if manual in ("game", "both"):
        twitch._manual_target_game = item.game
    if manual == "selection":
        twitch.gui._selected_channel_id = 999
    project = MagicMock(side_effect=AssertionError("Manual mode takes precedence"))
    monkeypatch.setattr(guard._channels, "capture", project)
    result = capture(runtime)
    assert result.reason == "manual_override" and result.bound is None
    assert twitch.gui._selected_channel_id == (999 if manual == "selection" else None)
    twitch.gui.channels.get_selection.assert_not_called()
    project.assert_not_called()


@pytest.mark.parametrize("field,value", [("_games_update_pending", True), ("_changing_auth", True), ("_state", State.EXIT)])
def test_busy_or_stopped_clients_do_not_enter_channel_projection(runtime, monkeypatch, field, value):
    twitch, _, _, guard, _ = runtime
    setattr(twitch, field, value)
    project = MagicMock(side_effect=AssertionError("No projection while inactive"))
    monkeypatch.setattr(guard._channels, "capture", project)
    result = capture(runtime)
    assert result.reason == "inactive" and result.bound is None
    project.assert_not_called()


def test_replaced_stream_cannot_reuse_captured_channel_eligibility(runtime):
    twitch, item, current, guard, _ = runtime
    bound = capture(runtime).bound
    current._stream = channel(twitch, current.id, item.game)._stream
    assert guard.validate(bound, twitch, now=NOW, enabled=True) == "superseded"
    assert bound.channels.watching.watchable is True
    assert capture(runtime).bound is not bound


@pytest.mark.parametrize("owner", ["campaign", "drop"])
def test_foreign_model_owner_does_not_supply_current_channel_eligibility(runtime, owner):
    _, item, _, _, _ = runtime
    (item if owner == "campaign" else item.timed_drops["reward"])._twitch = MagicMock()
    result = capture(runtime)
    assert result.reason == "invalid_snapshot" and result.bound is None


def test_same_id_tracked_replacement_does_not_overwrite_current_objects_priority(runtime):
    twitch, item, current, _, _ = runtime
    current._stream.game = Game({"id": "3", "name": "Other"})
    item.game = Game({"id": "509663", "name": "Special"})
    item.allowed_channels = [current]
    twitch.settings.games_to_watch = ["Special", "High"]
    high = Game({"id": "1", "name": "High"})
    twitch.wanted_games = [item.game, high]
    replacement = channel(twitch, current.id, high)
    twitch.channels[current.id] = replacement
    routes = capture(runtime).bound.channels
    assert routes.watching.channel_id == routes.candidates[0].channel_id == "10"
    assert routes.watching.mining_priority == (0, MAX_INT)
    assert routes.candidates[0].mining_priority == (0, 1)
    assert routes.automatic_action == "switch" and routes.automatic_channel_id == "10"
    assert twitch.watching_channel.get_with_default(None) is current


@pytest.mark.parametrize("bad", [None, True, (0, True), (2, 0), (0, MAX_INT + 1), [0, 0]])
def test_unknown_priority_refuses_the_entire_capture(runtime, monkeypatch, bad):
    twitch = runtime[0]
    monkeypatch.setattr(twitch._channel_service, "get_mining_priority", lambda channel: bad)
    result = capture(runtime)
    assert result.reason == "invalid_snapshot" and result.bound is None


@pytest.mark.parametrize("value", [None, 1, "yes"])
def test_non_boolean_watchability_is_not_truthy_eligibility(runtime, monkeypatch, value):
    monkeypatch.setattr(runtime[0]._watch_service, "can_watch", lambda channel: value)
    result = capture(runtime)
    assert result.reason == "invalid_snapshot" and result.bound is None


@pytest.mark.parametrize("service", ["_watch_service", "_channel_service"])
def test_service_replacement_invalidates_context_and_foreign_owners_are_rejected(runtime, service):
    twitch, _, _, guard, _ = runtime
    bound = capture(runtime).bound
    factory = WatchService if service == "_watch_service" else ChannelService
    setattr(twitch, service, factory(twitch))
    assert guard.validate(bound, twitch, now=NOW, enabled=True) == "superseded"
    setattr(twitch, service, factory(MagicMock()))
    result = capture(runtime)
    assert result.reason == "invalid_snapshot" and result.bound is None


@pytest.mark.parametrize("owner", ["watch_service", "channel_service", "campaign", "drop", "channel"])
def test_in_place_owner_rebinding_retires_capture(runtime, owner):
    twitch, item, current, guard, _ = runtime
    bound = capture(runtime).bound
    target = {
        "watch_service": twitch._watch_service, "channel_service": twitch._channel_service,
        "campaign": item, "drop": item.timed_drops["reward"], "channel": current,
    }[owner]
    original = target._twitch
    target._twitch = SimpleNamespace(wanted_games=[], inventory=[], settings=twitch.settings)
    assert guard.validate(bound, twitch, now=NOW, enabled=True) == "superseded"
    assert capture(runtime).reason == "invalid_snapshot"
    target._twitch = original
    assert guard.validate(bound, twitch, now=NOW, enabled=True) == "superseded"


def test_mutation_during_real_eligibility_read_refuses_mixed_capture(runtime, monkeypatch):
    twitch = runtime[0]
    real = twitch._watch_service.can_watch

    def changed(channel):
        result = real(channel)
        twitch.settings.connection_quality += 1
        return result

    monkeypatch.setattr(twitch._watch_service, "can_watch", changed)
    result = capture(runtime)
    assert result.reason == "superseded" and result.bound is None


@pytest.mark.parametrize("delay", [15, 16])
def test_projection_processing_counts_against_original_context_lifetime(runtime, monkeypatch, delay):
    guard, clock = runtime[3:]
    real = guard._channels.capture

    def slow(*args):
        result = real(*args)
        clock[0] += delay
        return result

    monkeypatch.setattr(guard._channels, "capture", slow)
    result = capture(runtime)
    assert result.reason == "expired" and result.bound is None


def test_channel_work_budget_refuses_before_service_calls_without_partial_result(runtime, monkeypatch):
    twitch, _, _, guard, _ = runtime
    guard._channels = DeadlineChannelAdapter(max_work=1)
    watcher = MagicMock(side_effect=AssertionError("Budget must apply before eligibility work"))
    monkeypatch.setattr(twitch._watch_service, "can_watch", watcher)
    result = capture(runtime)
    assert result.reason == "budget_exceeded" and result.bound is None
    watcher.assert_not_called()


def test_wanted_game_membership_scans_are_charged_before_route_reads(runtime, monkeypatch):
    twitch, item, _, guard, _ = runtime
    twitch.wanted_games = [Game({"id": str(100 + key), "name": f"Other{key}"}) for key in range(10)] + [item.game]
    guard._channels = DeadlineChannelAdapter(max_work=20)
    watcher = MagicMock(wraps=twitch._watch_service.can_watch)
    monkeypatch.setattr(twitch._watch_service, "can_watch", watcher)
    result = capture(runtime)
    assert result.reason == "budget_exceeded" and result.bound is None
    watcher.assert_not_called()


@pytest.mark.parametrize("size", [1, 512])
def test_acl_membership_scans_are_charged_before_eligibility_reads(runtime, monkeypatch, size):
    twitch, item, current, guard, _ = runtime
    item.allowed_channels = [channel(twitch, key + 100, item.game, online=False) for key in range(size - 1)] + [current]
    guard._channels = DeadlineChannelAdapter(max_work=40)
    watcher = MagicMock(wraps=twitch._watch_service.can_watch)
    monkeypatch.setattr(twitch._watch_service, "can_watch", watcher)
    result = capture(runtime)
    if size == 1:
        assert result.reason == "captured" and result.bound is not None
    else:
        assert result.reason == "budget_exceeded" and result.bound is None
        watcher.assert_not_called()


@pytest.mark.parametrize("limit", [0, True, 2_000_001])
def test_invalid_channel_budget_is_rejected(limit):
    with pytest.raises(ValueError):
        DeadlineChannelAdapter(max_work=limit)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["keep", "promote_acl", "offline", "absent", "untracked", "idle", "higher", "lower"])
async def test_capture_matches_actual_channel_switch_state_step(runtime, monkeypatch, scenario):
    twitch, item, current, _, _ = runtime
    other = channel(twitch, 9, item.game, acl=scenario == "promote_acl")
    twitch.channels[other.id] = other
    if scenario == "offline":
        current._stream = None
    elif scenario == "absent":
        twitch.watching_channel.clear()
    elif scenario == "untracked":
        del twitch.channels[current.id]
    elif scenario == "idle":
        current._stream = other._stream = None
    elif scenario in ("higher", "lower"):
        urgent = campaign(twitch, "urgent", "Urgent", drops=[_drop("urgent", "Urgent")])
        index(twitch)
        twitch.wanted_games = [item.game, urgent.game]
        (current if scenario == "higher" else other)._stream.game = urgent.game
    captured = capture(runtime)
    assert captured.reason == "captured"
    routes = captured.bound.channels

    async def noop():
        pass

    async def exit_step():
        twitch._state = State.EXIT

    twitch._ensure_api_clients = lambda: None
    twitch.get_auth = AsyncMock(return_value=twitch._auth_state)
    twitch.websocket = MagicMock(start=AsyncMock())
    twitch._message_handler_service = MagicMock()
    twitch._watching_task = None
    twitch._schedule_task = None
    twitch._stop_mining_schedule = AsyncMock()
    twitch._mining_schedule_loop = noop
    twitch._watch_service.watch_loop = noop
    twitch.request_inventory_refresh = MagicMock()
    twitch._state_change = MagicMock(wait=exit_step)
    twitch.get_active_campaign = MagicMock(return_value=None)
    twitch.watch = MagicMock()
    await asyncio.wait_for(twitch._run(), 1)
    await asyncio.gather(twitch._watching_task, twitch._schedule_task)
    if routes.automatic_action == "switch":
        twitch.watch.assert_called_once()
        assert str(twitch.watch.call_args.args[0].id) == routes.automatic_channel_id
    else:
        twitch.watch.assert_not_called()
        if routes.automatic_action == "keep":
            assert str(twitch.watching_channel.get_with_default(None).id) == routes.automatic_channel_id
        else:
            twitch.gui.print.assert_called_once()
    twitch.websocket.start.assert_awaited_once()
