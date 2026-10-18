"""Exercise context invalidation without live sessions, networking or steering."""

import asyncio
import copy
from collections import OrderedDict
from datetime import timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.auth import _AuthState
from src.auth.browser_session import BrowserIdentity
from src.config import State
from src.models.channel import Channel, Stream
from src.services.deadline_context import DeadlineContextGuard
from src.services.inventory_service import InventoryService
from src.utils.async_helpers import AwaitableValue
from src.web.gui_manager import WebGUIManager
from src.web.managers.channels import ChannelListManager
from tests.test_deadline_forecast import NOW
from tests.test_deadline_inputs import campaign, client
from tests.test_drop_ignore_policy import _drop


@pytest.fixture
def state():
    twitch = client()
    twitch.settings.connection_quality = 1
    twitch.gui = MagicMock()
    twitch.gui._selected_channel_id = None
    twitch.gui.channels.get_selection.return_value = None
    twitch._auth_state = _AuthState(twitch)
    twitch._auth_state.user_id = 42
    twitch._auth_state._logged_in.set()
    twitch._deadline_context_epoch = object()
    twitch._state = State.CHANNEL_SWITCH
    twitch._changing_auth = False
    twitch._inventory_loaded = True
    twitch._games_update_pending = twitch._inventory_refresh_pending = twitch._clear_cache_pending = False
    twitch._state_change = asyncio.Event()
    twitch._schedule_changed = asyncio.Event()
    twitch._watch_service = MagicMock()
    twitch.drop_history = MagicMock()
    twitch._inventory_service = InventoryService(twitch)
    twitch._inventory_service.fetch_inventory = AsyncMock()
    item = campaign(twitch, "one", "High", minutes=0)
    twitch._campaigns = {item.id: item}
    twitch._drops = dict(item.timed_drops)
    twitch.wanted_games = [item.game]
    channel = Channel(twitch, id=10, login="test-channel")
    channel._stream = Stream(channel, id=100, game={"id": "1", "name": "High"}, viewers=20, title="Test")
    twitch.channels = OrderedDict({channel.id: channel})
    twitch.watching_channel = AwaitableValue()
    twitch.watching_channel.set(channel)
    clock = [100.0]
    guard = DeadlineContextGuard(clock=lambda: clock[0])
    return twitch, item, channel, guard, clock


def bind(state, *, allow_refusal=False, **kwargs):
    twitch, _, _, guard, _ = state
    result = guard.capture(twitch, **{"now": NOW, "horizon": NOW + timedelta(days=3), "enabled": True, **kwargs})
    if not allow_refusal:
        assert result.reason == "captured", result.reason
        assert result.bound is not None
    return result


def validate(state, bound, **kwargs):
    twitch, _, _, guard, _ = state
    return guard.validate(bound, twitch, **{"now": NOW, "enabled": True, **kwargs})


def test_binding_is_default_off_and_never_changes_mining_or_claims(state):
    twitch, item, channel, guard, _ = state
    result = guard.capture(twitch, now=NOW, horizon=NOW + timedelta(days=3))
    assert result.reason == "disabled" and result.bound is None
    bound = bind(state).bound
    assert bound is not None and bound.inputs.baseline_complete is False
    assert bound.valid_until == NOW + timedelta(seconds=15)
    assert validate(state, bound) == "current"
    assert validate(state, bound, now=NOW + timedelta(seconds=1)) == "current"
    assert twitch.watching_channel.get_with_default(None) is channel
    assert twitch.settings.games_to_watch == ["High", "Urgent"]
    assert not item.timed_drops["reward"].is_claimed
    twitch._watch_service.watch.assert_not_called()
    twitch.gui.channels.select.assert_not_called()
    assert "reward" not in repr(bound) and "42" not in repr(bound)


@pytest.mark.parametrize("enabled", [False, 1, "yes", None])
def test_disabling_or_non_boolean_enable_retires_previous_capture(state, enabled):
    bound = bind(state).bound
    assert validate(state, bound, enabled=enabled) == "disabled"
    assert validate(state, bound) == "superseded"


