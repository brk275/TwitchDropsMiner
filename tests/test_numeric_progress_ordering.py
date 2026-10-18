"""Rejected websocket minutes must not supersede a pending progress request."""

import logging

import pytest

from src.services.message_handlers import MessageHandlerService
from tests.test_progress_reading_types import INVALID
from tests.test_stale_progress import mining_state as mining_state
from tests.test_stale_progress import poll


def snapshot(campaign):
    return [(drop.real_current_minutes, drop.extra_current_minutes,
             drop.is_claimed, drop.claim_id) for drop in campaign.drops]


@pytest.mark.parametrize("minutes", INVALID)
def test_rejected_minutes_preserve_revision_and_model_events(mining_state, minutes):
    twitch, _, campaign = mining_state
    drop, sibling = campaign.drops
    drop.extra_current_minutes, sibling.extra_current_minutes = 1, 2
    revision, before = campaign.progress_revision, snapshot(campaign)

    drop.update_minutes(minutes)

    assert campaign.progress_revision == revision
    assert snapshot(campaign) == before
    twitch.gui.inv.update_drop.assert_not_called()
    twitch.request_games_update.assert_not_called()
    twitch.drop_history.record.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", INVALID)
@pytest.mark.parametrize("reply", ["session", "null", "error", "timeout"])
async def test_bad_websocket_value_cannot_retire_pending_poll(mining_state, minutes, reply, caplog):
    twitch, _, campaign = mining_state
    revision, before = campaign.progress_revision, snapshot(campaign)

    async def during_request(request):
        await MessageHandlerService(twitch).process_drops(42, {"type": "drop-progress", "data": {
            "drop_id": "drop", "current_progress_min": minutes, "required_progress_min": 60,
        }})
        assert campaign.progress_revision == revision
        assert snapshot(campaign) == before

    assert await poll(twitch, during_request, returned_minutes=9, response=reply) == [20]
    assert [drop.real_current_minutes for drop in campaign.drops] == [9 if reply == "session" else 10] * 2
    assert [drop.extra_current_minutes for drop in campaign.drops] == [0 if reply == "session" else 1] * 2
    assert campaign.progress_revision == revision + int(reply == "session")
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [-1, 0, 10, 11, 61, 2**63])
@pytest.mark.parametrize("reply", ["session", "null", "error", "timeout"])
async def test_integer_websocket_reading_still_supersedes_pending_poll(mining_state, minutes, reply):
    twitch, _, campaign = mining_state
    revision = campaign.progress_revision
    for drop in campaign.drops:
        drop.extra_current_minutes = 1

    async def during_request(request):
        await MessageHandlerService(twitch).process_drops(42, {"type": "drop-progress", "data": {
            "drop_id": "drop", "current_progress_min": minutes, "required_progress_min": 60,
        }})

    assert await poll(twitch, during_request, returned_minutes=9, response=reply) == [20]
    assert [drop.real_current_minutes for drop in campaign.drops] == [min(60, max(0, minutes))] * 2
    assert [drop.extra_current_minutes for drop in campaign.drops] == [0, 0]
    assert campaign.progress_revision == revision + 1
