"""Invalid watch requirements must be rejected before inventory installation."""

import asyncio
from collections import deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import State
from src.exceptions import MinerException
from src.services.inventory_service import InventoryService
from tests.test_cache_clear import _make_cache_test_client
from tests.test_watch_drop_filtering import _campaign, _drop


INVALID_REQUIREMENTS = (None, True, False, -1, -60, 1.0, 1.5, "60", "", "private-canary", [], {}, float("inf"), float("nan"))


def campaign_data(required):
    now = datetime.now(timezone.utc)
    return {
        "id": "new", "name": "New", "status": "ACTIVE",
        "game": {"id": "1", "name": "Game A"},
        "self": {"isAccountConnected": True}, "accountLinkURL": "https://example.test/link",
        "startAt": (now - timedelta(days=1)).isoformat(),
        "endAt": (now + timedelta(days=1)).isoformat(),
        "allow": {"channels": [], "isEnabled": True},
        "timeBasedDrops": [_drop("new-drop", "New Reward", required)],
    }


def refresh_client(required):
    twitch, settings, auth, maintenance = _make_cache_test_client()
    data = campaign_data(required)
    twitch._state = State.INVENTORY_FETCH
    twitch.gui.status = SimpleNamespace(update=MagicMock())
    twitch.gui.inv.add_campaign = AsyncMock()
    twitch._maintenance_service = SimpleNamespace(run_maintenance_task=AsyncMock())
    twitch.get_auth = AsyncMock(return_value=SimpleNamespace(user_id=42))
    twitch.gql_request = AsyncMock(side_effect=[
        {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": [], "gameEventDrops": []}}}},
        {"data": {"currentUser": {"dropCampaigns": [{key: data[key] for key in ("id", "game", "status")}]}}},
        [{"data": {"user": {"dropCampaign": data}}}],
    ])
    return twitch, settings, auth, maintenance


@pytest.mark.parametrize("required", INVALID_REQUIREMENTS)
def test_invalid_watch_requirement_is_rejected_without_echoing_or_mutating_it(required):
    data = _drop("bad-drop", "Bad", required)
    before = repr(data)
    with pytest.raises(MinerException, match="^Invalid drop watch requirement$"):
        _campaign("bad", [data])
    assert repr(data) == before


def test_missing_watch_requirement_is_not_invented_as_zero():
    data = _drop("bad-drop", "Bad", 60)
    del data["requiredMinutesWatched"]
    with pytest.raises(MinerException, match="^Invalid drop watch requirement$"):
        _campaign("bad", [data])


@pytest.mark.parametrize("required", [0, 1, 15, 60, 2_147_483_647])
@pytest.mark.parametrize("claimed", [False, True])
def test_valid_integer_requirements_keep_claim_and_progress_semantics(required, claimed):
    data = _drop("valid", "Valid", required)
    data["self"] = {"dropInstanceID": "claim-valid", "isClaimed": claimed, "currentMinutesWatched": 0}
    before = deepcopy(data)
    drop = _campaign("valid", [data]).timed_drops["valid"]
    assert type(drop.required_minutes) is int and drop.required_minutes == required
    assert drop.is_watch_drop is (required > 0)
    assert drop.is_claimed is claimed and drop.claim_id == "claim-valid"
    assert drop.real_current_minutes == (required if claimed else 0)
    assert drop.extra_current_minutes == 0
    assert data == before


def test_invalid_requirement_is_rejected_even_when_claimed():
    data = _drop("bad-drop", "Bad", None)
    data["self"] = {"dropInstanceID": "claim-known", "isClaimed": True, "currentMinutesWatched": 60}
    with pytest.raises(MinerException, match="^Invalid drop watch requirement$"):
        _campaign("bad", [data])


def test_valid_subscription_and_watch_rewards_remain_distinct():
    campaign = _campaign("mixed", [_drop("sub", "Subscribe", 0), _drop("watch", "Watch", 30)])
    assert campaign.total_drops == 1
    assert campaign.timed_drops["sub"].is_watch_drop is False
    assert not campaign.timed_drops["sub"].is_claimed
    assert [drop.id for drop in campaign.watch_drops] == ["watch"]


def test_requirement_validation_does_not_clamp_existing_counter_estimates():
    drop = _campaign("valid", [_drop("valid", "Valid", 60)]).timed_drops["valid"]
    drop.real_current_minutes = 5
    drop.extra_current_minutes = -3
    assert drop.required_minutes == 60 and drop.current_minutes == 2
    assert drop.remaining_minutes == 58


@pytest.mark.asyncio
@pytest.mark.parametrize("required", INVALID_REQUIREMENTS)
async def test_invalid_requirement_failure_preserves_previous_live_state(required):
    twitch, settings, auth, maintenance = refresh_client(required)
    inventory, campaigns, drops, triggers = twitch.inventory[:], twitch._campaigns.copy(), twitch._drops.copy(), deque(twitch._mnt_triggers)
    containers = (twitch.inventory, twitch._campaigns, twitch._drops, twitch._mnt_triggers)
    settings_before = deepcopy(settings)
    watched = twitch.watching_channel.get_with_default(None)
    manual = (twitch._manual_target_channel, twitch._manual_target_game)
    try:
        try:
            await InventoryService(twitch).fetch_inventory()
        except (MinerException, TypeError, ValueError):
            pass
        else:
            pytest.fail("Invalid requirement was installed")
        assert twitch.inventory == inventory
        assert twitch._campaigns == campaigns and twitch._drops == drops
        assert twitch._mnt_triggers == triggers
        assert all(current is previous for current, previous in zip(
            (twitch.inventory, twitch._campaigns, twitch._drops, twitch._mnt_triggers), containers, strict=True,
        ))
        assert twitch.settings is settings and settings == settings_before
        assert twitch._auth_state is auth
        assert twitch.watching_channel.get_with_default(None) is watched
        assert (twitch._manual_target_channel, twitch._manual_target_game) == manual
        twitch.gui.inv.clear.assert_not_called()
        twitch.gui.inv.add_campaign.assert_not_awaited()
        maintenance.cancel.assert_not_called()
        assert twitch._mnt_task is maintenance
        assert auth.mock_calls == []
    finally:
        if isinstance(twitch._mnt_task, asyncio.Task):
            twitch._mnt_task.cancel()
            await asyncio.gather(twitch._mnt_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("required", [0, 1, 60])
async def test_valid_requirement_allows_complete_inventory_replacement(required):
    twitch, settings, auth, maintenance = refresh_client(required)
    try:
        await InventoryService(twitch).fetch_inventory()
        await twitch._mnt_task
        assert set(twitch._drops) == {"new-drop"} and set(twitch._campaigns) == {"new"}
        assert twitch._drops["new-drop"].required_minutes == required
        assert twitch._drops["new-drop"].is_watch_drop is (required > 0)
        assert twitch.settings is settings and twitch._auth_state is auth
        twitch.gui.inv.clear.assert_called_once()
        twitch.gui.inv.add_campaign.assert_awaited_once()
        maintenance.cancel.assert_called_once()
    finally:
        if isinstance(twitch._mnt_task, asyncio.Task):
            twitch._mnt_task.cancel()
            await asyncio.gather(twitch._mnt_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_invalid_sibling_campaign_cannot_publish_a_partial_replacement():
    twitch, _, _, maintenance = refresh_client(None)
    good, bad = campaign_data(60), campaign_data(None)
    good["id"] = good["timeBasedDrops"][0]["id"] = "good"
    twitch.gql_request.side_effect = [
        {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": [], "gameEventDrops": []}}}},
        {"data": {"currentUser": {"dropCampaigns": [
            {key: item[key] for key in ("id", "game", "status")} for item in (good, bad)
        ]}}},
        [{"data": {"user": {"dropCampaign": item}}} for item in (good, bad)],
    ]
    inventory, campaigns, drops = twitch.inventory[:], twitch._campaigns.copy(), twitch._drops.copy()
    with pytest.raises(MinerException, match="^Invalid drop watch requirement$"):
        await InventoryService(twitch).fetch_inventory()
    assert twitch.inventory == inventory and twitch._campaigns == campaigns and twitch._drops == drops
    twitch.gui.inv.clear.assert_not_called()
    twitch.gui.inv.add_campaign.assert_not_awaited()
    maintenance.cancel.assert_not_called()


@pytest.mark.asyncio
async def test_malformed_primary_inventory_requirement_is_not_hidden_by_valid_details():
    twitch, _, _, maintenance = refresh_client(60)
    known, details = campaign_data(None), campaign_data(60)
    known["timeBasedDrops"][0]["self"] = {
        "dropInstanceID": "claim-known", "isClaimed": False, "currentMinutesWatched": 17,
    }
    before = deepcopy(known)
    twitch.gql_request.side_effect = [
        {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": [known], "gameEventDrops": []}}}},
        {"data": {"currentUser": {"dropCampaigns": [{key: details[key] for key in ("id", "game", "status")}]}}},
        [{"data": {"user": {"dropCampaign": details}}}],
    ]
    inventory, campaigns, drops = twitch.inventory[:], twitch._campaigns.copy(), twitch._drops.copy()
    with pytest.raises(MinerException, match="^Invalid drop watch requirement$"):
        await InventoryService(twitch).fetch_inventory()
    assert twitch.inventory == inventory and twitch._campaigns == campaigns and twitch._drops == drops
    assert known == before
    twitch.gui.inv.clear.assert_not_called()
    twitch.gui.inv.add_campaign.assert_not_awaited()
    maintenance.cancel.assert_not_called()