def test_new_capture_retires_older_instance_and_unknown_bindings_fail_closed(state):
    first = bind(state).bound
    second = bind(state).bound
    assert first.inputs == second.inputs and first is not second
    # Even another structurally equal instance is not the guard's current binding.
    assert validate(state, copy.copy(second)) == "superseded"
    assert validate(state, second) == "superseded"
    third = bind(state).bound
    assert validate(state, first) == "superseded"
    assert validate(state, third) == "superseded"


@pytest.mark.parametrize("wall,mono,expected", [
    (15, 0, "expired"), (0, 15, "expired"), (-1, 0, "expired"), (0, -1, "expired"),
    (0, float("nan"), "invalid_context"), (0, float("inf"), "invalid_context"),
    (14.999, 14.999, "current"),
])
def test_both_clocks_bound_lifetime_and_clock_failures_latch(state, wall, mono, expected):
    bound = bind(state).bound
    state[4][0] += mono
    assert validate(state, bound, now=NOW + timedelta(seconds=wall)) == expected
    if expected != "current":
        state[4][0] = 100
        assert validate(state, bound) == "superseded"


def test_aware_offsets_compare_by_instant_and_validation_does_not_extend_lifetime(state):
    bound = bind(state).bound
    later = (NOW + timedelta(seconds=14)).astimezone(timezone(timedelta(hours=-4)))
    assert validate(state, bound, now=later) == "current"
    assert bound.valid_until == NOW + timedelta(seconds=15)
    assert validate(state, bound, now=NOW + timedelta(seconds=15)) == "expired"


@pytest.mark.parametrize("boundary", ["campaign_end", "drop_end", "drop_start", "hours"])
def test_expiry_is_clipped_to_policy_and_reward_boundaries(state, boundary):
    twitch, item, _, _, _ = state
    edge = NOW + timedelta(seconds=5)
    if boundary == "campaign_end":
        item.ends_at = edge
    elif boundary == "drop_end":
        item.timed_drops["reward"].ends_at = edge
    elif boundary == "drop_start":
        item.timed_drops["reward"].starts_at = edge
    else:
        # Put capture five seconds before the scheduled exclusive end.
        twitch.settings.mining_hours = {"mode": "range", "start": "00:00", "end": "01:00"}
        begin = NOW.replace(hour=0, minute=59, second=55)
        twitch.is_mining_time = lambda: True
        result = state[3].capture(twitch, now=begin, horizon=begin + timedelta(hours=2),
                                  enabled=True, local_timezone=timezone.utc)
        assert result.bound.valid_until == begin + timedelta(seconds=5)
        assert validate(state, result.bound, now=begin + timedelta(seconds=5),
                        local_timezone=timezone.utc) == "expired"
        return
    bound = bind(state).bound
    assert bound.valid_until == edge
    assert validate(state, bound, now=edge - timedelta(microseconds=1)) == "current"
    assert validate(state, bound, now=edge) == "expired"


@pytest.mark.parametrize("field,value", [
    ("_changing_auth", True), ("_inventory_loaded", False), ("_games_update_pending", True),
    ("_inventory_refresh_pending", True), ("_clear_cache_pending", True), ("_state", State.EXIT),
])
def test_busy_or_stopping_runtime_refuses_capture_and_existing_bindings(state, field, value):
    bound = bind(state).bound
    setattr(state[0], field, value)
    assert validate(state, bound) == "inactive"
    assert bind(state, allow_refusal=True).reason == "inactive"


@pytest.mark.parametrize("manual", ["channel", "game", "both", "selection"])
def test_manual_and_selected_channel_precedence_is_retained(state, manual):
    twitch, item, channel, _, _ = state
    bound = bind(state).bound
    if manual in ("channel", "both"):
        twitch._manual_target_channel = channel
    if manual in ("game", "both"):
        twitch._manual_target_game = item.game
    if manual == "selection":
        twitch.gui._selected_channel_id = channel.id
    assert validate(state, bound) == "manual_override"
    assert bind(state, allow_refusal=True).reason == "manual_override"


