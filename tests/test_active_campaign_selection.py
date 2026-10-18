"""Campaign lookup must honor the requested channel and runtime wanted queue."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.models.channel import Channel
from src.models.game import Game
from src.services.inventory_service import InventoryService
from src.services.watch_service import WatchService
from src.utils.async_helpers import AwaitableValue
from tests.test_priority_fallback import _campaign, _channel, _drop, _settings


@pytest.fixture
def client():
    twitch = MagicMock()
    twitch.settings = _settings("Wanted", priority_list_only=True)
    twitch.inventory = []
    twitch.wanted_games = []
    twitch.watching_channel = AwaitableValue()
    twitch.is_mining_time.return_value = True
    return twitch


def add_campaign(client, key, game, game_id, minutes, *, acl=()):
    item = _campaign(key, game, game_id=game_id, settings=client.settings,
                     drops=[_drop(f"{key}-drop", required_minutes=minutes)], acl=acl)
    item._twitch = client
    for drop in item.drops:
        drop._twitch = client
    client.inventory.append(item)
    return item


def test_explicit_channel_is_not_replaced_by_current_watched_channel(client):
    first = add_campaign(client, "first", "Wanted", 1, 60)
    second = add_campaign(client, "second", "Other Wanted", 2, 30)
    current = _channel(client, 100, {"id": "1", "name": "Wanted"})
    requested = _channel(client, 200, {"id": "2", "name": "Other Wanted"})
    client.wanted_games = [first.game, second.game]
    client.watching_channel.set(current)

    assert InventoryService(client).get_active_campaign(requested) is second
    assert client.watching_channel.get_with_default(None) is current


def test_unwanted_special_campaign_does_not_receive_fallback_estimate(client):
    wanted = add_campaign(client, "wanted", "Wanted", 1, 60)
    unwanted = add_campaign(client, "unwanted", "Special Events", 509663, 1, acl=(100,))
    current = _channel(client, 100, {"id": "1", "name": "Wanted"})
    client.wanted_games = [wanted.game]
    client.watching_channel.set(current)
    assert wanted.can_earn(current) and unwanted.can_earn(current)

    active = InventoryService(client).get_active_campaign()
    assert active is wanted
    active.bump_minutes(current)
    assert wanted.timed_drops["wanted-drop"].extra_current_minutes == 1
    assert unwanted.timed_drops["unwanted-drop"].extra_current_minutes == 0


def test_omitted_channel_still_uses_watched_channel(client):
    wanted = add_campaign(client, "wanted", "Wanted", 1, 60)
    client.wanted_games = [wanted.game]
    current = _channel(client, 100, {"id": "1", "name": "Wanted"})
    client.watching_channel.set(current)
    assert InventoryService(client).get_active_campaign() is wanted


def test_no_watched_or_explicit_channel_has_no_active_campaign(client):
    wanted = add_campaign(client, "wanted", "Wanted", 1, 60)
    client.wanted_games = [wanted.game]
    assert InventoryService(client).get_active_campaign() is None


@pytest.mark.parametrize("watched", [True, False])
def test_explicit_channel_with_no_eligible_campaign_does_not_fall_back_to_current(client, watched):
    wanted = add_campaign(client, "wanted", "Wanted", 1, 60)
    client.wanted_games = [wanted.game]
    if watched:
        client.watching_channel.set(_channel(client, 100, {"id": "1", "name": "Wanted"}))
    requested = _channel(client, 200, {"id": "2", "name": "Other"})
    assert InventoryService(client).get_active_campaign(requested) is None


def test_distinct_same_id_explicit_channel_keeps_its_own_stream_context(client):
    first = add_campaign(client, "first", "Wanted", 1, 60)
    second = add_campaign(client, "second", "Other Wanted", 2, 30)
    client.wanted_games = [first.game, second.game]
    current = _channel(client, 100, {"id": "1", "name": "Wanted"})
    requested = _channel(client, 100, {"id": "2", "name": "Other Wanted"})
    client.watching_channel.set(current)
    assert current == requested and current is not requested
    assert InventoryService(client).get_active_campaign(requested) is second
    assert client.watching_channel.get_with_default(None) is current


def test_explicit_lookup_does_not_read_or_consume_watched_value(client):
    wanted = add_campaign(client, "wanted", "Wanted", 1, 60)
    client.wanted_games = [wanted.game]
    client.watching_channel = MagicMock()
    client.watching_channel.get_with_default.side_effect = AssertionError("Explicit channel must be used")
    assert InventoryService(client).get_active_campaign(_channel(client, 100, {"id": "1", "name": "Wanted"})) is wanted
    client.watching_channel.get_with_default.assert_not_called()


@pytest.mark.parametrize("special_id", [509663, 509672])
@pytest.mark.parametrize("want_special", [False, True])
def test_wanted_queue_applies_in_both_directions_of_cross_category_credit(client, special_id, want_special):
    regular = add_campaign(client, "regular", "Wanted", 1, 1 if want_special else 60)
    special = add_campaign(client, "special", "Special", special_id, 60 if want_special else 1, acl=(100,))
    client.wanted_games = [(special if want_special else regular).game]
    current = _channel(client, 100, {"id": "1", "name": "Wanted"})
    assert regular.can_earn(current) and special.can_earn(current)
    assert InventoryService(client).get_active_campaign(current) is (special if want_special else regular)


def test_runtime_fallback_is_not_mistaken_for_an_unwanted_saved_game(client):
    fallback = add_campaign(client, "fallback", "Fallback", 2, 30)
    client.settings.priority_list_only = False
    client.wanted_games = [fallback.game]
    assert client.settings.games_to_watch == ["Wanted"]
    assert InventoryService(client).get_active_campaign(_channel(client, 100, {"id": "2", "name": "Fallback"})) is fallback
    assert client.settings.games_to_watch == ["Wanted"]


def test_game_identity_not_display_spelling_controls_membership(client):
    wanted = add_campaign(client, "wanted", "Wanted", 1, 60)
    client.wanted_games = [Game({"id": "1", "name": "Localized title"})]
    assert InventoryService(client).get_active_campaign(_channel(client, 100, {"id": "1", "name": "Wanted"})) is wanted


def test_shortest_remaining_wanted_campaign_is_still_selected(client):
    long = add_campaign(client, "long", "Wanted", 1, 60)
    short = add_campaign(client, "short", "Wanted", 1, 30)
    client.wanted_games = [long.game]
    current = _channel(client, 100, {"id": "1", "name": "Wanted"})
    assert InventoryService(client).get_active_campaign(current) is short
    short.timed_drops["short-drop"].is_claimed = True
    assert InventoryService(client).get_active_campaign(current) is long


def test_empty_wanted_queue_has_no_local_guess_even_with_earnable_campaign(client):
    wanted = add_campaign(client, "wanted", "Wanted", 1, 60)
    current = _channel(client, 100, {"id": "1", "name": "Wanted"})
    assert wanted.can_earn(current)
    assert InventoryService(client).get_active_campaign(current) is None


@pytest.mark.parametrize("blocker", ["expired_campaign", "upcoming_drop", "ignored", "unlinked", "claimed", "acl", "offline"])
def test_lookup_preserves_real_earning_guards(client, blocker):
    wanted = add_campaign(client, "wanted", "Wanted", 1, 60)
    current = _channel(client, 100, {"id": "1", "name": "Wanted"})
    client.wanted_games = [wanted.game]
    if blocker == "expired_campaign":
        wanted.ends_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif blocker == "upcoming_drop":
        wanted.timed_drops["wanted-drop"].starts_at = datetime.now(timezone.utc) + timedelta(hours=1)
    elif blocker == "ignored":
        client.settings.drop_name_blacklist = ["wanted-drop"]
    elif blocker == "unlinked":
        wanted.linked = False
    elif blocker == "claimed":
        wanted.timed_drops["wanted-drop"].is_claimed = True
    elif blocker == "acl":
        wanted.allowed_channels = [_channel(client, 200, {"id": "1", "name": "Wanted"})]
    else:
        current._stream = None
    assert InventoryService(client).get_active_campaign(current) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", [False, True])
async def test_watch_loop_scopes_guesses_but_keeps_other_authoritative_progress(client, monkeypatch, reported):
    wanted = add_campaign(client, "wanted", "Wanted", 1, 60)
    unwanted = add_campaign(client, "unwanted", "Special Events", 509663, 1, acl=(100,))
    client.wanted_games = [wanted.game]
    current = _channel(client, 100, {"id": "1", "name": "Wanted"})
    client.watching_channel.set(current)
    client._inventory_service = InventoryService(client)
    client._drops = {drop.id: drop for item in client.inventory for drop in item.drops}
    progress = {"dropID": "unwanted-drop", "currentMinutesWatched": 1} if reported else None
    client.gql_request = AsyncMock(return_value={"data": {"currentUser": {"dropCurrentSession": progress}}})
    client.gui.progress.minute_almost_done.return_value = True
    monkeypatch.setattr(Channel, "send_watch", AsyncMock(side_effect=[True, asyncio.CancelledError()]))
    clock = [0]
    monkeypatch.setattr("src.services.watch_service.monotonic", lambda: clock[0])
    watcher = WatchService(client)

    async def sleep_once(delay):
        clock[0] = 20

    watcher.watch_sleep = sleep_once
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(watcher.watch_loop(), 1)
    client.gql_request.assert_awaited_once()
    assert wanted.timed_drops["wanted-drop"].extra_current_minutes == (0 if reported else 1)
    assert unwanted.timed_drops["unwanted-drop"].extra_current_minutes == 0
    assert wanted.timed_drops["wanted-drop"].real_current_minutes == 0
    assert unwanted.timed_drops["unwanted-drop"].real_current_minutes == (1 if reported else 0)
