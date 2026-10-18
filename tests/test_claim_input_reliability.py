"""Claim admission rejects unusable metadata without inventing success or requests."""

from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.exceptions import MinerException
from src.models.drop import BaseDrop, TimedDrop
from src.services.inventory_service import InventoryService
from src.services.message_handlers import MessageHandlerService
from src.web.managers.inventory import InventoryManager
from tests.test_claim_followup_reliability import claiming as claiming
from tests.test_claim_response_shapes import reward as reward
from tests.test_claim_response_shapes import state
from tests.test_imported_session_retry import running_miner
from tests.test_stale_progress import mining_state as mining_state
from tests.test_watch_drop_filtering import _drop


INVALID_IDS = [None, False, True, 0, 1, 1.5, "", " \t\r\n", [], ["private-claim"], {}, {"private-claim": 1}]
INVALID_SELF = [None, False, 0, "private-claim", [], {}, {"isClaimed": False},
                {"dropInstanceID": None},
                *({"dropInstanceID": None, "isClaimed": value} for value in (None, 0, 1, "false", "true", [], {})),
                *({"dropInstanceID": value, "isClaimed": False} for value in INVALID_IDS[1:])]


@pytest.mark.parametrize("kind", [BaseDrop, TimedDrop])
@pytest.mark.parametrize("account", INVALID_SELF)
def test_unusable_inventory_claim_metadata_is_refused_before_model_creation(mining_state, kind, account):
    _, _, campaign = mining_state
    data = _drop("new", "New", 60)
    data["self"] = deepcopy(account)
    original = deepcopy(data)
    with pytest.raises(MinerException, match="^Invalid drop claim state$"):
        kind(campaign, data, {})
    assert data == original


@pytest.mark.parametrize("claim_id", INVALID_IDS)
def test_live_id_rejection_keeps_known_id_and_revision(mining_state, claim_id):
    _, _, campaign = mining_state
    drop = campaign.timed_drops["drop"]
    before = state(drop), campaign.progress_revision
    assert drop.update_claim(claim_id) is False
    assert (state(drop), campaign.progress_revision) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_id", INVALID_IDS)
async def test_directly_assigned_unusable_id_cannot_dispatch(reward, claim_id):
    drop, twitch = reward
    drop.claim_id = claim_id
    before = state(drop), drop.campaign.progress_revision
    assert not drop.can_claim
    assert await drop.claim() is False
    assert (state(drop), drop.campaign.progress_revision) == before
    twitch.gql_request.assert_not_awaited()
    twitch.drop_history.record.assert_not_called()
    twitch.request_games_update.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("claim_id", INVALID_IDS)
async def test_bad_websocket_claim_preserves_id_without_wait_or_publication(claiming, claim_id, missing, caplog):
    state = claiming
    event = deepcopy(state.event)
    if missing:
        del event["data"]["drop_instance_id"]
    else:
        event["data"]["drop_instance_id"] = deepcopy(claim_id)
    before = state.drop.claim_id, state.campaign.progress_revision
    state.drop.claim = AsyncMock(wraps=state.drop.claim)
    state.twitch.gql_request = AsyncMock()
    await MessageHandlerService(state.twitch).process_drops(42, event)
    assert (state.drop.claim_id, state.campaign.progress_revision) == before
    state.drop.claim.assert_not_awaited()
    state.twitch.gql_request.assert_not_awaited()
    state.twitch.gui.inv.update_drop.assert_not_called()
    state.twitch.restart_watching.assert_not_called()
    state.twitch.request_inventory_refresh.assert_not_called()
    state.timing.sleep.assert_not_awaited()
    assert state.twitch.drop_history.total_count == 0
    assert "private-claim" not in caplog.text and "Exception in" not in caplog.text


@pytest.mark.parametrize("claim_id", [None, "opaque", " opaque # / claim "])
@pytest.mark.parametrize("claimed", [False, True])
def test_valid_inventory_metadata_preserves_ids_and_claim_flags(mining_state, claim_id, claimed):
    _, _, campaign = mining_state
    data = _drop("new", "New", 60)
    data["self"] = {"isClaimed": claimed, "dropInstanceID": claim_id, "currentMinutesWatched": 7}
    original = deepcopy(data)
    drop = TimedDrop(campaign, data, {})
    assert drop.is_claimed is claimed and drop.claim_id == claim_id
    assert drop.real_current_minutes == (60 if claimed else 7)
    assert drop.can_claim is (not claimed and claim_id is not None)
    assert data == original


