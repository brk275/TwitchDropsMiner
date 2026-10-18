"""HTTP body decoding failures are invalid GQL replies, not watch-task failures."""

import asyncio
import json
from collections import deque
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from src.api.gql_client import GQLClient
from src.config import GQL_OPERATIONS
from src.exceptions import ExitRequest, GQLInvalidResponse
from tests.test_current_drop_responses import counts, response, run_watch
from tests.test_current_drop_responses import mining as mining


BROKEN = [
    pytest.param(b'{"private-response-marker":', 'application/json', id='invalid-json'),
    pytest.param(b'<html>private-response-marker</html>', 'text/html', id='html'),
    pytest.param(b'{"data":{}}', 'text/plain', id='json-wrong-mime'),
    pytest.param(b'{"data":"\xff"}', 'application/json', id='invalid-utf8'),
]


@pytest_asyncio.fixture
async def peer():
    rows, requests = deque(), []

    async def handle(request):
        requests.append(await request.json())
        body, content_type, *status = rows.popleft()
        return web.Response(body=body, headers={'Content-Type': content_type},
                            status=status[0] if status else 200)

    app = web.Application()
    app.router.add_post('/gql', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    session = aiohttp.ClientSession()

    class LocalHTTP:
        @asynccontextmanager
        async def request(self, method, url, **kwargs):
            assert method == 'POST' and url == 'https://gql.twitch.tv/gql'
            async with session.request(method, f'http://127.0.0.1:{port}/gql', **kwargs) as reply:
                yield reply

    auth = SimpleNamespace(browser_active=False, headers=MagicMock(return_value={}))
    auth.validate = AsyncMock(return_value=auth)
    client = GQLClient(LocalHTTP(), auth, SimpleNamespace(USER_AGENT='test'))

    async def cleanup():
        await session.close()
        await runner.cleanup()
        task, client._qgl_limiter._reset_task = client._qgl_limiter._reset_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert client._qgl_limiter.concurrent == 0

    try:
        yield SimpleNamespace(client=client, rows=rows, requests=requests, cleanup=cleanup, auth=auth)
    finally:
        await cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize('body,content_type', BROKEN)
@pytest.mark.parametrize('mutation', [False, True])
async def test_bad_http_body_fails_once_with_fixed_error_then_next_request_recovers(peer, body, content_type, mutation):
    operation = (GQL_OPERATIONS['ClaimDrop'].with_variables({'input': {'dropInstanceID': 'test'}})
                 if mutation else GQL_OPERATIONS['Inventory'])
    good = {'data': {'currentUser': {'id': 'test'}}}
    peer.rows.extend([(body, content_type), (json.dumps(good).encode(), 'application/json')])

    with pytest.raises(GQLInvalidResponse) as caught:
        await peer.client.request(operation)

    assert type(caught.value) is GQLInvalidResponse
    assert str(caught.value) == 'Invalid GraphQL response container or error metadata'
    assert caught.value.__suppress_context__
    assert peer.requests == [operation]
    assert len(peer.rows) == 1
    assert peer.client._qgl_limiter.concurrent == 0
    assert await peer.client.request(GQL_OPERATIONS['Inventory']) == good
    assert len(peer.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('body,content_type', BROKEN)
async def test_watch_survives_bad_http_body_without_estimate_then_reconciles(peer, mining, monkeypatch, caplog, body, content_type):
    good = response({'dropID': 'drop', 'currentMinutesWatched': 9})
    peer.rows.extend([(body, content_type), (json.dumps(good).encode(), 'application/json')])
    before = mining[1].progress_revision

    snapshots = await run_watch(mining, monkeypatch, None,
                                query=peer.client.request, cleanup=peer.cleanup)

    assert snapshots == [[(10, 2), (10, 2)], [(10, 2), (10, 2)]]
    assert counts(mining[1]) == [(9, 0), (9, 0)]
    assert mining[1].progress_revision == before + 1
    assert len(peer.requests) == 2 and not peer.rows
    assert not any(record.levelname == 'ERROR' for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize('content_type', ['application/json', 'application/graphql-response+json'])
@pytest.mark.parametrize('batch', [False, True])
async def test_valid_json_http_replies_preserve_single_and_batch_contract(peer, content_type, batch):
    data = {'data': {'currentUser': {'id': 'test'}}}
    payload = [data] if batch else data
    operation = [GQL_OPERATIONS['Inventory']] if batch else GQL_OPERATIONS['Inventory']
    peer.rows.append((json.dumps(payload).encode(), content_type))

    assert await peer.client.request(operation) == payload
    assert peer.requests == [operation]


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [ValueError('test'), RuntimeError('test'), ExitRequest(), asyncio.CancelledError()])
async def test_authentication_failures_are_not_body_decode_errors(peer, failure):
    peer.auth.validate.side_effect = failure
    with pytest.raises(type(failure)) as caught:
        await peer.client.request(GQL_OPERATIONS['Inventory'])
    assert caught.value is failure
    assert peer.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize('body,content_type', BROKEN)
@pytest.mark.parametrize('status', [401, 403])
async def test_http_authentication_rejection_preserves_original_decoding_failure(peer, body, content_type, status):
    expected = (aiohttp.ContentTypeError if content_type != 'application/json'
                else UnicodeDecodeError if b'\xff' in body else json.JSONDecodeError)
    peer.rows.append((body, content_type, status))
    with pytest.raises(expected):
        await peer.client.request(GQL_OPERATIONS['Inventory'])
    assert len(peer.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('body,content_type', BROKEN)
async def test_ambiguous_claim_body_remains_unconfirmed_without_replay(peer, mining, body, content_type, caplog):
    twitch, campaign, _ = mining
    before, revision = counts(campaign), campaign.progress_revision
    twitch.gql_request = peer.client.request
    twitch.request_games_update = MagicMock()
    peer.rows.extend([(body, content_type),
                      (b'{"data":{"claimDropRewards":{"status":"ELIGIBLE_FOR_ALL"}}}', 'application/json')])

    assert await campaign.timed_drops['drop'].claim() is False

    assert len(peer.requests) == len(peer.rows) == 1
    assert not any(drop.is_claimed for drop in campaign.drops)
    assert counts(campaign) == before and campaign.progress_revision == revision
    twitch.drop_history.record.assert_not_called()
    twitch.request_games_update.assert_not_called()
    assert 'private-response-marker' not in caplog.text
