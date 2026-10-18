"""Server-error normalization must not crash or follow invalid response paths."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from src.api.gql_client import GQLClient
from src.config import GQL_OPERATIONS
from src.exceptions import GQLException
from src.services.watch_service import WatchService
from src.utils.async_helpers import AwaitableValue


@pytest_asyncio.fixture(params=[False, True], ids=["http", "browser"])
async def gql(request):
    tasks = asyncio.all_tasks()
    read = AsyncMock()

    class Reply:
        async def __aenter__(self):
            return SimpleNamespace(json=read)

        async def __aexit__(self, *args):
            return False

    http = SimpleNamespace(request=MagicMock(return_value=Reply()))
    browser = SimpleNamespace(gql=read)
    auth = SimpleNamespace(browser_active=request.param, _twitch=SimpleNamespace(_browser=browser),
                           headers=MagicMock(return_value={}))
    auth.validate = AsyncMock(return_value=auth)
    client = GQLClient(http, auth, SimpleNamespace(USER_AGENT="test"))
    try:
        yield client, read
    finally:
        limiter = client._qgl_limiter
        task, limiter._reset_task = limiter._reset_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert limiter.concurrent == 0
        assert not {task for task in asyncio.all_tasks() - tasks
                    if task is not asyncio.current_task() and not task.done()}


def server_error(data, path):
    return {"data": data, "errors": [{"message": "server error", "path": path}]}


UNUSABLE = [
    pytest.param({"errors": [{"message": "server error"}]}, id="missing-data-path"),
    pytest.param({"data": {}, "errors": [{"message": "server error"}]}, id="missing-path"),
    pytest.param(server_error(None, ["currentUser", "dropCurrentSession"]), id="null-root"),
    pytest.param(server_error({"currentUser": None}, ["currentUser", "dropCurrentSession"]), id="bubbled-null"),
    pytest.param(server_error({}, ["currentUser", "dropCurrentSession"]), id="missing-parent"),
    pytest.param(server_error({"currentUser": 7}, ["currentUser", "dropCurrentSession"]), id="integer-parent"),
    pytest.param(server_error({"currentUser": "private-response"}, ["currentUser", "dropCurrentSession"]), id="string-parent"),
    pytest.param(server_error([], [0]), id="array-root"),
    pytest.param(server_error("private-response", ["currentUser"]), id="string-root"),
    pytest.param(server_error({}, []), id="empty-path"),
    pytest.param(server_error({}, None), id="null-path"),
    pytest.param(server_error({}, "private-path"), id="string-path"),
    pytest.param(server_error({}, 0), id="integer-path"),
    pytest.param(server_error({}, {}), id="object-path"),
    pytest.param(server_error({}, [""]), id="empty-key"),
    pytest.param(server_error({}, [[]]), id="array-key"),
    pytest.param(server_error({}, [{}]), id="object-key"),
    pytest.param(server_error({}, [True]), id="boolean-key"),
    pytest.param(server_error({}, [0]), id="integer-object-key"),
    pytest.param(server_error({"items": ["private-response"]}, ["items", True]), id="boolean-index"),
    pytest.param(server_error({"items": ["private-response"]}, ["items", -1]), id="negative-index"),
    pytest.param(server_error({"items": ["private-response"]}, ["items", 1]), id="out-of-range-index"),
    pytest.param(server_error({"items": ["private-response"]}, ["items", 0.0]), id="float-index"),
    pytest.param(server_error({"items": ["private-response"]}, ["items", "0"]), id="string-index"),
    pytest.param(server_error({"items": [None]}, ["items", 0, "name"]), id="null-array-parent"),
    pytest.param(server_error({"items": [{}, {}]}, ["items", True, "name"]), id="boolean-parent-index"),
    pytest.param(server_error({"items": [{}]}, ["items", -1, "name"]), id="negative-parent-index"),
    pytest.param(server_error({"items": [{}]}, ["items", 1, "name"]), id="missing-array-parent"),
    pytest.param(server_error({"user": {}}, ["user", "", "name"]), id="empty-parent-key"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", UNUSABLE)
@pytest.mark.parametrize("batched", [False, True])
async def test_unusable_server_error_paths_raise_gql_without_replay_or_data_mutation(gql, payload, batched):
    client, read = gql
    payload = deepcopy(payload)
    returned = [payload] if batched else payload
    before = deepcopy(returned)
    read.return_value = returned
    operation = GQL_OPERATIONS["ClaimDrop"].with_variables({"input": {"dropInstanceID": "test-only"}})
    with pytest.raises(GQLException, match="^Unable to normalize GraphQL server error$") as error:
        await client.request([operation] if batched else operation)
    assert returned == before
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    read.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("batched", [False, True])
async def test_success_without_errors_is_returned_unchanged(gql, batched):
    client, read = gql
    payload = {"data": {"user": {"id": "42"}}, "extensions": {"requestID": "test"}}
    read.return_value = [payload] if batched else payload
    operations = [GQL_OPERATIONS["Inventory"]] if batched else GQL_OPERATIONS["Inventory"]
    assert await client.request(operations) is read.return_value
    read.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    "service error", "PersistedQueryNotFound", "service timeout", "service unavailable", "context deadline exceeded",
])
async def test_existing_retry_policy_is_unchanged_for_other_error_types(gql, monkeypatch, message):
    client, read = gql
    monkeypatch.setattr("src.api.gql_client.ExponentialBackoff", lambda **_: [0, 0])
    sleep = AsyncMock()
    monkeypatch.setattr("src.api.gql_client.asyncio", SimpleNamespace(sleep=sleep))
    failed = {"errors": [{"message": message}], "extensions": {"operationName": "Inventory"}}
    success = {"data": {"currentUser": {"id": "42"}}}
    read.side_effect = [failed, success]
    assert await client.request(GQL_OPERATIONS["Inventory"]) is success
    assert read.await_count == 2
    sleep.assert_awaited_once_with(5 if message in ("service error", "PersistedQueryNotFound") else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["service error", "PersistedQueryNotFound"])
async def test_once_only_retries_are_not_extended(gql, monkeypatch, message):
    client, read = gql
    monkeypatch.setattr("src.api.gql_client.ExponentialBackoff", lambda **_: [0, 0, 0])
    sleep = AsyncMock()
    monkeypatch.setattr("src.api.gql_client.asyncio", SimpleNamespace(sleep=sleep))
    failed = {"errors": [{"message": message}], "extensions": {"operationName": "Inventory"}}
    read.side_effect = [deepcopy(failed), deepcopy(failed), {"data": {"mustNotRun": True}}]
    with pytest.raises(GQLException):
        await client.request(GQL_OPERATIONS["Inventory"])
    assert read.await_count == 2
    sleep.assert_awaited_once_with(5)


@pytest.mark.asyncio
async def test_unknown_error_retains_existing_gql_failure(gql):
    client, read = gql
    read.return_value = {"errors": [{"message": "test unknown error"}]}
    with pytest.raises(GQLException, match="test unknown error"):
        await client.request(GQL_OPERATIONS["Inventory"])
    read.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [asyncio.CancelledError(), TimeoutError("test"), RuntimeError("test")])
async def test_transport_cancellation_and_unexpected_failures_propagate(gql, failure):
    client, read = gql
    read.side_effect = failure
    with pytest.raises(type(failure)) as error:
        await client.request(GQL_OPERATIONS["Inventory"])
    assert error.value is failure
    read.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    server_error(None, ["currentUser", "dropCurrentSession"]),
    server_error({"currentUser": None}, ["currentUser", "dropCurrentSession"]),
    {"data": {}, "errors": [{"message": "server error"}]},
    server_error({"items": []}, ["items", 0]),
])
async def test_watch_loop_keeps_existing_fallback_and_recovers_on_next_poll(gql, monkeypatch, payload):
    client, read = gql
    clock, watches = [0.0], []
    read.side_effect = [deepcopy(payload), {"data": {"currentUser": {"dropCurrentSession": {
        "dropID": "drop", "currentMinutesWatched": 9,
    }}}}]
    channel = MagicMock(id=123, online=True, banned=False)
    channel._stream = object()
    channel._watch_current.return_value = True

    async def send_watch():
        watches.append(clock[0])
        return True

    channel.send_watch = send_watch
    selected = AwaitableValue()
    selected.set(channel)
    campaign = MagicMock(first_drop=None)
    drop = MagicMock()
    drop.can_earn.return_value = True
    twitch = SimpleNamespace(
        is_mining_time=lambda: True, watching_channel=selected, close=MagicMock(),
        gui=SimpleNamespace(progress=SimpleNamespace(minute_almost_done=lambda: True)),
        gql_request=client.request, _drops={"drop": drop},
        _inventory_service=SimpleNamespace(get_active_campaign=lambda _: campaign),
    )
    service = WatchService(twitch)

    async def advance(delay):
        clock[0] += delay
        if clock[0] >= 90:
            raise asyncio.CancelledError

    monkeypatch.setattr("src.services.watch_service.monotonic", lambda: clock[0])
    monkeypatch.setattr(service, "watch_sleep", advance)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(service.watch_loop(), 2)
    assert read.await_count == 2
    assert watches == list(range(0, 90, 10))
    campaign.bump_minutes.assert_called_once_with(channel)
    drop.update_minutes.assert_called_once_with(9)
    twitch.close.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(("data", "path", "expected"), [
    ({"user": {"dropCampaign": {"id": "one"}, "id": "42"}}, ["user", "dropCampaign"],
     {"user": {"dropCampaign": None, "id": "42"}}),
    ({"alias": {"value": None, "keep": 7}}, ["alias", "value"], {"alias": {"value": None, "keep": 7}}),
    ({"alias": {"keep": 7}}, ["alias", "value"], {"alias": {"keep": 7, "value": None}}),
    ({}, ["user"], {"user": None}),
    ({"items": [{"name": "one"}, {"name": "two", "keep": 7}]}, ["items", 1, "name"],
     {"items": [{"name": "one"}, {"name": None, "keep": 7}]}),
    ({"items": [{"name": "one"}, {"name": "two"}]}, ["items", 0], {"items": [None, {"name": "two"}]}),
    ({"items": [["one", "two"]]}, ["items", 0, 1], {"items": [["one", None]]}),
])
async def test_valid_paths_preserve_existing_nullification_and_other_batch_members(gql, data, path, expected):
    client, read = gql
    response = server_error(deepcopy(data), deepcopy(path))
    errors = deepcopy(response["errors"])
    untouched = {"data": {"user": {"id": "other"}}, "extensions": {"requestID": "test"}}
    read.return_value = [untouched, response]
    assert await client.request([GQL_OPERATIONS["Inventory"], GQL_OPERATIONS["Inventory"]]) is read.return_value
    assert response == {"data": expected, "errors": errors}
    assert untouched == {"data": {"user": {"id": "other"}}, "extensions": {"requestID": "test"}}
    read.assert_awaited_once()
