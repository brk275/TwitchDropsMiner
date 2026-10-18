"""Rejected minute types are not observations that supersede pending progress."""

import logging
from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from src.services.message_handlers import MessageHandlerService
from tests.test_deadline_channels import runtime as runtime
from tests.test_deadline_forecast import NOW
from tests.test_progress_reading_types import INVALID
from tests.test_stale_progress import mining_state as mining_state
from tests.test_stale_progress import poll


def snapshot(campaign):
    return [(drop.real_current_minutes, drop.extra_current_minutes, drop.is_claimed,
             drop.claim_id, drop.progress_observation) for drop in campaign.drops]


@pytest.mark.parametrize("minutes", INVALID)
def test_invalid_type_does_not_advance_revision_or_replace_provenance(mining_state, minutes):
    twitch, _, campaign = mining_state
    reported, sibling = campaign.drops
    reported.extra_current_minutes, sibling.extra_current_minutes = 1, 2
    revision, before = campaign.progress_revision, snapshot(campaign)
    reported.update_minutes(minutes, source="websocket")
    assert campaign.progress_revision == revision
    assert snapshot(campaign) == before
    twitch.gui.inv.update_drop.assert_not_called()
    twitch.request_games_update.assert_not_called()
    twitch.drop_history.record.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", INVALID)
@pytest.mark.parametrize("reply", ["session", "null", "error", "timeout"])
async def test_invalid_websocket_value_does_not_supersede_pending_poll(mining_state, minutes, reply, caplog):
    twitch, _, campaign = mining_state
    reported, sibling = campaign.drops
    observations = [drop.progress_observation for drop in campaign.drops]
    revision = campaign.progress_revision
    message = {"type": "drop-progress", "data": {
        "drop_id": reported.id, "current_progress_min": minutes, "required_progress_min": 60,
    }}
    before = deepcopy(message)

    async def during_request(request):
        await MessageHandlerService(twitch).process_drops(42, message)
        assert campaign.progress_revision == revision
        assert [drop.progress_observation for drop in campaign.drops] == observations
        assert [drop.real_current_minutes for drop in campaign.drops] == [10, 10]
        assert message == before

    assert await poll(twitch, during_request, returned_minutes=9, response=reply) == [20]
    if reply == "session":
        assert reported.real_current_minutes == sibling.real_current_minutes == 9
        assert reported.observed_minutes == 9
        assert reported.progress_observation["source"] == "current_drop"
        assert sibling.progress_observation == observations[1]
        assert campaign.progress_revision == revision + 1
    else:
        assert reported.real_current_minutes == sibling.real_current_minutes == 10
        assert [drop.progress_observation for drop in campaign.drops] == observations
        assert campaign.progress_revision == revision
    assert reported.extra_current_minutes == sibling.extra_current_minutes == (0 if reply == "session" else 1)
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [0, 10, 11])
@pytest.mark.parametrize("reply", ["session", "null", "error", "timeout"])
async def test_valid_even_unchanged_websocket_reading_still_supersedes_old_poll(mining_state, minutes, reply):
    twitch, _, campaign = mining_state
    reported, sibling = campaign.drops
    reported.extra_current_minutes = sibling.extra_current_minutes = 1
    sibling_observation = sibling.progress_observation
    revision = campaign.progress_revision

    async def during_request(request):
        await MessageHandlerService(twitch).process_drops(42, {"type": "drop-progress", "data": {
            "drop_id": reported.id, "current_progress_min": minutes, "required_progress_min": 60,
        }})

    assert await poll(twitch, during_request, returned_minutes=9, response=reply) == [20]
    assert reported.real_current_minutes == sibling.real_current_minutes == minutes
    assert reported.extra_current_minutes == sibling.extra_current_minutes == 0
    assert reported.observed_minutes == minutes and reported.progress_observation["source"] == "websocket"
    assert sibling.progress_observation == sibling_observation
    assert campaign.progress_revision == revision + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [-1, 61, 2**63])
@pytest.mark.parametrize("reply", ["session", "null", "error", "timeout"])
async def test_existing_integer_clamps_still_supersede_old_poll_without_faking_provenance(mining_state, minutes, reply):
    twitch, _, campaign = mining_state
    observations, revision = [drop.progress_observation for drop in campaign.drops], campaign.progress_revision

    async def during_request(request):
        await MessageHandlerService(twitch).process_drops(42, {"type": "drop-progress", "data": {
            "drop_id": "drop", "current_progress_min": minutes, "required_progress_min": 60,
        }})

    assert await poll(twitch, during_request, returned_minutes=9, response=reply) == [20]
    expected = min(60, max(0, minutes))
    assert [drop.real_current_minutes for drop in campaign.drops] == [expected, expected]
    assert [drop.extra_current_minutes for drop in campaign.drops] == [0, 0]
    assert [drop.progress_observation for drop in campaign.drops] == observations
    assert campaign.progress_revision == revision + 1


@pytest.mark.parametrize("minutes", [False, 1.5, float("nan"), None, "9", {}])
def test_rejected_value_keeps_context_current_but_later_real_reading_retires_it(runtime, minutes):
    twitch, campaign, _, guard, _ = runtime
    bound = guard.capture(twitch, now=NOW, horizon=NOW + timedelta(days=3),
                          enabled=True, include_channels=True).bound
    assert bound is not None
    drop = campaign.timed_drops["reward"]
    before = drop.progress_observation
    drop.update_minutes(minutes, source="websocket")
    assert drop.progress_observation == before
    assert guard.validate(bound, twitch, now=NOW, enabled=True) == "current"
    drop.update_minutes(9, source="websocket")
    assert drop.observed_minutes == 9
    assert guard.validate(bound, twitch, now=NOW, enabled=True) == "superseded"
    assert bound.inputs.baseline_complete is bound.channels.baseline_complete is False
    assert bound.channels.allocation_known is False


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [False, float("nan")])
async def test_claim_after_rejected_value_keeps_direct_observation_and_one_transition(mining_state, minutes):
    twitch, _, campaign = mining_state
    drop = campaign.timed_drops["drop"]
    observation, revision = drop.progress_observation, campaign.progress_revision
    drop.update_minutes(minutes, source="websocket")
    assert campaign.progress_revision == revision
    drop._claim = AsyncMock(return_value=True)
    assert await drop.claim()
    assert drop.is_claimed and drop.real_current_minutes == drop.required_minutes
    assert drop.progress_observation == observation
    assert campaign.progress_revision == revision + 1
    assert await drop.claim()
    assert campaign.progress_revision == revision + 1
    twitch.drop_history.record.assert_called_once_with(drop, campaign)
    twitch.request_games_update.assert_called_once_with()
