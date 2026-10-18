"""Cache clearing must not let a new inventory overtake an older claim result."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.config import GQL_OPERATIONS, State
from src.drop_history import DropHistory
from src.exceptions import GQLException
from tests.test_cache_clear import _make_cache_test_client
from tests.test_imported_session_retry import running_miner
from tests.test_inventory_refresh_races import installed, reads, refresh
from tests.test_priority_fallback import _settings
from tests.test_unavailable_campaign_details import campaign_data, metadata, response


@pytest.fixture
def state(tmp_path):
    twitch, _, _, _ = _make_cache_test_client()
    twitch.settings = _settings("Wanted")
    twitch.inventory, twitch._campaigns, twitch._drops = [], {}, {}
    twitch._state = State.CHANNEL_SWITCH
    twitch._state_change, twitch._schedule_changed = asyncio.Event(), asyncio.Event()
    twitch._games_update_pending = False
    twitch.get_auth = AsyncMock(return_value=SimpleNamespace(user_id=42))
    result = installed(twitch, tmp_path)
    twitch._inventory_service = result.service
    result.drop.update_minutes(60, source="websocket")
    yield result
    result.service.cancel_refresh_retry()


async def fresh_inventory(state, confirmed):
    calls = reads(state, minutes=60, claimed=confirmed)
    await refresh(state)
    assert len(calls) == 3 and not state.service.refresh_retry_pending
    current = state.twitch._drops[state.drop.id]
    assert current is not state.drop and current.is_claimed is confirmed
    assert current.progress_observation["source"] == "inventory"
    assert DropHistory(state.history_dir).total_count == int(confirmed)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["confirmed", "already_claimed", "unconfirmed", "request_error", "cancel", "unexpected"])
async def test_new_inventory_waits_for_a_retired_targets_result(state, outcome):
    twitch, old, drop, service = state.twitch, state.previous, state.drop, state.service
    entered, release, claims = asyncio.Event(), asyncio.Event(), []
    read_calls = reads(state, minutes=60)
    dispatch = twitch.gql_request

    async def request(operation):
        if not isinstance(operation, list) and operation["operationName"] == GQL_OPERATIONS["ClaimDrop"]["operationName"]:
            claims.append(operation)
            entered.set()
            await release.wait()
            if outcome == "request_error":
                raise GQLException("test-only request failure")
            if outcome == "unexpected":
                raise RuntimeError("test-only unexpected failure")
            if outcome == "unconfirmed":
                return {"data": None}
            return {"data": {"claimDropRewards": {"status": "DROP_INSTANCE_ALREADY_CLAIMED"
                                                   if outcome == "already_claimed" else "ELIGIBLE_FOR_ALL"}}}
        return await dispatch(operation)

    twitch.gql_request = request
    task = asyncio.create_task(drop.claim())
    observation = drop.progress_observation
    try:
        await asyncio.wait_for(entered.wait(), 2)
        service.clear_cached_state()
        assert twitch.inventory == [] and twitch._drops == {} and twitch._campaigns == {}
        assert twitch._claim_requests == 1 and old._pending_claims == {drop.id: 1}
        await refresh(state)
        assert twitch.inventory == [] and twitch._drops == {} and twitch._campaigns == {}
        assert service.refresh_retry_pending
        assert twitch._mnt_task is None
        twitch.gui.inv.clear.assert_called_once_with()
        twitch.gui.inv.add_campaign.assert_not_awaited()
        assert len(claims) == 1 and len(read_calls) == 3
        assert DropHistory(state.history_dir).total_count == 0
        if outcome == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            if outcome == "unexpected":
                with pytest.raises(RuntimeError, match="test-only unexpected failure"):
                    await task
            else:
                assert await task is (outcome in ("confirmed", "already_claimed"))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    confirmed = outcome in ("confirmed", "already_claimed")
    assert twitch._claim_requests == 0 and not old._pending_claims
    assert drop.is_claimed is confirmed and drop.progress_observation == observation
    assert DropHistory(state.history_dir).total_count == int(confirmed)
    assert len(claims) == 1
    await fresh_inventory(state, confirmed)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [GQL_OPERATIONS["Inventory"]["operationName"],
                                   GQL_OPERATIONS["Campaigns"]["operationName"], "details"])
@pytest.mark.parametrize("confirmed", [False, True])
async def test_result_settled_during_reads_still_requires_a_fresh_inventory(state, stage, confirmed):
    twitch, drop, service = state.twitch, state.drop, state.service
    entered, release, claims = asyncio.Event(), asyncio.Event(), []
    task = None

    async def settle(name):
        if name == stage:
            release.set()
            assert await task is confirmed

    read_calls = reads(state, minutes=60, during=settle)
    dispatch = twitch.gql_request

    async def request(operation):
        if not isinstance(operation, list) and operation["operationName"] == GQL_OPERATIONS["ClaimDrop"]["operationName"]:
            claims.append(operation)
            entered.set()
            await release.wait()
            return ({"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}}
                    if confirmed else {"data": None})
        return await dispatch(operation)

    twitch.gql_request = request
    task = asyncio.create_task(drop.claim())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        service.clear_cached_state()
        await refresh(state)
        assert task.done() and twitch._claim_requests == 0
        assert twitch.inventory == [] and twitch._drops == {} and twitch._campaigns == {}
        assert service.refresh_retry_pending
        assert len(claims) == 1 and len(read_calls) == 3
        assert DropHistory(state.history_dir).total_count == int(confirmed)
        assert drop.is_claimed is confirmed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await fresh_inventory(state, confirmed)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [GQL_OPERATIONS["Inventory"]["operationName"], "details"])
@pytest.mark.parametrize("confirmed", [False, True])
async def test_retired_target_attempt_started_and_finished_during_reads_retires_snapshot(state, stage, confirmed):
    twitch, drop, service = state.twitch, state.drop, state.service
    service.clear_cached_state()

    async def attempt(name):
        if name == stage:
            assert await drop.claim() is confirmed

    read_calls = reads(state, minutes=60, during=attempt)
    dispatch = twitch.gql_request

    async def request(operation):
        if not isinstance(operation, list) and operation["operationName"] == GQL_OPERATIONS["ClaimDrop"]["operationName"]:
            return ({"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}}
                    if confirmed else {"data": None})
        return await dispatch(operation)

    twitch.gql_request = request
    await refresh(state)
    assert twitch._claim_requests == 0 and not state.previous._pending_claims
    assert twitch.inventory == [] and twitch._drops == {} and twitch._campaigns == {}
    assert service.refresh_retry_pending and len(read_calls) == 3
    assert DropHistory(state.history_dir).total_count == int(confirmed)
    await fresh_inventory(state, confirmed)


@pytest.mark.asyncio
async def test_unrelated_context_epoch_change_does_not_block_inventory(state):
    state.service.clear_cached_state()

    async def update(name):
        if name == "details":
            state.twitch.invalidate_deadline_context()

    reads(state, minutes=60, during=update)
    await refresh(state)
    assert state.twitch._drops[state.drop.id] is not state.drop
    assert not state.service.refresh_retry_pending


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["pending", "settled", "admitted"])
@pytest.mark.parametrize("confirmed", [False, True])
async def test_actual_imported_claim_cannot_be_overtaken_by_cache_rebuild(tmp_path, monkeypatch, timing, confirmed):
    async with running_miner(tmp_path, monkeypatch) as (twitch, peer, _):
        twitch.settings.__dict__.update(vars(_settings("Wanted")))
        twitch._state, twitch._inventory_loaded = State.CHANNEL_SWITCH, True
        state = installed(twitch, tmp_path)
        service = twitch._inventory_service = state.service
        state.drop.update_minutes(60, source="websocket")
        entered, release = asyncio.Event(), asyncio.Event()
        task = None
        raw = campaign_data("known", minutes=60)

        def inventory(data):
            return {"data": {"currentUser": {"inventory": {
                "dropCampaignsInProgress": [deepcopy(data)], "gameEventDrops": [],
            }}}}

        confirmation = ({"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}}
                        if confirmed else {"data": None})
        catalog = {"data": {"currentUser": {"dropCampaigns": [metadata(raw)]}}}
        peer.responses.extend(([confirmation, inventory(raw)] if timing != "admitted"
                               else [inventory(raw), confirmation]) + [catalog, [response(deepcopy(raw))]])
        dispatch = twitch.gql_request

        async def query(operation):
            result = await dispatch(operation)
            assert not twitch._browser._lock.locked()
            if not isinstance(operation, list):
                if operation["operationName"] == GQL_OPERATIONS["ClaimDrop"]["operationName"] and timing != "admitted":
                    entered.set()
                    await release.wait()
                elif operation["operationName"] == GQL_OPERATIONS["Inventory"]["operationName"]:
                    if timing == "settled":
                        release.set()
                        assert await task is confirmed
                    elif timing == "admitted":
                        assert await state.drop.claim() is confirmed
            return result

        twitch.gql_request = query
        try:
            if timing != "admitted":
                task = asyncio.create_task(state.drop.claim())
                await asyncio.wait_for(entered.wait(), 2)
            service.clear_cached_state()
            await refresh(state)
            assert twitch.inventory == [] and twitch._drops == {} and twitch._campaigns == {}
            assert service.refresh_retry_pending and len(peer.calls) == 4
            twitch.gui.inv.clear.assert_called_once_with()
            twitch.gui.inv.add_campaign.assert_not_awaited()
            if task is not None:
                release.set()
                assert await asyncio.wait_for(task, 2) is confirmed
            assert twitch._claim_requests == 0 and not state.previous._pending_claims
            assert DropHistory(tmp_path).total_count == int(confirmed)
            raw["timeBasedDrops"][0]["self"]["isClaimed"] = confirmed
            peer.responses.extend([inventory(raw), catalog, [response(deepcopy(raw))]])
            twitch.gql_request = dispatch
            await refresh(state)
            assert not service.refresh_retry_pending
            current = twitch._drops[state.drop.id]
            assert current is not state.drop and current.is_claimed is confirmed
            assert current.progress_observation["source"] == "inventory"
            assert DropHistory(tmp_path).total_count == int(confirmed)
            assert len(peer.calls) == 7 and not peer.responses
            assert sum(not isinstance(operation, list)
                       and operation["operationName"] == GQL_OPERATIONS["ClaimDrop"]["operationName"]
                       for operation in peer.calls) == 1
            assert not twitch._browser._lock.locked()
            assert twitch._browser.status()["state"] == "ready"
            assert twitch._browser.status()["generation"] == 1
        finally:
            service.cancel_refresh_retry()
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
