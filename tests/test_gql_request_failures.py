"""Imported request failures use GQL recovery without adding transport retries."""

import asyncio
from unittest.mock import MagicMock

import pytest
from aiohttp import ClientConnectionError

from src.config import GQL_OPERATIONS
from src.exceptions import GQLException
from tests.test_current_drop_responses import counts, response, run_watch
from tests.test_current_drop_responses import mining as mining
from tests.test_imported_session_retry import immediate_delays, running_miner


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,sends,delays", [
    (GQL_OPERATIONS["CurrentDrop"].with_variables({"channelID": "123"}), 3, [1, 2]),
    (GQL_OPERATIONS["ClaimDrop"].with_variables({"input": {"dropInstanceID": "test"}}), 1, []),
    (GQL_OPERATIONS["NotificationsDelete"], 1, []),
])
async def test_exhausted_dispatch_is_recoverable_without_outer_replay(
    tmp_path, monkeypatch, operation, sends, delays,
):
    async with running_miner(tmp_path, monkeypatch) as (client, peer, _clock):
        success = {"data": {"currentUser": {"id": "42"}}}
        peer.responses.extend([503] * sends + [success])
        waited = immediate_delays(client._browser, monkeypatch)
        with pytest.raises(GQLException) as caught:
            await client.gql_request(operation)
        assert type(caught.value) is GQLException
        assert str(caught.value) == "Authenticated GraphQL request failed (SESSION_REQUEST)"
        assert caught.value.__suppress_context__
        assert peer.calls == [operation] * sends
        assert waited == delays
        assert list(peer.responses) == [success]
        assert client._browser.status()["state"] == "ready"
        assert client._browser.status()["generation"] == 1
        assert client._gql_client._qgl_limiter.concurrent == 0
        assert await client.gql_request(GQL_OPERATIONS["Inventory"]) == success


@pytest.mark.asyncio
async def test_real_watch_recovers_after_exhausted_imported_transport(mining, tmp_path, monkeypatch):
    _, campaign, _ = mining
    revision = campaign.progress_revision
    async with running_miner(tmp_path, monkeypatch) as (client, peer, _):
        peer.responses.extend([503, 502, 504, response({"dropID": "drop", "currentMinutesWatched": 9})])
        waited = immediate_delays(client._browser, monkeypatch)
        polls = 0

        async def query(operation):
            nonlocal polls
            polls += 1
            if polls == 2:
                assert counts(campaign) == [(10, 3), (10, 3)]
                assert campaign.progress_revision == revision
            return await client.gql_request(operation)

        async def cleanup():
            task, client._gql_client._qgl_limiter._reset_task = client._gql_client._qgl_limiter._reset_task, None
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        await run_watch(mining, monkeypatch, None, query=query, cleanup=cleanup)
        assert polls == 2
        assert len(peer.calls) == 4
        assert waited == [1, 2]
        assert counts(campaign) == [(9, 0), (9, 0)]
        assert campaign.progress_revision == revision + 1


@pytest.mark.asyncio
async def test_real_ambiguous_claim_remains_unconfirmed(mining, tmp_path, monkeypatch):
    twitch, campaign, _ = mining
    twitch.request_games_update = MagicMock()
    before, revision = counts(campaign), campaign.progress_revision
    async with running_miner(tmp_path, monkeypatch) as (client, peer, _):
        peer.responses.extend([503, {"data": {"mustNotRun": True}}])
        waited = immediate_delays(client._browser, monkeypatch)
        twitch.gql_request = client.gql_request
        assert await campaign.timed_drops["drop"].claim() is False
        assert len(peer.calls) == len(peer.responses) == 1
        assert waited == []
        assert client._browser.status()["state"] == "ready"
    assert not campaign.timed_drops["drop"].is_claimed
    assert counts(campaign) == before
    assert campaign.progress_revision == revision
    twitch.drop_history.record.assert_not_called()
    twitch.request_games_update.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError, ClientConnectionError])
@pytest.mark.parametrize("mutation", [False, True])
async def test_transport_exception_exhaustion_is_bounded_and_recovers(tmp_path, monkeypatch, failure, mutation):
    async with running_miner(tmp_path, monkeypatch) as (client, peer, _):
        waited = immediate_delays(client._browser, monkeypatch)
        http = client._browser._transport._http
        request = http.request
        attempts = []

        def unavailable(*args, **kwargs):
            attempts.append(kwargs["json"])
            raise failure("private transport detail")

        monkeypatch.setattr(http, "request", unavailable)
        operation = (GQL_OPERATIONS["NotificationsDelete"] if mutation else GQL_OPERATIONS["Inventory"])
        with pytest.raises(GQLException) as caught:
            await client.gql_request(operation)
        assert type(caught.value) is GQLException
        assert str(caught.value) == "Authenticated GraphQL request failed (SESSION_REQUEST)"
        assert caught.value.__suppress_context__
        assert attempts == [operation] * (1 if mutation else 3)
        assert waited == ([] if mutation else [1, 2])
        assert client._browser.status()["state"] == "ready"
        assert peer.calls == []
        monkeypatch.setattr(http, "request", request)
        peer.responses.append({"data": {"recovered": True}})
        assert await client.gql_request(GQL_OPERATIONS["Inventory"]) == {"data": {"recovered": True}}
