"""Direct observations stay truthful across polling and estimate recovery."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import MAX_EXTRA_MINUTES, State
from src.services.message_handlers import MessageHandlerService
from src.web.managers.inventory import InventoryManager
from tests.test_stale_progress import mining_state as mining_state
from tests.test_stale_progress import poll


RECEIVED_AT = datetime(2026, 10, 17, 20, tzinfo=timezone.utc)


async def observe_websocket(twitch, drop_id, minutes):
    await MessageHandlerService(twitch).process_drops(1, {
        "type": "drop-progress",
        "data": {
            "drop_id": drop_id, "current_progress_min": minutes,
            "required_progress_min": 60,
        },
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("source_id", ["drop", "sibling"])
@pytest.mark.parametrize("response", ["session", "null", "error", "timeout"])
async def test_superseded_replies_do_not_replace_direct_observations(
    mining_state, source_id, response,
):
    twitch, channel, campaign = mining_state
    other_id = "sibling" if source_id == "drop" else "drop"
    original_other = campaign.timed_drops[other_id].progress_observation

    async def observe(request):
        await observe_websocket(twitch, source_id, 11)

    with patch("src.models.drop.datetime", wraps=datetime) as clock:
        clock.now.return_value = RECEIVED_AT
        assert await poll(twitch, observe, returned_minutes=9, response=response) == [20]

    assert all(drop.real_current_minutes == 11 for drop in campaign.drops)
    assert all(drop.extra_current_minutes == 0 for drop in campaign.drops)
    assert campaign.timed_drops[source_id].progress_observation == {
        "minutes": 11, "source": "websocket", "received_at": RECEIVED_AT.isoformat(),
    }
    # Sibling inference changes the counter, not the sibling's last direct reading.
    assert campaign.timed_drops[other_id].progress_observation == original_other
    twitch.request_games_update.assert_not_called()


@pytest.mark.asyncio
async def test_fresh_downward_poll_recovers_cap_without_confirming_sibling_inference(mining_state):
    twitch, channel, campaign = mining_state
    target, sibling = campaign.drops
    target.extra_current_minutes = MAX_EXTRA_MINUTES
    original_sibling = sibling.progress_observation

    async def no_observation(request):
        await asyncio.sleep(0)

    with patch("src.models.drop.datetime", wraps=datetime) as clock:
        clock.now.return_value = RECEIVED_AT
        assert await poll(twitch, no_observation, returned_minutes=9) == [20]

    assert all(drop.real_current_minutes == 9 for drop in campaign.drops)
    assert all(drop.extra_current_minutes == 0 for drop in campaign.drops)
    assert target.can_earn(channel)
    assert target.progress_observation == {
        "minutes": 9, "source": "current_drop", "received_at": RECEIVED_AT.isoformat(),
    }
    assert sibling.observed_minutes == 10
    assert sibling.progress_observation == original_sibling
    twitch.request_games_update.assert_called_once_with()


@pytest.mark.asyncio
async def test_unchanged_websocket_reading_recovers_cap_and_refreshes_cached_metadata(mining_state):
    twitch, channel, campaign = mining_state
    target, sibling = campaign.drops
    target.extra_current_minutes = MAX_EXTRA_MINUTES
    original_sibling = sibling.progress_observation
    broadcaster = SimpleNamespace(emit=AsyncMock())
    inventory = InventoryManager(broadcaster, MagicMock())
    twitch.gui.inv = inventory
    await inventory.add_campaign(campaign)

    async def observe(request):
        await observe_websocket(twitch, target.id, 10)

    with patch("src.models.drop.datetime", wraps=datetime) as clock:
        clock.now.return_value = RECEIVED_AT
        assert await poll(twitch, observe, returned_minutes=9) == [20]
        expected = {
            "minutes": 10, "source": "websocket", "received_at": RECEIVED_AT.isoformat(),
        }
        cached = inventory.get_campaigns()[0]["drops"]
        assert cached[0]["current_minutes"] == 10
        assert cached[0]["progress_observation"] == expected
        assert cached[1]["progress_observation"] == original_sibling
        # A second same-value reading publishes its receipt without another recovery.
        clock.now.return_value = RECEIVED_AT + timedelta(minutes=1)
        await observe_websocket(twitch, target.id, 10)

    await asyncio.sleep(0)
    assert target.can_earn(channel)
    assert target.extra_current_minutes == 0
    assert target.progress_observation["received_at"] != expected["received_at"]
    assert inventory.get_campaigns()[0]["drops"][0]["progress_observation"] == target.progress_observation
    assert sibling.progress_observation == original_sibling
    twitch.request_games_update.assert_called_once_with()
    updates = [call.args[1] for call in broadcaster.emit.await_args_list if call.args[0] == "drop_update"]
    assert updates[-1]["drop"]["progress_observation"] == target.progress_observation


@pytest.mark.asyncio
async def test_fallback_batch_reaches_all_rewards_without_refreshing_direct_readings(mining_state):
    twitch, channel, campaign = mining_state
    twitch.change_state = MagicMock()
    before = [drop.progress_observation for drop in campaign.drops]
    for drop in campaign.drops:
        drop.extra_current_minutes = MAX_EXTRA_MINUTES - 1

    async def no_observation(request):
        await asyncio.sleep(0)

    assert await poll(twitch, no_observation, response="null") == [20]

    assert all(drop.extra_current_minutes == MAX_EXTRA_MINUTES for drop in campaign.drops)
    assert [drop.progress_observation for drop in campaign.drops] == before
    assert all(drop.observed_minutes == 10 for drop in campaign.drops)
    twitch.change_state.assert_called_once_with(State.CHANNEL_SWITCH)


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", ["ignored", "claimed", "expired", "prerequisite"])
async def test_blocked_websocket_reading_does_not_refresh_provenance(mining_state, blocker):
    twitch, channel, campaign = mining_state
    target, sibling = campaign.drops
    original = [drop.progress_observation for drop in campaign.drops]
    if blocker == "ignored":
        twitch.settings.drop_name_blacklist = [target.name]
    elif blocker == "claimed":
        target.is_claimed = True
    elif blocker == "expired":
        target.ends_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif blocker == "prerequisite":
        target.precondition_drops = [sibling.id]
    assert not target.can_reconcile_progress(channel)

    await observe_websocket(twitch, target.id, 12)

    assert [drop.real_current_minutes for drop in campaign.drops] == [10, 10]
    assert [drop.progress_observation for drop in campaign.drops] == original
    twitch.request_games_update.assert_not_called()
