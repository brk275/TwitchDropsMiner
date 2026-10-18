"""Exercise reconciliation and stale-poll protection together."""

import asyncio

import pytest

from src.config import MAX_EXTRA_MINUTES
from tests.test_stale_progress import mining_state as mining_state
from tests.test_stale_progress import poll


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["session", "null", "error", "timeout"])
async def test_unchanged_observation_recovers_cap_without_accepting_superseded_poll(
    mining_state, response,
):
    twitch, channel, campaign = mining_state
    drop = campaign.timed_drops["drop"]
    drop.extra_current_minutes = MAX_EXTRA_MINUTES
    assert not drop.can_earn(channel)
    # An eligible sibling keeps the fallback campaign stable across the request.
    assert campaign.can_earn(channel)

    async def observe(request):
        drop.update_minutes(10)
        await asyncio.sleep(0)

    assert await poll(twitch, observe, response=response, returned_minutes=9) == [20]
    assert all(reward.real_current_minutes == 10 for reward in campaign.drops)
    assert all(reward.extra_current_minutes == 0 for reward in campaign.drops)
    assert drop.can_earn(channel)
    twitch.request_games_update.assert_called_once_with()


@pytest.mark.asyncio
async def test_fresh_poll_can_correct_and_recover_a_capped_reward(mining_state):
    twitch, channel, campaign = mining_state
    drop = campaign.timed_drops["drop"]
    drop.extra_current_minutes = MAX_EXTRA_MINUTES
    assert not drop.can_earn(channel)

    async def observe(request):
        await asyncio.sleep(0)

    assert await poll(twitch, observe, returned_minutes=9) == [20]
    assert all(reward.real_current_minutes == 9 for reward in campaign.drops)
    assert all(reward.extra_current_minutes == 0 for reward in campaign.drops)
    assert drop.can_earn(channel)
    twitch.request_games_update.assert_called_once_with()
