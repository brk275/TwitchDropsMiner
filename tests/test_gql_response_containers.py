"""Malformed structured replies must fail as requests, not Python traversal errors."""

from copy import deepcopy

import pytest

from src.auth.imported_session import ImportedSession
from src.config import GQL_OPERATIONS
from src.exceptions import GQLException, GQLInvalidResponse
from tests.test_current_drop_responses import response
from tests.test_gql_retry_safety import gql as gql
from tests.test_gql_server_error_paths import server_error
from tests.test_imported_session_retry import running_miner
from tests.test_progress_response_integration import mining as mining
from tests.test_progress_response_integration import run_poll, snapshot
from tests.test_progress_retry_integration import drain_limiter


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, False, 7, "private malformed reply", [], [None]])
async def test_invalid_single_response_container_is_rejected_without_retry(gql, payload):
    client, read, sleep, operations = gql
    before = deepcopy(payload)
    read.return_value = payload
    with pytest.raises(GQLInvalidResponse, match="^Invalid GraphQL response container or error metadata$"):
        await client.request(GQL_OPERATIONS["Inventory"])
    assert payload == before
    assert operations == [GQL_OPERATIONS["Inventory"]]
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_valid_response_control_is_returned_unchanged(gql):
    client, read, sleep, operations = gql
    result = {"data": {"currentUser": {"id": "42"}}}
    read.return_value = result
    assert await client.request(GQL_OPERATIONS["Inventory"]) is result
    assert operations == [GQL_OPERATIONS["Inventory"]]
    sleep.assert_not_awaited()


MALFORMED_ERRORS = [
    {"errors": value} for value in (
        None, False, 0, "private invalid error list", {}, [], [None], [False], [7], [[]], [{}],
        [{"message": None}], [{"message": []}], [{"message": {}}], [{"message": True}],
        [{"message": ""}], [{"message": "service unavailable"}, None],
    )
] + [
    {"error": "private missing message"},
    {"error": None, "message": "private invalid error"},
    {"error": [], "message": "private invalid error"},
    {"error": "", "message": "private invalid error"},
    {"error": "private error", "message": []},
    {"error": "private error", "message": ""},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", MALFORMED_ERRORS)
@pytest.mark.parametrize("batched", [False, True])
async def test_malformed_error_metadata_is_fixed_and_never_retried(gql, payload, batched):
    client, read, sleep, operations = gql
    payload = deepcopy(payload)
    returned = [payload] if batched else payload
    before = deepcopy(returned)
    query = [GQL_OPERATIONS["Inventory"]] if batched else GQL_OPERATIONS["Inventory"]
    read.return_value = returned
    with pytest.raises(GQLInvalidResponse) as caught:
        await client.request(query)
    assert str(caught.value) == "Invalid GraphQL response container or error metadata"
    assert caught.value.__cause__ is caught.value.__context__ is None
    assert returned == before
    assert operations == [query]
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, {}, [], [None, {}], [{}, 7], [{}], [{}, {}, {}]])
async def test_batched_response_requires_matching_object_rows(gql, payload):
    client, read, sleep, operations = gql
    query = [GQL_OPERATIONS["Inventory"], GQL_OPERATIONS["Campaigns"]]
    read.return_value = payload
    with pytest.raises(GQLInvalidResponse):
        await client.request(query)
    assert operations == [query]
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("batched", [False, True])
@pytest.mark.parametrize("payload", [{}, {"data": None}, {"data": ["operation-owned"]}, {"extensions": None}])
async def test_valid_container_does_not_invent_an_operation_data_schema(gql, batched, payload):
    client, read, sleep, _ = gql
    result = [payload] if batched else payload
    query = [GQL_OPERATIONS["Inventory"]] if batched else GQL_OPERATIONS["Inventory"]
    read.return_value = result
    assert await client.request(query) is result
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_request_batch_retains_its_empty_response_control(gql):
    client, read, sleep, operations = gql
    read.return_value = []
    assert await client.request([]) == []
    assert operations == [[]]
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [{}] + [{"extensions": value} for value in (
    None, [], "private metadata", {}, {"operationName": []}, {"operationName": ""},
)])
@pytest.mark.parametrize("message", ["service error", "PersistedQueryNotFound"])
async def test_optional_retry_metadata_cannot_crash_a_known_read(gql, metadata, message, caplog):
    client, read, sleep, operations = gql
    failed = {"errors": [{"message": message}], **metadata}
    saved = deepcopy(failed)
    success = {"data": {"currentUser": {"id": "42"}}}
    read.side_effect = [failed, success]
    assert await client.request(GQL_OPERATIONS["Inventory"]) is success
    assert operations == [GQL_OPERATIONS["Inventory"]] * 2
    assert failed == saved
    sleep.assert_awaited_once_with(5)
    assert any(record.getMessage() == f"Retrying a {message} for unknown" for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_first", [False, True])
@pytest.mark.parametrize("first_action", ["normalize", "retry"])
async def test_later_bad_row_is_validated_before_any_row_can_mutate_or_retry(gql, bad_first, first_action):
    client, read, sleep, operations = gql
    valid = (server_error({"user": {"name": "untouched"}}, ["user", "name"]) if first_action == "normalize"
             else {"errors": [{"message": "service unavailable"}]})
    malformed = {"errors": [None]}
    payload = [malformed, valid] if bad_first else [valid, malformed]
    before = deepcopy(payload)
    query = [GQL_OPERATIONS["Inventory"], GQL_OPERATIONS["Campaigns"]]
    read.return_value = payload
    with pytest.raises(GQLInvalidResponse):
        await client.request(query)
    assert payload == before
    assert operations == [query]
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"errors": [{"message": "unknown request error"}]},
                                    {"error": "request error", "message": "known legacy error"}])
