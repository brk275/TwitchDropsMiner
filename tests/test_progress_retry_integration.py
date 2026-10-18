"""Authenticated request failures must not terminate progress or invent claims."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from src.auth.session_bundle import SessionError
from src.config import GQL_OPERATIONS
from src.exceptions import ExitRequest, GQLException, GQLInvalidResponse
from src.services.message_handlers import MessageHandlerService
from tests.test_current_drop_responses import response
from tests.test_gql_retry_safety import MESSAGES, failure
from tests.test_gql_retry_safety import gql as gql
from tests.test_gql_server_error_paths import server_error
from tests.test_imported_session_retry import immediate_delays, running_miner
from tests.test_progress_response_integration import mining as mining
from tests.test_progress_response_integration import run_poll, snapshot


async def drain_limiter(client):
    limiter = client._qgl_limiter
    task, limiter._reset_task = limiter._reset_task, None
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def run_client(twitch, gql, monkeypatch):
    client, _, _, operations = gql
    channel = twitch.watching_channel.get_with_default(None)

    async def cleanup():
        await drain_limiter(client)

    await run_poll(twitch, monkeypatch, (), query=client.request, cleanup=cleanup)
    operation = GQL_OPERATIONS["CurrentDrop"].with_variables({"channelID": str(channel.id)})
    assert operations == [operation, operation]
    assert client._auth_state.validate.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("exhausted", [False, True])
async def test_imported_http_failures_leave_watch_loop_running_for_the_next_poll(
    mining, tmp_path, monkeypatch, exhausted,
):
    twitch, channel, campaign = mining
    before, revision = snapshot(campaign), campaign.progress_revision
    async with running_miner(tmp_path, monkeypatch) as (client, peer, _):
        delays = immediate_delays(client._browser, monkeypatch)
        fresh = response({"dropID": "drop", "currentMinutesWatched": 9})
        peer.responses.extend([503, 502, 504, fresh] if exhausted else [503, fresh, fresh])

        count = 0

        async def query(operation):
            nonlocal count
            count += 1
            if count == 2:
                if exhausted:
                    assert campaign.progress_revision == revision
                    assert [drop.real_current_minutes for drop in campaign.drops] == [10, 10]
                    assert [drop.extra_current_minutes for drop in campaign.drops] == [3, 3]
                    assert [drop.progress_observation for drop in campaign.drops] == [item[-1] for item in before]
                else:
                    assert campaign.progress_revision == revision + 1
                    assert [drop.extra_current_minutes for drop in campaign.drops] == [0, 0]
            return await client.gql_request(operation)

        async def cleanup():
            await drain_limiter(client._gql_client)

        await run_poll(twitch, monkeypatch, (), query=query, cleanup=cleanup, until=90)
        operation = GQL_OPERATIONS["CurrentDrop"].with_variables({"channelID": str(channel.id)})
        assert peer.calls == [operation] * (4 if exhausted else 3)
        assert delays == ([1, 2] if exhausted else [1])
        assert not peer.responses
        assert client._browser.status()["state"] == "ready"
        assert client._browser.status()["generation"] == 1
    assert campaign.progress_revision == revision + (1 if exhausted else 2)
    assert [drop.real_current_minutes for drop in campaign.drops] == [9, 9]
    assert [drop.extra_current_minutes for drop in campaign.drops] == [0, 0]
    assert campaign.timed_drops["drop"].progress_observation["source"] == "current_drop"
    assert campaign.timed_drops["sibling"].progress_observation == before[1][-1]
    assert campaign.bump_minutes.call_count == int(exhausted)
    twitch.drop_history.record.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_actual_claim_path_does_not_repeat_an_ambiguous_authenticated_write(
    mining, tmp_path, monkeypatch, failed,
):
    twitch, _, campaign = mining
    drop = campaign.timed_drops["drop"]
    before, revision = snapshot(campaign), campaign.progress_revision
    async with running_miner(tmp_path, monkeypatch) as (client, peer, _):
        delays = immediate_delays(client._browser, monkeypatch)
        success = {"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}}
        peer.responses.extend([503, success] if failed else [success])
        twitch.gql_request = client.gql_request
        assert await drop.claim() is not failed
        operation = GQL_OPERATIONS["ClaimDrop"].with_variables({"input": {"dropInstanceID": drop.claim_id}})
        assert peer.calls == [operation]
        assert len(peer.responses) == int(failed)
        assert delays == []
        assert client._browser.status()["state"] == "ready"
    if failed:
        assert snapshot(campaign) == before
        assert campaign.progress_revision == revision
        twitch.drop_history.record.assert_not_called()
        twitch.request_games_update.assert_not_called()
        twitch.gui.broadcast_wanted_items_now.assert_not_awaited()
    else:
        assert drop.is_claimed and drop.real_current_minutes == 60 and drop.extra_current_minutes == 0
        assert drop.progress_observation == before[0][-1]
        assert snapshot(campaign)[1] == before[1]
        assert campaign.progress_revision == revision + 1
        twitch.drop_history.record.assert_called_once_with(drop, campaign)
        twitch.request_games_update.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("message", MESSAGES)
async def test_known_current_drop_read_can_retry_and_record_only_direct_target_evidence(mining, gql, monkeypatch, message):
    twitch, _, campaign = mining
    _, read, sleep, _ = gql
    before, revision = snapshot(campaign), campaign.progress_revision
    read.side_effect = [failure(message), response({"dropID": "drop", "currentMinutesWatched": 9})]
    await run_client(twitch, gql, monkeypatch)
    sleep.assert_awaited_once_with(5 if message in MESSAGES[:2] else 0)
    assert campaign.progress_revision == revision + 1
    assert [drop.real_current_minutes for drop in campaign.drops] == [9, 9]
    assert [drop.extra_current_minutes for drop in campaign.drops] == [0, 0]
    assert campaign.timed_drops["drop"].progress_observation["source"] == "current_drop"
    assert campaign.timed_drops["sibling"].progress_observation == before[1][-1]
    campaign.bump_minutes.assert_not_called()
    twitch.drop_history.record.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["drop", "sibling", "claim_drop", "claim_sibling"])
@pytest.mark.parametrize("result", ["valid", "empty", "server_error"])
async def test_retry_wait_cannot_undo_newer_observations_or_claims(mining, gql, monkeypatch, transition, result):
    twitch, _, campaign = mining
    _, read, sleep, _ = gql
    before, revision = snapshot(campaign), campaign.progress_revision
    target = campaign.timed_drops[transition.removeprefix("claim_")]
    claimed = transition.startswith("claim_")
    target._claim = AsyncMock(return_value=True)

    async def during(_):
        if claimed:
            assert await target.claim()
        else:
            await MessageHandlerService(twitch).process_drops(42, {"type": "drop-progress", "data": {
                "drop_id": target.id, "current_progress_min": 10, "required_progress_min": 60,
            }})

    sleep.side_effect = during
    result = {
        "valid": response({"dropID": "drop", "currentMinutesWatched": 9}),
        "empty": response(None),
        "server_error": server_error({"currentUser": None}, ["currentUser", "dropCurrentSession"]),
    }[result]
    saved = deepcopy(result)
    read.side_effect = [failure("service unavailable"), result]
    await run_client(twitch, gql, monkeypatch)
    assert result == saved
    assert campaign.progress_revision == revision + 1
    if claimed:
        assert target.is_claimed and target.real_current_minutes == 60 and target.extra_current_minutes == 0
        assert target.progress_observation == before[0 if target.id == "drop" else 1][-1]
        other = campaign.timed_drops["sibling" if target.id == "drop" else "drop"]
        assert snapshot(campaign)[0 if other.id == "drop" else 1] == before[0 if other.id == "drop" else 1]
        twitch.drop_history.record.assert_called_once_with(target, campaign)
        twitch.request_games_update.assert_called_once_with()
    else:
        assert [drop.real_current_minutes for drop in campaign.drops] == [10, 10]
        assert [drop.extra_current_minutes for drop in campaign.drops] == [0, 0]
        assert target.progress_observation["source"] == "websocket"
        other = campaign.timed_drops["sibling" if target.id == "drop" else "drop"]
        assert other.progress_observation == before[0 if other.id == "drop" else 1][-1]
        twitch.drop_history.record.assert_not_called()
    campaign.bump_minutes.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("message", MESSAGES)
async def test_claim_service_error_returns_false_until_a_later_explicit_attempt(mining, gql, message):
    twitch, _, campaign = mining
    client, read, sleep, operations = gql
    drop = campaign.timed_drops["drop"]
    before, revision = snapshot(campaign), campaign.progress_revision
    success = {"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}}
    read.side_effect = [failure(message), success]
    twitch.gql_request = client.request
    operation = GQL_OPERATIONS["ClaimDrop"].with_variables({"input": {"dropInstanceID": drop.claim_id}})
    assert await drop.claim() is False
    assert operations == [operation]
    assert snapshot(campaign) == before
    assert campaign.progress_revision == revision
    twitch.drop_history.record.assert_not_called()
    twitch.request_games_update.assert_not_called()
    twitch.gui.broadcast_wanted_items_now.assert_not_awaited()
    sleep.assert_not_awaited()
    assert await drop.claim() is True
    assert await drop.claim() is True
    assert operations == [operation, operation]
    assert campaign.progress_revision == revision + 1
    assert drop.is_claimed and drop.real_current_minutes == 60 and drop.extra_current_minutes == 0
    assert drop.progress_observation == before[0][-1]
    assert snapshot(campaign)[1] == before[1]
    twitch.drop_history.record.assert_called_once_with(drop, campaign)
    twitch.request_games_update.assert_called_once_with()


SESSION_CODES = ("REQUEST", "AUTH", "STALE", "ACCOUNT_MISMATCH", "RESPONSE", "IDENTITY", "CATALOG", "FILE")


@pytest.mark.asyncio
@pytest.mark.parametrize("code", SESSION_CODES)
@pytest.mark.parametrize("in_auth", [False, True])
async def test_only_imported_dispatch_request_and_response_errors_become_fixed_gql_failures(gql, code, in_auth, caplog):
    client, read, sleep, operations = gql
    error = SessionError(code)
    error.args = ("private test-only upstream detail",)
    if in_auth:
        client._auth_state.validate.side_effect = error
    else:
        read.side_effect = error
    translated = client._auth_state.browser_active and not in_auth and code in ("REQUEST", "RESPONSE")
    error_type = GQLInvalidResponse if translated and code == "RESPONSE" else GQLException if translated else SessionError
    with pytest.raises(error_type) as caught:
        await client.request(GQL_OPERATIONS["Inventory"])
    if translated:
        assert str(caught.value) == (
            "Invalid GraphQL response container or error metadata" if code == "RESPONSE"
            else "Authenticated GraphQL request failed (SESSION_REQUEST)"
        )
        assert caught.value.__cause__ is None and caught.value.__suppress_context__
        assert "private" not in str(caught.value)
        assert not any("private" in record.getMessage() for record in caplog.records)
    else:
        assert caught.value is error
    assert len(operations) == int(not in_auth)
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [asyncio.CancelledError(), ExitRequest(), TimeoutError("test"), RuntimeError("test")])
async def test_non_session_failures_keep_their_identity_and_do_not_retry(gql, error):
    client, read, sleep, operations = gql
    read.side_effect = error
    with pytest.raises(type(error)) as caught:
        await client.request(GQL_OPERATIONS["Inventory"])
    assert caught.value is error
    assert len(operations) == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_cancellation_during_retry_drains_without_estimates(mining, gql, monkeypatch):
    twitch, _, campaign = mining
    client, read, sleep, operations = gql
    before, revision = snapshot(campaign), campaign.progress_revision
    entered, drained = asyncio.Event(), asyncio.Event()

    async def wait(_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    async def cleanup():
        await drain_limiter(client)

    sleep.side_effect = wait
    read.return_value = failure("service unavailable")
    task = asyncio.create_task(run_poll(twitch, monkeypatch, (), query=client.request, cleanup=cleanup))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.wait_for(task, 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert drained.is_set()
    assert len(operations) == 1
    assert snapshot(campaign) == before
    assert campaign.progress_revision == revision
    campaign.bump_minutes.assert_not_called()
    twitch.drop_history.record.assert_not_called()
