"""GraphQL service retries must not replay ambiguous mutations or mixed batches."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from src.api.gql_client import GQLClient
from src.api.gql_retry_policy import GQLRetryPolicy
from src.auth.imported_session import ImportedSession
from src.config import GQL_OPERATIONS
from src.exceptions import GQLException
from tests.test_imported_session_retry import running_miner


MESSAGES = ("service error", "PersistedQueryNotFound", "service timeout",
            "service unavailable", "context deadline exceeded")
MUTATIONS = ("ClaimDrop", "ClaimCommunityPoints", "NotificationsView", "NotificationsDelete")


def failure(message):
    return {"errors": [{"message": message}], "extensions": {"operationName": "TestOnly"}}


def mutation(name):
    variables = {
        "ClaimDrop": {"dropInstanceID": "test-claim"},
        "ClaimCommunityPoints": {"claimID": "test-points", "channelID": "123"},
        "NotificationsView": {},
        "NotificationsDelete": {"id": "test-notification"},
    }[name]
    return GQL_OPERATIONS[name].with_variables({"input": variables})


@pytest_asyncio.fixture(params=[False, True], ids=["http", "browser"])
async def gql(request, monkeypatch):
    read, requests = AsyncMock(), []

    class Reply:
        async def __aenter__(self):
            return SimpleNamespace(json=read)

        async def __aexit__(self, *args):
            return False

    def http_request(*args, **kwargs):
        json.dumps(kwargs["json"])
        requests.append(deepcopy(kwargs["json"]))
        return Reply()

    async def browser_request(operations):
        json.dumps(operations)
        requests.append(deepcopy(operations))
        return await read()

    http = SimpleNamespace(request=MagicMock(side_effect=http_request))
    browser = SimpleNamespace(gql=AsyncMock(side_effect=browser_request))
    auth = SimpleNamespace(browser_active=request.param, _twitch=SimpleNamespace(_browser=browser),
                           headers=MagicMock(return_value={}))
    auth.validate = AsyncMock(return_value=auth)
    client = GQLClient(http, auth, SimpleNamespace(USER_AGENT="test"))
    sleep = AsyncMock()
    monkeypatch.setattr("src.api.gql_client.asyncio", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr("src.api.gql_client.ExponentialBackoff", lambda **_: [0, 0, 0])
    tasks = asyncio.all_tasks()
    try:
        yield client, read, sleep, requests
    finally:
        limiter = client._qgl_limiter
        task, limiter._reset_task = limiter._reset_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert limiter.concurrent == 0
        assert not {task for task in asyncio.all_tasks() - tasks
                    if task is not asyncio.current_task() and not task.done()}


@pytest.mark.asyncio
@pytest.mark.parametrize("message", MESSAGES)
@pytest.mark.parametrize("name", MUTATIONS)
async def test_single_mutation_is_not_replayed_after_service_error(gql, message, name):
    client, read, sleep, requests = gql
    operation = mutation(name)
    read.side_effect = [failure(message), {"data": {"mustNotReplay": True}}]
    with pytest.raises(GQLException, match="^Automatic retry is unsafe for this GraphQL request$"):
        await client.request(operation)
    assert requests == [operation]
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("message", MESSAGES)
@pytest.mark.parametrize("mutation_first", [False, True])
async def test_partial_batch_does_not_replay_a_confirmed_mutation(gql, message, mutation_first):
    client, read, sleep, requests = gql
    write, query = mutation("NotificationsDelete"), deepcopy(GQL_OPERATIONS["Inventory"])
    success = {"data": {"notificationsDelete": {"status": "done"}}}
    operations = [write, query] if mutation_first else [query, write]
    first = [success, failure(message)] if mutation_first else [failure(message), success]
    read.side_effect = [first, [{"data": {"mustNotReplay": True}}] * 2]
    with pytest.raises(GQLException, match="^Automatic retry is unsafe for this GraphQL request$"):
        await client.request(operations)
    assert requests == [operations]
    assert success == {"data": {"notificationsDelete": {"status": "done"}}}
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("message", MESSAGES)
async def test_known_read_retains_existing_retry_behavior(gql, message):
    client, read, sleep, requests = gql
    operation = deepcopy(GQL_OPERATIONS["Inventory"])
    success = {"data": {"currentUser": {"id": "42"}}}
    read.side_effect = [failure(message), success]
    assert await client.request(operation) is success
    assert requests == [operation, operation]
    sleep.assert_awaited_once_with(5 if message in MESSAGES[:2] else 0)


UNSAFE = [
    {"query": "mutation { testOnly }"},
    {"query": "query { currentUser { id } }"},
    {"operationName": "Inventory"},
    {**GQL_OPERATIONS["Inventory"], "query": "mutation { testOnly }"},
    {**GQL_OPERATIONS["Inventory"], "operationName": "Unknown"},
    {**GQL_OPERATIONS["Inventory"], "extensions": {"persistedQuery": {"version": 1, "sha256Hash": "unknown"}}},
    {**GQL_OPERATIONS["Inventory"], "extensions": {"persistedQuery": {
        "version": True, "sha256Hash": GQL_OPERATIONS["Inventory"]["extensions"]["persistedQuery"]["sha256Hash"],
    }}},
    {**GQL_OPERATIONS["Inventory"], "extensions": {"persistedQuery": {
        "version": 1, "sha256Hash": [],
    }}},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", UNSAFE)
@pytest.mark.parametrize("message", MESSAGES)
async def test_unrecognized_reads_and_mutations_are_not_automatically_replayed(gql, operation, message, caplog):
    client, read, sleep, requests = gql
    operation = deepcopy(operation)
    before = deepcopy(operation)
    read.side_effect = [failure(message), {"data": {"mustNotRun": True}}]
    with pytest.raises(GQLException, match="^Automatic retry is unsafe for this GraphQL request$") as error:
        await client.request(operation)
    assert requests == [before] and operation == before
    assert error.value.__cause__ is error.value.__context__ is None
    sleep.assert_not_awaited()
    assert not any("Retrying" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", MUTATIONS)
@pytest.mark.parametrize("batched", [False, True])
async def test_successful_mutations_are_still_sent_once_and_returned_unchanged(gql, name, batched):
    client, read, sleep, requests = gql
    operation = mutation(name)
    operations = [operation] if batched else operation
    result = {"data": {"result": "success"}}
    read.return_value = [result] if batched else result
    assert await client.request(operations) is read.return_value
    assert requests == [operations]
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("message", MESSAGES)
@pytest.mark.parametrize("failed_first", [False, True])
async def test_all_read_batches_retain_retry_timing_and_order(gql, message, failed_first):
    client, read, sleep, requests = gql
    operations = [deepcopy(GQL_OPERATIONS["Inventory"]), deepcopy(GQL_OPERATIONS["Campaigns"])]
    partial = {"data": {"first": "read"}}
    read.side_effect = [[failure(message), partial] if failed_first else [partial, failure(message)],
                        [{"data": {"first": "complete"}}, {"data": {"second": "complete"}}]]
    result = await client.request(operations)
    assert result == [{"data": {"first": "complete"}}, {"data": {"second": "complete"}}]
    assert requests == [operations, operations]
    sleep.assert_awaited_once_with(5 if message in MESSAGES[:2] else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("message", MESSAGES[:2])
async def test_single_retry_limit_for_known_reads_is_unchanged(gql, message):
    client, read, sleep, requests = gql
    operation = deepcopy(GQL_OPERATIONS["Inventory"])
    read.side_effect = [failure(message), failure(message), {"data": {"mustNotRun": True}}]
    with pytest.raises(GQLException):
        await client.request(operation)
    assert requests == [operation, operation]
    sleep.assert_awaited_once_with(5)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["response", "backoff"])
async def test_caller_changes_do_not_replace_a_pending_read_with_a_mutation(gql, phase):
    client, read, sleep, requests = gql
    operation = deepcopy(GQL_OPERATIONS["Inventory"])
    before = deepcopy(operation)
    result = {"data": {"currentUser": {"id": "42"}}}

    def change():
        operation.clear()
        operation.update(mutation("NotificationsDelete"))

    async def incoming():
        if read.await_count == 1:
            if phase == "response":
                change()
            return failure("service unavailable")
        return result

    if phase == "backoff":
        sleep.side_effect = lambda _: change()
    read.side_effect = incoming
    assert await client.request(operation) is result
    assert requests == [before, before]
    assert operation == mutation("NotificationsDelete")


@pytest.mark.asyncio
async def test_snapshot_preserves_nested_variables_and_batch_membership(gql):
    client, read, sleep, requests = gql
    operations = [GQL_OPERATIONS["CurrentDrop"].with_variables({"channelID": "123"}),
                  deepcopy(GQL_OPERATIONS["Inventory"])]
    before = deepcopy(operations)
    result = [{"data": {"first": "done"}}, {"data": {"second": "done"}}]

    async def incoming():
        if read.await_count == 1:
            operations[0]["variables"]["channelID"] = "999"
            operations.append(mutation("NotificationsDelete"))
            return [failure("service unavailable"), {"data": {"second": "initial"}}]
        return result

    read.side_effect = incoming
    assert await client.request(operations) is result
    assert requests == [before, before]
    assert len(operations) == 3 and operations[0]["variables"]["channelID"] == "999"
    sleep.assert_awaited_once_with(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["backoff", "auth"])
async def test_owned_payload_tampering_is_rechecked_before_retry_send(gql, phase):
    client, read, sleep, requests = gql
    operation = deepcopy(GQL_OPERATIONS["Inventory"])
    read.side_effect = [failure("service unavailable"), {"data": {"mustNotRun": True}}]

    def change():
        if client._auth_state.browser_active:
            live = client._auth_state._twitch._browser.gql.await_args.args[0]
        else:
            live = client.http_client.request.call_args.kwargs["json"]
        live.clear()
        live.update(mutation("NotificationsDelete"))

    if phase == "backoff":
        sleep.side_effect = lambda _: change()
    else:
        async def validate():
            if client._auth_state.validate.await_count == 2:
                change()
            return client._auth_state
        client._auth_state.validate.side_effect = validate
    with pytest.raises(GQLException, match="^Automatic retry is unsafe for this GraphQL request$"):
        await client.request(operation)
    assert requests == [operation]
    assert read.await_count == 1
    sleep.assert_awaited_once_with(0)


@pytest.mark.asyncio
async def test_initial_mutation_cannot_be_reclassified_by_transport_callback(gql):
    client, read, sleep, requests = gql
    operation = mutation("NotificationsDelete")

    async def incoming():
        if client._auth_state.browser_active:
            live = client._auth_state._twitch._browser.gql.await_args.args[0]
        else:
            live = client.http_client.request.call_args.kwargs["json"]
        live.clear()
        live.update(deepcopy(GQL_OPERATIONS["Inventory"]))
        return failure("service unavailable")

    read.side_effect = incoming
    with pytest.raises(GQLException, match="^Automatic retry is unsafe for this GraphQL request$"):
        await client.request(operation)
    assert requests == [operation]
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_during_service_retry_wait_sends_nothing_more(gql):
    client, read, sleep, requests = gql
    operation = deepcopy(GQL_OPERATIONS["Inventory"])
    read.return_value = failure("service unavailable")
    entered, drained = asyncio.Event(), asyncio.Event()

    async def wait(_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    sleep.side_effect = wait
    task = asyncio.create_task(client.request(operation))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert drained.is_set()
    assert requests == [operation] and read.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [asyncio.CancelledError, TimeoutError, RuntimeError])
async def test_transport_failures_are_not_reclassified_or_retried(gql, error_type):
    client, read, sleep, requests = gql
    operation = deepcopy(GQL_OPERATIONS["Inventory"])
    error = error_type("test transport failure")
    read.side_effect = error
    with pytest.raises(error_type) as raised:
        await client.request(operation)
    assert raised.value is error
    assert requests == [operation]
    sleep.assert_not_awaited()


@pytest.mark.parametrize("name", GQL_OPERATIONS)
def test_shared_classifier_preserves_existing_catalog_boundary(name):
    expected = name in {
        "GetStreamInfo", "ChannelPointsContext", "Inventory", "CurrentDrop", "Campaigns",
        "CampaignDetails", "AvailableDrops", "PlaybackAccessToken", "GameDirectory",
        "SlugRedirect", "NotificationsList",
    }
    operation = deepcopy(GQL_OPERATIONS[name])
    before = deepcopy(operation)
    assert GQLRetryPolicy.is_safe_read(operation) is expected
    assert ImportedSession._safe_read(operation) is expected
    assert GQLRetryPolicy.can_retry([GQL_OPERATIONS["Inventory"], operation]) is expected
    assert operation == before


@pytest.mark.parametrize("operation", [None, True, 0, "Inventory", {}, [], [GQL_OPERATIONS["Inventory"], {}], *UNSAFE])
def test_unknown_and_empty_requests_are_not_retry_candidates(operation):
    assert not GQLRetryPolicy.can_retry(operation)


@pytest.mark.asyncio
@pytest.mark.parametrize("message", MESSAGES)
@pytest.mark.parametrize("mutation_first", [False, True])
async def test_production_dispatch_does_not_repeat_a_partial_batch_mutation(tmp_path, monkeypatch, message, mutation_first):
    async with running_miner(tmp_path, monkeypatch) as (client, peer, _):
        write, query = mutation("NotificationsDelete"), deepcopy(GQL_OPERATIONS["Inventory"])
        operations = [write, query] if mutation_first else [query, write]
        success = {"data": {"notificationsDelete": {"status": "done"}}}
        peer.responses.extend([[success, failure(message)] if mutation_first else [failure(message), success],
                               [{"data": {"mustNotRun": True}}] * 2])
        with pytest.raises(GQLException, match="^Automatic retry is unsafe for this GraphQL request$"):
            await client.gql_request(operations)
        assert peer.calls == [operations]
        assert len(peer.responses) == 1
        assert client._browser.status()["state"] == "ready"
