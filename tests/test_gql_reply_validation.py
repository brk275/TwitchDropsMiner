"""Malformed GraphQL envelopes must not crash or manufacture watch progress."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from src.api.gql_client import GQLClient
from src.auth.imported_session import ImportedSession
from src.auth.session_bundle import SessionError
from src.config import GQL_OPERATIONS
from src.exceptions import GQLException, GQLInvalidResponse
from tests.test_current_drop_responses import counts, response, run_watch
from tests.test_current_drop_responses import mining as mining
from tests.test_imported_session_retry import running_miner


@pytest_asyncio.fixture(params=[False, True], ids=["http", "browser"])
async def gql(request, monkeypatch):
    tasks = asyncio.all_tasks()
    read, sleep = AsyncMock(), AsyncMock()

    class Reply:
        async def __aenter__(self):
            return SimpleNamespace(json=read)

        async def __aexit__(self, *args):
            return False

    auth = SimpleNamespace(browser_active=request.param,
                           _twitch=SimpleNamespace(_browser=SimpleNamespace(gql=read)),
                           headers=MagicMock(return_value={}))
    auth.validate = AsyncMock(return_value=auth)
    client = GQLClient(SimpleNamespace(request=MagicMock(return_value=Reply())),
                       auth, SimpleNamespace(USER_AGENT="test"))
    monkeypatch.setattr("src.api.gql_client.ExponentialBackoff", lambda **_: [0, 0])
    monkeypatch.setattr("src.api.gql_client.asyncio", SimpleNamespace(sleep=sleep))
    try:
        yield client, read, sleep
    finally:
        limiter = client._qgl_limiter
        task, limiter._reset_task = limiter._reset_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert limiter.concurrent == 0
        assert not {task for task in asyncio.all_tasks() - tasks
                    if task is not asyncio.current_task() and not task.done()}


BAD_ERRORS = [{"errors": value} for value in (
    None, False, 0, "private-error", {}, [], [None], [False], [7], [[]], [{}],
    [{"message": None}], [{"message": []}], [{"message": {}}], [{"message": True}],
    [{"message": ""}], [{"message": "service unavailable"}, None],
)] + [{"error": value, "message": "private-message"} for value in (None, [], "")] + [
    {"error": "private-error"}, {"error": "private-error", "message": []},
]
BAD = [None, False, 7, "private-reply", [], [None], *BAD_ERRORS]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", BAD)
async def test_bad_single_reply_fails_once_with_fixed_error(gql, payload):
    client, read, sleep = gql
    before = deepcopy(payload)
    read.return_value = payload
    with pytest.raises(GQLInvalidResponse) as caught:
        await client.request(GQL_OPERATIONS["Inventory"])
    assert str(caught.value) == "Invalid GraphQL response container or error metadata"
    assert payload == before
    read.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, {}, [], [None, {}], [{}, 7], [{}], [{}, {}, {}]])
async def test_batch_shape_and_count_are_validated(gql, payload):
    client, read, sleep = gql
    read.return_value = payload
    with pytest.raises(GQLInvalidResponse):
        await client.request([GQL_OPERATIONS["Inventory"], GQL_OPERATIONS["Campaigns"]])
    read.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_first", [False, True])
@pytest.mark.parametrize("action", ["normalize", "retry"])
async def test_all_rows_validate_before_normalization_or_retry(gql, bad_first, action):
    client, read, sleep = gql
    good = ({"data": {"value": "untouched"}, "errors": [{"message": "server error", "path": ["value"]}]}
            if action == "normalize" else {"errors": [{"message": "service unavailable"}]})
    bad = {"errors": [None]}
    payload = [bad, good] if bad_first else [good, bad]
    before = deepcopy(payload)
    read.return_value = payload
    with pytest.raises(GQLInvalidResponse):
        await client.request([GQL_OPERATIONS["Inventory"], GQL_OPERATIONS["Campaigns"]])
    assert payload == before
    read.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"data": None}, {"data": ["operation-owned"]}, {"extensions": None}])
@pytest.mark.parametrize("batch", [False, True])
async def test_valid_container_preserves_operation_data(gql, payload, batch):
    client, read, sleep = gql
    read.return_value = [payload] if batch else payload
    query = [GQL_OPERATIONS["Inventory"]] if batch else GQL_OPERATIONS["Inventory"]
    assert await client.request(query) is read.return_value
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_batch_control(gql):
    client, read, sleep = gql
    read.return_value = []
    assert await client.request([]) == []
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["service error", "PersistedQueryNotFound"])
@pytest.mark.parametrize("extensions", [None, [], {}, {"operationName": []}, {"operationName": ""}])
async def test_existing_service_retry_uses_safe_optional_metadata(gql, message, extensions):
    client, read, sleep = gql
    success = {"data": {"value": 1}}
    read.side_effect = [{"errors": [{"message": message}], "extensions": extensions}, success]
    assert await client.request(GQL_OPERATIONS["Inventory"]) is success
    assert read.await_count == 2
    sleep.assert_awaited_once_with(5)


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["service error", "PersistedQueryNotFound"])
@pytest.mark.parametrize("extensions", [None, [], {}, {"operationName": "ClaimDrop"}])
async def test_optional_retry_metadata_never_authorizes_mutation_replay(gql, message, extensions):
    client, read, sleep = gql
    read.side_effect = [{"errors": [{"message": message}], "extensions": extensions},
                        {"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}}]
    operation = GQL_OPERATIONS["ClaimDrop"].with_variables({"input": {"dropInstanceID": "test-only"}})
    with pytest.raises(GQLException, match="^Automatic retry is unsafe for this GraphQL request$"):
        await client.request(operation)
    read.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"errors": [{"message": "ordinary request error"}]},
    {"error": "ordinary", "message": "request error"},
])
async def test_well_formed_failures_keep_existing_exception(gql, payload):
    client, read, sleep = gql
    read.return_value = payload
    with pytest.raises(GQLException) as caught:
        await client.request(GQL_OPERATIONS["Inventory"])
    assert type(caught.value) is GQLException
    read.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [asyncio.CancelledError(), TimeoutError("test"), ValueError("test")])
async def test_unexpected_transport_failures_and_cancellation_propagate(gql, error):
    client, read, sleep = gql
    read.side_effect = error
    with pytest.raises(type(error)) as caught:
        await client.request(GQL_OPERATIONS["Inventory"])
    assert caught.value is error
    read.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["RESPONSE", "REQUEST", "AUTH", "STALE"])
@pytest.mark.parametrize("initial_auth", [False, True])
async def test_only_imported_dispatch_request_and_response_are_reclassified(gql, code, initial_auth):
    client, read, sleep = gql
    error = SessionError(code)
    if initial_auth:
        client._auth_state.validate.side_effect = error
    else:
        read.side_effect = error
    translated = client._auth_state.browser_active and not initial_auth and code in ("RESPONSE", "REQUEST")
    expected = (GQLInvalidResponse if code == "RESPONSE" else GQLException) if translated else SessionError
    with pytest.raises(expected) as caught:
        await client.request(GQL_OPERATIONS["Inventory"])
    if not translated:
        assert caught.value is error
    else:
        assert type(caught.value) is expected
        assert caught.value.__suppress_context__
    sleep.assert_not_awaited()


@pytest.mark.parametrize("message", [None, False, 1, [], {}, ["failed integrity check"]])
def test_auth_precheck_refuses_malformed_messages(message):
    assert ImportedSession._auth_rejection({"errors": [{"message": message}]}) is False
    assert ImportedSession._auth_rejection({"errors": [{"message": "invalid oauth token"}, {"message": message}]}) is False


@pytest.mark.parametrize("message", ["invalid oauth token", "failed integrity check"])
def test_definitive_auth_rejection_still_requires_no_data_or_path(message):
    row = {"errors": [{"message": message}]}
    assert ImportedSession._auth_rejection(row) is True
    assert ImportedSession._auth_rejection({**row, "data": None}) is False
    assert ImportedSession._auth_rejection({"errors": [{"message": message, "path": None}]}) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, [], {"errors": None}, {"errors": [{"message": []}]}])
async def test_watch_skips_malformed_reply_then_recovers(gql, mining, monkeypatch, payload):
    client, read, _ = gql
    twitch, campaign, _ = mining
    initial, revision = counts(campaign), campaign.progress_revision

    async def reply(*args):
        if read.await_count == 1:
            return payload
        assert counts(campaign) == initial
        assert campaign.progress_revision == revision
        return response({"dropID": "drop", "currentMinutesWatched": 9})

    read.side_effect = reply

    async def cleanup():
        task, client._qgl_limiter._reset_task = client._qgl_limiter._reset_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    await run_watch(mining, monkeypatch, payload, query=client.request, cleanup=cleanup)
    assert counts(campaign) == [(9, 0), (9, 0)]
    assert campaign.progress_revision == revision + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, {"errors": None}, {"errors": [{"message": []}]}, {"errors": [{"message": {}}]}])
async def test_actual_imported_reply_does_not_reject_auth_or_confirm_claim(mining, tmp_path, monkeypatch, payload):
    twitch, campaign, _ = mining
    initial, revision = counts(campaign), campaign.progress_revision
    async with running_miner(tmp_path, monkeypatch) as (client, peer, _):
        peer.responses.extend([payload, {"data": {"mustNotRun": True}}])
        twitch.gql_request = client.gql_request
        assert await campaign.timed_drops["drop"].claim() is False
        assert len(peer.calls) == len(peer.responses) == 1
        assert client._browser.status()["state"] == "ready"
    assert counts(campaign) == initial
    assert campaign.progress_revision == revision
    twitch.drop_history.record.assert_not_called()