@pytest.mark.parametrize("replacement", ["inventory", "settings", "campaign_index", "drop_index", "channel_index", "model", "stream"])
def test_equal_value_replacements_are_not_rebound_to_old_context(state, replacement):
    twitch, item, channel, _, _ = state
    bound = bind(state).bound
    if replacement == "inventory":
        twitch.inventory = list(twitch.inventory)
    elif replacement == "settings":
        twitch.settings = copy.copy(twitch.settings)
    elif replacement == "campaign_index":
        twitch._campaigns = dict(twitch._campaigns)
    elif replacement == "drop_index":
        twitch._drops = dict(twitch._drops)
    elif replacement == "channel_index":
        twitch.channels = OrderedDict(twitch.channels)
    elif replacement == "stream":
        channel._stream = copy.copy(channel._stream)
    else:
        clone = copy.copy(item)
        clone.timed_drops = {key: copy.copy(drop) for key, drop in item.timed_drops.items()}
        for drop in clone.drops:
            drop.campaign = clone
        twitch.inventory[0] = clone
        twitch._campaigns[item.id] = clone
        twitch._drops.update(clone.timed_drops)
    assert validate(state, bound) == "superseded"


@pytest.mark.parametrize("mutation", ["rank", "ignore", "benefit", "hours", "fallback", "unlinked", "quality", "progress", "estimate", "claim", "revision", "acl", "channel_game", "viewers", "drops_enabled", "ban", "watch", "observation"])
def test_in_place_mutations_cannot_reuse_the_capture(state, mutation):
    twitch, item, channel, _, _ = state
    bound = bind(state).bound
    drop = item.timed_drops["reward"]
    if mutation == "rank":
        twitch.settings.games_to_watch.reverse()
    elif mutation == "ignore":
        twitch.settings.drop_name_blacklist.append("Reward")
    elif mutation == "benefit":
        twitch.settings.mining_benefits["DIRECT_ENTITLEMENT"] = False
    elif mutation == "hours":
        twitch.settings.mining_hours["end"] = "21:00"
    elif mutation == "fallback":
        twitch.settings.priority_list_only = False
    elif mutation == "unlinked":
        twitch.settings.allow_unlinked_campaigns = True
    elif mutation == "quality":
        twitch.settings.connection_quality = 2
    elif mutation == "progress":
        drop.real_current_minutes += 1
    elif mutation == "estimate":
        drop.extra_current_minutes += 1
    elif mutation == "claim":
        drop._apply_claim_result(True)
        twitch._games_update_pending = False
    elif mutation == "revision":
        item.progress_revision += 1
    elif mutation == "acl":
        item.allowed_channels.append(channel)
    elif mutation == "channel_game":
        channel._stream.game = None
    elif mutation == "viewers":
        channel.viewers += 1
    elif mutation == "drops_enabled":
        channel._stream.drops_enabled = False
    elif mutation == "ban":
        channel._banned = True
    elif mutation == "watch":
        twitch.watching_channel.clear()
    else:
        drop.update_minutes(0, source="websocket")
    assert validate(state, bound) == "superseded"


@pytest.mark.parametrize("operation", ["games", "inventory", "manual", "state", "watch", "stop", "restart", "auth_clear", "auth_invalidate"])
def test_real_lifecycle_events_remain_invalid_after_visible_state_is_restored(state, operation):
    twitch, item, channel, _, _ = state
    bound = bind(state).bound
    if operation == "games":
        twitch.request_games_update()
        twitch._games_update_pending = False
    elif operation == "inventory":
        twitch.request_inventory_refresh()
        twitch._inventory_refresh_pending = False
    elif operation == "manual":
        twitch.enter_manual_mode(channel)
        twitch.clear_manual_mode()
    elif operation == "state":
        twitch.change_state(State.GAMES_UPDATE)
        twitch.change_state(State.CHANNEL_SWITCH)
    elif operation in ("watch", "stop", "restart"):
        if operation == "watch":
            twitch.watch(channel)
        else:
            getattr(twitch, f"{operation}_watching")()
    else:
        getattr(twitch._auth_state, "clear" if operation == "auth_clear" else "invalidate")()
        twitch._auth_state.user_id = 42
        twitch._auth_state._logged_in.set()
    assert validate(state, bound) == "superseded"


@pytest.mark.asyncio
async def test_direct_inventory_fetch_invalidates_before_awaiting_network_work(state):
    twitch = state[0]
    bound = bind(state).bound

    async def fetch():
        assert validate(state, bound) == "superseded"

    twitch._inventory_service.fetch_inventory.side_effect = fetch
    await twitch.fetch_inventory()