async def test_ordinary_well_formed_request_errors_keep_their_existing_type(gql, payload):
    client, read, sleep, _ = gql
    read.return_value = payload
    with pytest.raises(GQLException) as caught:
        await client.request(GQL_OPERATIONS["Inventory"])
    assert type(caught.value) is GQLException
    sleep.assert_not_awaited()


BAD_PROGRESS = [None, [], [None], {"errors": None}, {"errors": [None]}, {"errors": []},
                {"error": "private missing message"}, {"errors": [{"message": []}]},
                {"errors": [{"message": {}}]}, {}]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", BAD_PROGRESS)
async def test_malformed_reply_does_not_estimate_and_the_next_valid_poll_recovers(mining, gql, monkeypatch, payload):
    twitch, channel, campaign = mining
    client, read, sleep, operations = gql
    before, revision = snapshot(campaign), campaign.progress_revision

    async def reply():
        if read.await_count == 1:
            return deepcopy(payload)
        assert snapshot(campaign) == before
        assert campaign.progress_revision == revision
        campaign.bump_minutes.assert_not_called()
        return response({"dropID": "drop", "currentMinutesWatched": 9})

    async def cleanup():
        await drain_limiter(client)

    read.side_effect = reply
    await run_poll(twitch, monkeypatch, (), query=client.request, cleanup=cleanup, until=90)
    query = GQL_OPERATIONS["CurrentDrop"].with_variables({"channelID": str(channel.id)})
    assert operations == [query, query]
    sleep.assert_not_awaited()
    assert campaign.progress_revision == revision + 1
    assert [drop.real_current_minutes for drop in campaign.drops] == [9, 9]
    assert [drop.extra_current_minutes for drop in campaign.drops] == [0, 0]
    assert campaign.timed_drops["drop"].progress_observation["source"] == "current_drop"
    assert campaign.timed_drops["sibling"].progress_observation == before[1][-1]
    campaign.bump_minutes.assert_not_called()
    twitch.drop_history.record.assert_not_called()


@pytest.mark.parametrize("message", [None, False, 1, "", [], {}, ["failed integrity check"], {"nested": "invalid"}, 1.5])
@pytest.mark.parametrize("with_valid_prefix", [False, True])
def test_malformed_auth_message_never_escapes_or_authorizes_replay(message, with_valid_prefix):
    errors = [{"message": message}]
    if with_valid_prefix:
        errors.insert(0, {"message": "failed integrity check"})
    row = {"errors": errors}
    before = deepcopy(row)
    assert ImportedSession._auth_rejection(row) is False
    assert row == before


@pytest.mark.parametrize("message", ["failed integrity check", "invalid oauth token"])
def test_definitive_auth_rejection_controls_keep_data_and_path_guards(message):
    row = {"errors": [{"message": message}]}
    assert ImportedSession._auth_rejection(row) is True
    assert ImportedSession._auth_rejection({**row, "data": None}) is False
    assert ImportedSession._auth_rejection({"errors": [{"message": message, "path": None}]}) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [[], {}])
