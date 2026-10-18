"""Real claim confirmations must compose with pending progress reconciliation."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from src.config import GQL_OPERATIONS
from src.drop_history import DropHistory
from src.exceptions import GQLException
from tests.test_current_drop_responses import response
from tests.test_gql_retry_safety import gql as gql
from tests.test_imported_session_retry import running_miner
from tests.test_progress_response_integration import mining as mining
from tests.test_progress_response_integration import run_poll, snapshot
from tests.test_progress_retry_integration import drain_limiter


CLAIMS = {
    "null_data": {"data": None},
    "missing_status": {"data": {"claimDropRewards": {"other": 1}}},
    "partial_error": {
        "data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}, "nullable": "test"},
        "errors": [{"message": "server error", "path": ["nullable"]}],
    },
    "confirmed": {"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}},
    "already_confirmed": {"data": {"claimDropRewards": {"status": "DROP_INSTANCE_ALREADY_CLAIMED"}}},
}
PROGRESS = {
    "valid": response({"dropID": "drop", "currentMinutesWatched": 9}),
    "empty": response(None),
    "invalid": {"errors": [None]},
    "failed": {"errors": [{"message": "test-only request failure"}]},
}
CURRENT = GQL_OPERATIONS["CurrentDrop"]["operationName"]
CLAIM = GQL_OPERATIONS["ClaimDrop"]["operationName"]


async def compose(mining, monkeypatch, dispatch, cleanup, *, claim_kind, progress_kind, target):
    twitch, _, campaign = mining
    claimed = campaign.timed_drops[target]
    confirmed = claim_kind in ("confirmed", "already_confirmed")
    before, revision, attempts = snapshot(campaign), campaign.progress_revision, []

    async def attempt():
        result = await claimed.claim()
        attempts.append(result)
        assert result is confirmed

    async def query(operation):
        if operation["operationName"] != CURRENT:
            return await dispatch(operation)
        try:
            result = await dispatch(operation)
        except GQLException:
            # ImportedSession has released its lock before another request starts.
            await attempt()
            raise
        await attempt()
        return result

    await run_poll(twitch, monkeypatch, (), query=query, cleanup=cleanup)
    assert attempts == [confirmed]
    if confirmed:
        assert campaign.progress_revision == revision + 1
        assert claimed.is_claimed and claimed.real_current_minutes == 60 and claimed.extra_current_minutes == 0
        for drop, old in zip(campaign.drops, before, strict=True):
            assert drop.progress_observation == old[-1]
            if drop is not claimed:
                assert (drop.real_current_minutes, drop.extra_current_minutes, drop.is_claimed) == old[:3]
        twitch.drop_history.record.assert_called_once_with(claimed, campaign)
        twitch.request_games_update.assert_called_once_with()
        twitch.gui.broadcast_wanted_items_now.assert_awaited_once_with()
        campaign.bump_minutes.assert_not_called()
        assert await claimed.claim() is True
        twitch.drop_history.record.assert_called_once_with(claimed, campaign)
        assert campaign.progress_revision == revision + 1
    else:
        assert not any(drop.is_claimed for drop in campaign.drops)
        twitch.drop_history.record.assert_not_called()
        twitch.request_games_update.assert_not_called()
        twitch.gui.broadcast_wanted_items_now.assert_not_awaited()
        if progress_kind == "valid":
            assert campaign.progress_revision == revision + 1
            assert [drop.real_current_minutes for drop in campaign.drops] == [9, 9]
            assert [drop.extra_current_minutes for drop in campaign.drops] == [0, 0]
            assert campaign.timed_drops["drop"].progress_observation["source"] == "current_drop"
            assert campaign.timed_drops["sibling"].progress_observation == before[1][-1]
            campaign.bump_minutes.assert_not_called()
        else:
            assert campaign.progress_revision == revision
            assert [drop.real_current_minutes for drop in campaign.drops] == [10, 10]
            assert [drop.extra_current_minutes for drop in campaign.drops] == [2 if progress_kind == "invalid" else 3] * 2
            assert [drop.progress_observation for drop in campaign.drops] == [old[-1] for old in before]
            assert campaign.bump_minutes.call_count == int(progress_kind != "invalid")


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_kind", CLAIMS)
@pytest.mark.parametrize("progress_kind", PROGRESS)
@pytest.mark.parametrize("target", ["drop", "sibling"])
async def test_real_gql_claim_result_does_not_bypass_poll_observation_guards(
    mining, gql, monkeypatch, claim_kind, progress_kind, target,
):
    twitch, channel, campaign = mining
    client, read, sleep, requests = gql

    async def reply():
        assert requests[-1]["operationName"] in (CURRENT, CLAIM)
        return deepcopy(PROGRESS[progress_kind] if requests[-1]["operationName"] == CURRENT else CLAIMS[claim_kind])

    async def cleanup():
        await drain_limiter(client)

    read.side_effect = reply
    await compose(mining, monkeypatch, client.request, cleanup,
                  claim_kind=claim_kind, progress_kind=progress_kind, target=target)
    assert requests == [GQL_OPERATIONS["CurrentDrop"].with_variables({"channelID": str(channel.id)}),
                        GQL_OPERATIONS["ClaimDrop"].with_variables({"input": {"dropInstanceID": campaign.timed_drops[target].claim_id}})]
    assert read.await_count == client._auth_state.validate.await_count == 2
    sleep.assert_not_awaited()
    twitch.close.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_kind", CLAIMS)
@pytest.mark.parametrize("progress_kind", ["valid", "empty", "invalid"])
@pytest.mark.parametrize("target", ["drop", "sibling"])
async def test_actual_imported_claim_and_watch_composition_keeps_session_ready(
    mining, tmp_path, monkeypatch, claim_kind, progress_kind, target,
):
    _, channel, campaign = mining
    async with running_miner(tmp_path, monkeypatch) as (client, peer, _):
        peer.responses.extend([deepcopy(PROGRESS[progress_kind]), deepcopy(CLAIMS[claim_kind])])

        async def cleanup():
            await drain_limiter(client._gql_client)

        await compose(mining, monkeypatch, client.gql_request, cleanup,
                      claim_kind=claim_kind, progress_kind=progress_kind, target=target)
        assert peer.calls == [GQL_OPERATIONS["CurrentDrop"].with_variables({"channelID": str(channel.id)}),
                              GQL_OPERATIONS["ClaimDrop"].with_variables({"input": {"dropInstanceID": campaign.timed_drops[target].claim_id}})]
        assert not peer.responses
        assert not client._browser._lock.locked()
        assert client._browser.status()["state"] == "ready"
        assert client._browser.status()["generation"] == 1


async def cancelled_delivery(mining, monkeypatch, dispatch, cleanup, history_dir, target):
    twitch, _, campaign = mining
    claimed = campaign.timed_drops[target]
    before, revision = snapshot(campaign), campaign.progress_revision
    history_dir.mkdir()
    history = twitch.drop_history = DropHistory(history_dir)
    settled = []

    async def cancel():
        assert claimed.is_claimed and claimed.real_current_minutes == 60 and claimed.extra_current_minutes == 0
        assert campaign.progress_revision == revision + 1
        assert DropHistory(history_dir).total_count == 1
        settled.append(True)
        raise asyncio.CancelledError

    async def query(operation):
        if operation["operationName"] != CURRENT:
            return await dispatch(operation)
        await dispatch(operation)
        await claimed.claim()
        raise AssertionError("Cancelled GUI delivery must propagate")

    twitch.gui.broadcast_wanted_items_now = AsyncMock(side_effect=cancel)
    await run_poll(twitch, monkeypatch, (), query=query, cleanup=cleanup)
    assert settled == [True]
    assert campaign.progress_revision == revision + 1
    assert history.total_count == DropHistory(history_dir).total_count == 1
    assert history.get_entries()[0]["id"] == claimed.id
    for drop, old in zip(campaign.drops, before, strict=True):
        assert drop.progress_observation == old[-1]
        if drop is not claimed:
            assert (drop.real_current_minutes, drop.extra_current_minutes, drop.is_claimed) == old[:3]
    twitch.request_games_update.assert_called_once_with()
    twitch.gui.broadcast_wanted_items_now.assert_awaited_once_with()
    campaign.bump_minutes.assert_not_called()
    assert await claimed.claim() is True
    assert campaign.progress_revision == revision + 1
    assert DropHistory(history_dir).total_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_kind", ["confirmed", "already_confirmed"])
@pytest.mark.parametrize("target", ["drop", "sibling"])
async def test_claim_history_survives_cancelled_gui_delivery_during_progress_poll(
    mining, gql, monkeypatch, tmp_path, claim_kind, target,
):
    client, read, sleep, requests = gql

    async def reply():
        return deepcopy(PROGRESS["valid"] if requests[-1]["operationName"] == CURRENT else CLAIMS[claim_kind])

    async def cleanup():
        await drain_limiter(client)

    read.side_effect = reply
    await cancelled_delivery(mining, monkeypatch, client.request, cleanup, tmp_path / "history", target)
    assert [operation["operationName"] for operation in requests] == [CURRENT, CLAIM]
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_kind", ["confirmed", "already_confirmed"])
@pytest.mark.parametrize("target", ["drop", "sibling"])
async def test_actual_imported_claim_history_survives_cancelled_gui_delivery(
    mining, monkeypatch, tmp_path, claim_kind, target,
):
    async with running_miner(tmp_path, monkeypatch) as (client, peer, _):
        peer.responses.extend([deepcopy(PROGRESS["valid"]), deepcopy(CLAIMS[claim_kind])])

        async def cleanup():
            await drain_limiter(client._gql_client)

        await cancelled_delivery(mining, monkeypatch, client.gql_request, cleanup, tmp_path / "history", target)
        assert [operation["operationName"] for operation in peer.calls] == [CURRENT, CLAIM]
        assert not peer.responses and not client._browser._lock.locked()
        assert client._browser.status()["state"] == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_kind", ["null_data", "missing_status", "partial_error"])
@pytest.mark.parametrize("target", ["drop", "sibling"])
async def test_later_explicit_confirmation_preserves_new_direct_reading_and_records_once(
    mining, gql, monkeypatch, claim_kind, target,
):
    twitch, _, campaign = mining
    client, read, sleep, requests = gql
    claimed, attempts = campaign.timed_drops[target], []

    async def reply():
        if requests[-1]["operationName"] == CURRENT:
            return deepcopy(PROGRESS["valid"])
        attempts.append(True)
        return deepcopy(CLAIMS[claim_kind if len(attempts) == 1 else "already_confirmed"])

    async def cleanup():
        await drain_limiter(client)

    read.side_effect = reply
    await compose(mining, monkeypatch, client.request, cleanup,
                  claim_kind=claim_kind, progress_kind="valid", target=target)
    direct = [drop.progress_observation for drop in campaign.drops]
    revision = campaign.progress_revision
    assert await claimed.claim() is True
    assert await claimed.claim() is True
    assert [operation["operationName"] for operation in requests] == [CURRENT, CLAIM, CLAIM]
    assert len(attempts) == 2
    assert campaign.progress_revision == revision + 1
    assert [drop.progress_observation for drop in campaign.drops] == direct
    assert claimed.real_current_minutes == 60 and claimed.extra_current_minutes == 0
    twitch.drop_history.record.assert_called_once_with(claimed, campaign)
    twitch.request_games_update.assert_called_once_with()
    twitch.gui.broadcast_wanted_items_now.assert_awaited_once_with()
    sleep.assert_not_awaited()