def test_real_same_account_session_renewal_invalidates_without_reading_secrets_into_capture(state):
    twitch = state[0]
    twitch._http_client = MagicMock()
    twitch._gql_client = MagicMock()
    twitch.websocket = SimpleNamespace(websockets=[])
    twitch.gui.login.get_status.return_value = {}
    identity = BrowserIdentity(42, "synthetic-test-token", "synthetic-test-device", "Test Agent")
    first = bind(state).bound
    twitch._auth_state._use_browser_identity(identity)
    assert validate(state, first) == "superseded"
    second = bind(state).bound
    twitch._auth_state.accept_imported_identity(identity)
    assert validate(state, second) == "current"
    twitch._auth_state.accept_imported_identity(
        BrowserIdentity(42, "synthetic-renewed-token", "synthetic-test-device", "Test Agent"),
    )
    assert validate(state, second) == "superseded"


@pytest.mark.asyncio
async def test_authentication_change_retires_context_before_draining_and_never_revives_it(state):
    twitch = state[0]
    twitch._run_task = twitch._schedule_task = twitch._watching_task = twitch._mnt_task = None
    twitch._resume_mining = asyncio.Event()
    twitch._resume_mining.set()
    twitch.websocket = SimpleNamespace(stop=AsyncMock())
    twitch._stop_channel_tasks = AsyncMock()
    twitch.gui.login.stop_avatar = AsyncMock()
    twitch._channel_service = SimpleNamespace(clear_bans=MagicMock())
    twitch.diagnostics = SimpleNamespace(clear=MagicMock())
    twitch._inventory_service.clear_cached_state = MagicMock()
    old_epoch = twitch._deadline_context_epoch
    bound = bind(state).bound

    async def draining(**kwargs):
        assert twitch._deadline_context_epoch is not old_epoch
        assert validate(state, bound) == "inactive"

    twitch.websocket.stop.side_effect = draining
    async with twitch.authentication_change():
        assert bind(state, allow_refusal=True).reason == "inactive"
    twitch._state = State.CHANNEL_SWITCH
    twitch._inventory_loaded = True
    twitch._auth_state.user_id = 42
    twitch._auth_state._logged_in.set()
    assert validate(state, bound) == "superseded"


def test_capture_and_validation_do_not_inspect_credentials_or_authentication_files(state):
    class SecretTripwire(_AuthState):
        def __getattribute__(self, name):
            if name in ("access_token", "device_id", "session_id", "headers", "validate"):
                raise AssertionError("Credential access from context guard")
            return super().__getattribute__(name)

    class SettingTripwire(SimpleNamespace):
        def __getattribute__(self, name):
            if name in ("proxy", "telegram_bot_token", "telegram_chat_id", "discord_webhook_url", "save", "load"):
                raise AssertionError("Private setting access from context guard")
            return super().__getattribute__(name)

    twitch = state[0]
    twitch._auth_state = SecretTripwire(twitch)
    twitch._auth_state.user_id = 42
    twitch._auth_state._logged_in.set()
    twitch.settings = SettingTripwire(**vars(twitch.settings))
    bound = bind(state).bound
    assert validate(state, bound) == "current"


def test_foreign_client_and_auth_replacement_are_rejected(state):
    twitch, _, _, guard, _ = state
    bound = bind(state).bound
    assert guard.validate(bound, copy.copy(twitch), now=NOW, enabled=True) == "superseded"
    bound = bind(state).bound
    twitch._auth_state = copy.copy(twitch._auth_state)
    assert validate(state, bound) == "superseded"


@pytest.mark.parametrize("bad", ["missing_auth", "unknown_epoch", "invalid_flag", "wrong_drop_index", "too_many_channels", "too_many_parents", "nonfinite_counter", "naive_now"])
def test_invalid_or_oversized_state_returns_no_partial_binding(state, bad):
    twitch, item, channel, _, _ = state
    if bad == "missing_auth":
        del twitch._auth_state.user_id
    elif bad == "unknown_epoch":
        twitch._auth_state.context_epoch = None
    elif bad == "invalid_flag":
        twitch._changing_auth = 0
    elif bad == "wrong_drop_index":
        twitch._drops["reward"] = copy.copy(item.timed_drops["reward"])
    elif bad == "too_many_channels":
        twitch.channels = dict.fromkeys(range(200), channel)
    elif bad == "too_many_parents":
        item.timed_drops["reward"].precondition_drops = ["reward"] * 257
    elif bad == "nonfinite_counter":
        item.timed_drops["reward"].extra_current_minutes = float("nan")
    else:
        result = bind(state, allow_refusal=True, now=NOW.replace(tzinfo=None))
        assert result.bound is None
        return
    result = bind(state, allow_refusal=True)
    assert result.bound is None and result.reason in ("invalid_context", "budget_exceeded")