async def test_imported_claim_with_malformed_message_records_no_success(mining, tmp_path, monkeypatch, message):
    twitch, _, campaign = mining
    before, revision = snapshot(campaign), campaign.progress_revision
    async with running_miner(tmp_path, monkeypatch) as (client, peer, _):
        peer.responses.extend([{"errors": [{"message": message}]}, {"data": {"mustNotRun": True}}])
        client.gui.login.import_pending.reset_mock()
        twitch.gql_request = client.gql_request
        assert await campaign.timed_drops["drop"].claim() is False
        assert len(peer.calls) == len(peer.responses) == 1
        assert client._browser.status()["state"] == "ready"
        client.gui.login.import_pending.assert_not_awaited()
    assert snapshot(campaign) == before
    assert campaign.progress_revision == revision
    twitch.drop_history.record.assert_not_called()
    twitch.request_games_update.assert_not_called()
    twitch.gui.broadcast_wanted_items_now.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [[], {}])
@pytest.mark.parametrize("bad_first", [False, True])
async def test_malformed_auth_message_in_partial_batch_never_repeats_a_write(tmp_path, monkeypatch, message, bad_first):
    async with running_miner(tmp_path, monkeypatch) as (client, peer, _):
        claim = GQL_OPERATIONS["ClaimDrop"].with_variables({"input": {"dropInstanceID": "test-only"}})
        inventory = GQL_OPERATIONS["Inventory"]
        success = {"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}}
        malformed = {"errors": [{"message": "failed integrity check"}, {"message": message}]}
        query = [inventory, claim] if bad_first else [claim, inventory]
        reply = [malformed, success] if bad_first else [success, malformed]
        before = deepcopy(reply)
        peer.responses.extend([reply, {"data": {"mustNotRun": True}}])
        client.gui.login.import_pending.reset_mock()
        with pytest.raises(GQLInvalidResponse):
            await client.gql_request(query)
        assert peer.calls == [query]
        assert len(peer.responses) == 1
        assert reply == before
        assert client._browser.status()["state"] == "ready"
        assert client._browser.status()["generation"] == 1
        client.gui.login.import_pending.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, [], {"errors": None}, {"error": "private missing message"}])
async def test_actual_claim_path_returns_false_without_recording_malformed_success(mining, gql, payload):
    twitch, _, campaign = mining
    client, read, sleep, operations = gql
    before, revision = snapshot(campaign), campaign.progress_revision
    twitch.gql_request = client.request
    read.return_value = deepcopy(payload)
    assert await campaign.timed_drops["drop"].claim() is False
    assert snapshot(campaign) == before
    assert campaign.progress_revision == revision
    assert len(operations) == 1
    sleep.assert_not_awaited()
    twitch.drop_history.record.assert_not_called()
    twitch.request_games_update.assert_not_called()
    twitch.gui.broadcast_wanted_items_now.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", BAD_PROGRESS[:-1])
async def test_imported_response_failure_skips_estimates_without_rejecting_session(mining, tmp_path, monkeypatch, payload):
    twitch, _, campaign = mining
    before, revision = snapshot(campaign), campaign.progress_revision
    async with running_miner(tmp_path, monkeypatch) as (client, peer, _):
        peer.responses.extend([deepcopy(payload), response({"dropID": "drop", "currentMinutesWatched": 9})])
        count = 0

        async def query(operation):
            nonlocal count
            count += 1
            if count == 2:
                assert snapshot(campaign) == before
                assert campaign.progress_revision == revision
                campaign.bump_minutes.assert_not_called()
            return await client.gql_request(operation)

        async def cleanup():
            await drain_limiter(client._gql_client)

        await run_poll(twitch, monkeypatch, (), query=query, cleanup=cleanup, until=90)
        assert len(peer.calls) == 2
        assert not peer.responses
        assert client._browser.status()["state"] == "ready"
        assert client._browser.status()["generation"] == 1
    assert campaign.progress_revision == revision + 1
    assert campaign.timed_drops["drop"].progress_observation["source"] == "current_drop"
    assert campaign.timed_drops["sibling"].progress_observation == before[1][-1]
    campaign.bump_minutes.assert_not_called()
    twitch.drop_history.record.assert_not_called()
