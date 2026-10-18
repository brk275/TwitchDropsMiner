"""Selected campaign guesses and complete estimate batches work together."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import MAX_EXTRA_MINUTES, State
from src.models.channel import Channel
from src.services.inventory_service import InventoryService
from src.services.watch_service import WatchService
from src.utils.async_helpers import AwaitableValue
from tests.test_drop_ignore_policy import _drop
from tests.test_priority_fallback import _campaign, _channel, _settings


@pytest.fixture
def mining_pair():
    client = MagicMock()
    client.settings = _settings("Wanted", priority_list_only=True)
    client.is_mining_time.return_value = True
    wanted = _campaign(
        "wanted", "Wanted", settings=client.settings, drops=[
            _drop("base", "Wanted foundation", has_benefit=False, required_minutes=60),
            _drop("left", "Wanted left", preconditions=("base",), required_minutes=100),
            _drop("right", "Wanted right", preconditions=("base",), required_minutes=100),
            _drop("parallel", "Wanted parallel", required_minutes=200),
        ],
    )
    unwanted = _campaign(
        "unwanted", "Special", game_id=509663, settings=client.settings,
        drops=[_drop("other", "Other reward", required_minutes=1)], acl=(100,),
    )
    client.inventory = [unwanted, wanted]
    for campaign in client.inventory:
        campaign._twitch = client
        for drop in campaign.drops:
            drop._twitch = client
    client.wanted_games = [wanted.game]
    current = _channel(client, 100, {"id": "1", "name": "Wanted"})
    client.watching_channel = AwaitableValue()
    client.watching_channel.set(current)
    client._inventory_service = InventoryService(client)
    client._drops = {drop.id: drop for campaign in client.inventory for drop in campaign.drops}
    return client, wanted, unwanted, current


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_claimed", [False, True])
@pytest.mark.parametrize("reported", [False, True])
async def test_watch_loop_combines_selected_guesses_complete_batches_and_prerequisites(
    mining_pair, monkeypatch, parent_claimed, reported,
):
    client, wanted, unwanted, current = mining_pair
    base = wanted.timed_drops["base"]
    if parent_claimed:
        base.is_claimed = True
        base.real_current_minutes = base.required_minutes
    limited = wanted.timed_drops["left"] if parent_claimed else base
    limited.extra_current_minutes = MAX_EXTRA_MINUTES - 1
    eligible = {drop.id for drop in wanted.drops if drop.can_earn(current)}
    before_real = {drop.id: drop.real_current_minutes for drop in wanted.drops}
    assert wanted.can_earn(current) and unwanted.can_earn(current)
    assert client._inventory_service.get_active_campaign(current) is wanted

    progress = {"dropID": "parallel", "currentMinutesWatched": 5} if reported else None
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
    for drop in wanted.drops:
        expected_extra = 0
        if not reported and drop.id in eligible:
            expected_extra = MAX_EXTRA_MINUTES if drop is limited else 1
        assert drop.extra_current_minutes == expected_extra
        assert drop.real_current_minutes == before_real[drop.id] + (5 if reported and drop.id in eligible else 0)
    assert unwanted.timed_drops["other"].real_current_minutes == 0
    assert unwanted.timed_drops["other"].extra_current_minutes == 0
    if reported:
        client.change_state.assert_not_called()
    else:
        client.change_state.assert_called_once_with(State.CHANNEL_SWITCH)
    assert wanted.claimed_drops == int(parent_claimed)


@pytest.mark.parametrize("blocker", ["ignored", "expired", "claimed"])
def test_policy_and_expiry_changes_do_not_fall_back_to_an_unwanted_campaign(mining_pair, blocker):
    client, wanted, unwanted, current = mining_pair
    assert client._inventory_service.get_active_campaign(current) is wanted
    if blocker == "ignored":
        client.settings.drop_name_blacklist = ["Wanted"]
    elif blocker == "expired":
        wanted.ends_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    else:
        for drop in wanted.drops:
            drop.is_claimed = True
    assert not wanted.can_earn(current)
    assert unwanted.can_earn(current)
    assert client._inventory_service.get_active_campaign(current) is None
    assert client.wanted_games == [wanted.game]