def test_timezone_change_cannot_reuse_old_capture(state):
    bound = bind(state, local_timezone=timezone.utc).bound
    assert validate(state, bound, local_timezone=timezone(timedelta(hours=1))) == "superseded"
    assert validate(state, bound, local_timezone=timezone.utc) == "superseded"


def test_capture_processing_time_consumes_its_lifetime(state):
    times = iter((100.0, 115.0))
    guard = DeadlineContextGuard(clock=lambda: next(times))
    result = guard.capture(state[0], now=NOW, horizon=NOW + timedelta(days=3), enabled=True)
    assert result.reason == "expired" and result.bound is None


def test_capture_processing_time_cannot_cross_a_nearby_reward_boundary(state):
    state[1].ends_at = NOW + timedelta(seconds=5)
    times = iter((100.0, 106.0))
    guard = DeadlineContextGuard(clock=lambda: next(times))
    result = guard.capture(state[0], now=NOW, horizon=NOW + timedelta(days=3), enabled=True)
    assert result.reason == "expired" and result.bound is None


@pytest.mark.parametrize("phase", ["capture", "validation"])
@pytest.mark.parametrize("pending", [10, 999])
def test_real_pending_selection_is_preserved_even_when_its_channel_is_unknown(state, phase, pending):
    twitch = state[0]
    gui = object.__new__(WebGUIManager)
    gui._twitch = twitch
    gui._selected_channel_id = None
    gui.channels = ChannelListManager(MagicMock(), gui)
    twitch.gui = gui
    bound = bind(state).bound if phase == "validation" else None
    gui.select_channel(pending)
    if phase == "capture":
        result = bind(state, allow_refusal=True)
        assert result.reason == "manual_override" and result.bound is None
    else:
        assert validate(state, bound) == "manual_override"
        assert validate(state, bound) == "superseded"
    assert gui._selected_channel_id == pending
    # The normal consumer still gets exactly the queued ID and owns its removal.
    assert gui.get_selected_channel_id() == pending
    assert gui._selected_channel_id is None


@pytest.mark.parametrize("relationship", ["tracked", "acl"])
def test_channel_relationship_replacement_is_checked_even_when_both_nodes_were_seen(state, relationship):
    twitch, first, channel, _, _ = state
    other = copy.copy(channel)
    other._stream = copy.copy(channel._stream)
    other._stream.channel = other
    other._stream.drops_enabled = False
    other._banned = True
    second = campaign(twitch, "second", "Urgent", drops=[_drop("second-reward", "Second")])
    twitch._campaigns[second.id] = second
    twitch._drops.update(second.timed_drops)
    first.allowed_channels = [channel, other] if relationship == "acl" else [channel]
    second.allowed_channels = [channel] if relationship == "acl" else [other]
    bound = bind(state).bound
    if relationship == "tracked":
        twitch.channels[channel.id] = other
    else:
        second.allowed_channels[:] = [other]
    assert validate(state, bound) == "superseded"
    twitch.channels[channel.id] = channel
    second.allowed_channels[:] = [channel]
    assert validate(state, bound) == "superseded"


@pytest.mark.parametrize("which", ["wall", "monotonic", "both"])
def test_clock_high_water_marks_survive_successful_validation(state, which):
    bound = bind(state).bound
    state[4][0] = 110
    assert validate(state, bound, now=NOW + timedelta(seconds=10)) == "current"
    state[4][0] = 105 if which in ("monotonic", "both") else 111
    wall = 5 if which in ("wall", "both") else 11
    assert validate(state, bound, now=NOW + timedelta(seconds=wall)) == "expired"
    state[4][0] = 112
    assert validate(state, bound, now=NOW + timedelta(seconds=12)) == "superseded"


