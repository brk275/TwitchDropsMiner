"""A retry must not depend on optional response extensions for its log label."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.config import GQL_OPERATIONS
from src.exceptions import GQLException
from tests.test_imported_session_retry import running_miner


@pytest.fixture
def retry_sleep(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("src.api.gql_client.asyncio", SimpleNamespace(sleep=sleep))
    return sleep


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["service error", "PersistedQueryNotFound"])
@pytest.mark.parametrize("metadata", [
    {}, {"extensions": {}}, {"extensions": None},
    {"extensions": {"operationName": "response-label-canary"}},
])
async def test_single_retry_uses_requested_operation_without_response_metadata(
    tmp_path, monkeypatch, caplog, message, metadata, retry_sleep,
):
    operation = GQL_OPERATIONS["CurrentDrop"].with_variables({"channelID": "123"})
    success = {"data": {"currentUser": {"dropCurrentSession": None}}}
    async with running_miner(tmp_path, monkeypatch) as (client, peer, _clock):
        peer.responses.extend([{"errors": [{"message": message}], **metadata}, success])
        assert await client.gql_request(operation) == success
        assert peer.calls == [operation, operation]
        retry_sleep.assert_awaited_once_with(5)
        assert f"Retrying a {message} for {operation['operationName']}" in caplog.text
        assert "response-label-canary" not in caplog.text
        assert client._browser.status()["state"] == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_index", [0, 1])
async def test_batch_retry_labels_the_failing_requested_operation(
    tmp_path, monkeypatch, caplog, failed_index, retry_sleep,
):
    operations = [GQL_OPERATIONS["Inventory"], GQL_OPERATIONS["Campaigns"]]
    success = [{"data": {"ok": True}}, {"data": {"ok": True}}]
    failed = deepcopy(success)
    failed[failed_index] = {"errors": [{"message": "service error"}]}
    async with running_miner(tmp_path, monkeypatch) as (client, peer, _clock):
        peer.responses.extend([failed, success])
        assert await client.gql_request(operations) == success
        assert peer.calls == [operations, operations]
        retry_sleep.assert_awaited_once_with(5)
        assert (
            f"Retrying a service error for {operations[failed_index]['operationName']}"
            in caplog.text
        )


@pytest.mark.asyncio
async def test_repeat_service_error_still_exhausts_single_retry(tmp_path, monkeypatch, retry_sleep):
    operation = GQL_OPERATIONS["Inventory"]
    failure = {"errors": [{"message": "service error"}]}
    async with running_miner(tmp_path, monkeypatch) as (client, peer, _clock):
        peer.responses.extend([deepcopy(failure), deepcopy(failure)])
        with pytest.raises(GQLException):
            await client.gql_request(operation)
        assert peer.calls == [operation, operation]
        retry_sleep.assert_awaited_once_with(5)
