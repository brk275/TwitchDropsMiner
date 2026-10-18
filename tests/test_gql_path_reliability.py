"""Checked server-error paths retain ordinary request recovery without fake claims."""

import asyncio
from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from tests.test_current_drop_responses import counts, response, run_watch
from tests.test_current_drop_responses import mining as mining
from tests.test_gql_reply_validation import gql as gql
from tests.test_gql_server_error_paths import server_error


UNREACHABLE = [
    {"errors": [{"message": "server error"}]},
    server_error({"currentUser": None}, ["currentUser", "dropCurrentSession"]),
    server_error({}, []),
    server_error({"items": ["untouched"]}, ["items", -1]),
    server_error({"items": ["untouched"]}, ["items", True]),
    server_error({}, [[]]),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", UNREACHABLE)
async def test_unreachable_path_falls_back_once_then_real_poll_corrects(gql, mining, monkeypatch, payload):
    client, read, sleep = gql
    _, campaign, _ = mining
    revision = campaign.progress_revision
    payload = deepcopy(payload)
    before = deepcopy(payload)

    async def reply(*args):
        if read.await_count == 1:
            return payload
        assert counts(campaign) == [(10, 3), (10, 3)]
        assert campaign.progress_revision == revision
        return response({"dropID": "drop", "currentMinutesWatched": 9})

    async def cleanup():
        task, client._qgl_limiter._reset_task = client._qgl_limiter._reset_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    read.side_effect = reply
    await run_watch(mining, monkeypatch, payload, query=client.request, cleanup=cleanup)
    assert read.await_count == 2
    sleep.assert_not_awaited()
    assert payload == before
    assert counts(campaign) == [(9, 0), (9, 0)]
    assert campaign.progress_revision == revision + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", UNREACHABLE)
async def test_unreachable_claim_path_keeps_counters_revision_and_history(gql, mining, payload):
    client, read, sleep = gql
    twitch, campaign, _ = mining
    twitch.request_games_update = MagicMock()
    before, revision = counts(campaign), campaign.progress_revision
    payload = deepcopy(payload)
    saved = deepcopy(payload)
    read.return_value = payload
    twitch.gql_request = client.request
    assert await campaign.timed_drops["drop"].claim() is False
    assert not campaign.timed_drops["drop"].is_claimed
    assert counts(campaign) == before
    assert campaign.progress_revision == revision
    assert payload == saved
    read.assert_awaited_once()
    sleep.assert_not_awaited()
    twitch.drop_history.record.assert_not_called()
    twitch.request_games_update.assert_not_called()