def test_validation_finish_tick_is_a_high_water_mark_too(state):
    times = iter((100.0, 100.0, 110.0, 111.0, 110.5, 110.5))
    guard = DeadlineContextGuard(clock=lambda: next(times))
    bound = guard.capture(state[0], now=NOW, horizon=NOW + timedelta(days=3), enabled=True).bound
    assert guard.validate(bound, state[0], now=NOW + timedelta(seconds=10), enabled=True) == "current"
    assert guard.validate(bound, state[0], now=NOW + timedelta(seconds=12), enabled=True) == "expired"


@pytest.mark.parametrize("wall,expected", [(2, "expired"), (4, "current")])
def test_capture_completion_is_the_first_wall_clock_high_water_mark(state, wall, expected):
    times = iter((100.0, 104.0, 104.0, 104.0))
    guard = DeadlineContextGuard(clock=lambda: next(times))
    bound = guard.capture(state[0], now=NOW, horizon=NOW + timedelta(days=3), enabled=True).bound
    assert bound is not None
    assert guard.validate(bound, state[0], now=NOW + timedelta(seconds=wall), enabled=True) == expected
    if expected == "expired":
        assert guard.validate(bound, state[0], now=NOW + timedelta(seconds=5), enabled=True) == "superseded"


def test_validation_processing_respects_utc_remaining_after_a_forward_wall_step(state):
    times = iter((100.0, 100.0, 104.0, 106.0))
    guard = DeadlineContextGuard(clock=lambda: next(times))
    bound = guard.capture(state[0], now=NOW, horizon=NOW + timedelta(days=3), enabled=True).bound
    assert guard.validate(bound, state[0], now=NOW + timedelta(seconds=14), enabled=True) == "expired"
    assert guard.validate(bound, state[0], now=NOW, enabled=True) == "superseded"


def test_equal_tuple_cannot_replace_the_list_required_by_the_input_adapter(state):
    bound = bind(state).bound
    state[0].settings.games_to_watch = tuple(state[0].settings.games_to_watch)
    assert validate(state, bound) == "superseded"
    state[0].settings.games_to_watch = list(state[0].settings.games_to_watch)
    assert validate(state, bound) == "superseded"


@pytest.mark.asyncio
async def test_same_state_user_selection_request_cannot_be_consumed_then_hide_from_guard(state, monkeypatch):
    from src.web import app as webapp

    twitch = state[0]
    gui = object.__new__(WebGUIManager)
    gui._twitch = twitch
    gui._selected_channel_id = None
    gui.channels = ChannelListManager(MagicMock(), gui)
    twitch.gui = gui
    monkeypatch.setattr(webapp, "gui_manager", gui)
    monkeypatch.setattr(webapp, "twitch_client", twitch)
    bound = bind(state).bound
    assert await webapp.select_channel(webapp.ChannelSelectRequest(channel_id=state[2].id)) == {"success": True}
    assert gui.get_selected_channel_id() == state[2].id
    assert twitch._state is State.CHANNEL_SWITCH
    assert validate(state, bound) == "superseded"


def test_many_duplicate_channel_relationships_hit_a_shared_budget(state):
    twitch, first, channel, _, _ = state
    first.allowed_channels = [channel] * 512
    for index in range(1, 9):
        item = campaign(twitch, str(index), "High", drops=[_drop(f"drop-{index}", "Reward")])
        item.allowed_channels = [channel] * 512
        twitch._campaigns[item.id] = item
        twitch._drops.update(item.timed_drops)
    result = bind(state, allow_refusal=True)
    assert result.reason == "budget_exceeded" and result.bound is None


def test_validation_processing_time_cannot_extend_the_binding(state):
    times = iter((100.0, 100.0, 100.0, 115.0))
    guard = DeadlineContextGuard(clock=lambda: next(times))
    result = guard.capture(state[0], now=NOW, horizon=NOW + timedelta(days=3), enabled=True)
    assert result.bound is not None
    assert guard.validate(result.bound, state[0], now=NOW, enabled=True) == "expired"


@pytest.mark.parametrize("age", [timedelta(0), timedelta(seconds=-1), timedelta(seconds=31), 15, None])
def test_lifetime_requires_a_positive_bounded_duration(age):
    with pytest.raises(ValueError, match="lifetime"):
        DeadlineContextGuard(max_age=age)