@pytest.mark.asyncio
@pytest.mark.parametrize("account", INVALID_SELF)
async def test_bad_inventory_does_not_clear_published_models_or_gui(mining_state, account):
    twitch, _, campaign = mining_state
    twitch.inventory = [campaign]
    twitch._campaigns = {campaign.id: campaign}
    twitch._mnt_triggers = deque([campaign.ends_at])
    twitch._mnt_task = MagicMock()
    twitch._maintenance_service = MagicMock()
    broadcaster = MagicMock(emit=AsyncMock())
    manager = InventoryManager(broadcaster, MagicMock())
    await manager.add_campaign(campaign)
    manager.clear = MagicMock(wraps=manager.clear)
    manager.add_campaign = AsyncMock(wraps=manager.add_campaign)
    twitch.gui.inv = manager
    published = deepcopy(manager.get_campaigns())
    broadcaster.emit.reset_mock()
    containers = twitch.inventory, twitch._campaigns, twitch._drops, twitch._mnt_triggers
    data = {
        "id": "new", "name": "New", "game": {"id": "1", "name": "Game"},
        "self": {"isAccountConnected": True}, "accountLinkURL": "https://example.test/link",
        "startAt": "2000-01-01T00:00:00Z", "endAt": "2099-01-01T00:00:00Z",
        "status": "ACTIVE", "allow": {"channels": [], "isEnabled": True},
        "timeBasedDrops": [{**_drop("new", "New", 60), "self": deepcopy(account)}],
    }
    twitch.gql_request = AsyncMock(side_effect=[
        {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": [data], "gameEventDrops": []}}}},
        {"data": {"currentUser": {"dropCampaigns": []}}},
    ])
    with pytest.raises(MinerException, match="^Invalid drop claim state$"):
        await InventoryService(twitch).fetch_inventory()
    assert twitch.inventory == [campaign] and twitch._campaigns == {campaign.id: campaign}
    assert twitch._drops == campaign.timed_drops and list(twitch._mnt_triggers) == [campaign.ends_at]
    assert all(current is previous for current, previous in zip(
        (twitch.inventory, twitch._campaigns, twitch._drops, twitch._mnt_triggers), containers, strict=True,
    ))
    twitch.gui.inv.clear.assert_not_called()
    twitch.gui.inv.add_campaign.assert_not_called()
    assert manager.get_campaigns() == published
    broadcaster.emit.assert_not_awaited()
    twitch._mnt_task.cancel.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_drop_does_not_log_claim_payload(mining_state, caplog):
    twitch, _, _ = mining_state
    await MessageHandlerService(twitch).process_drops(42, {
        "type": "drop-claim", "data": {"drop_id": "unknown", "drop_instance_id": "private-claim"},
    })
    assert "private-claim" not in caplog.text


@pytest.mark.parametrize("awarded", [False, True])
def test_absent_self_keeps_benefit_inference_without_claim_id(mining_state, awarded):
    _, _, campaign = mining_state
    data = _drop("new", "New", 60)
    benefits = {"benefit-new": datetime.now(timezone.utc)} if awarded else {}
    drop = TimedDrop(campaign, data, benefits)
    assert drop.is_claimed is awarded and drop.claim_id is None
    assert drop.real_current_minutes == (60 if awarded else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_id", ["opaque-id", " opaque # / claim "])
async def test_real_imported_dispatch_keeps_exact_opaque_id_and_single_claim(claiming, tmp_path, monkeypatch, claim_id):
    state = claiming
    state.twitch.watching_channel.clear()
    state.event["data"]["drop_instance_id"] = claim_id
    async with running_miner(tmp_path / "session", monkeypatch) as (client, peer, _):
        peer.responses.append({"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}})
        state.twitch.gql_request = client.gql_request
        service = MessageHandlerService(state.twitch)
        await service.process_drops(42, state.event)
        await service.process_drops(42, state.event)
        assert len(peer.calls) == 1 and not peer.responses
        assert peer.calls[0]["variables"] == {"input": {"dropInstanceID": claim_id}}
        assert state.drop.claim_id == claim_id and state.drop.is_claimed
        assert state.campaign.progress_revision == 1
        assert state.twitch.drop_history.total_count == 1
        assert client._browser.status()["state"] == "ready"
