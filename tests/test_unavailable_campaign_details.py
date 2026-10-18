"""Unavailable campaign details must not abort an otherwise usable refresh."""

import asyncio
from collections import deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api import GQLClient
from src.config import ClientType, State
from src.exceptions import GQLException
from src.models.campaign import DropsCampaign
from src.services.inventory_service import InventoryService
from tests.test_priority_fallback import _settings
from tests.test_watch_drop_filtering import _drop


def campaign_data(key, *, minutes=0):
    now = datetime.now(timezone.utc)
    drop = _drop(f"{key}-drop", key, 60)
    drop["self"] = {"dropInstanceID": f"claim-{key}", "isClaimed": False, "currentMinutesWatched": minutes}
    return {
        "id": key, "name": key, "status": "ACTIVE",
        "game": {"id": "1", "name": "Wanted"},
        "self": {"isAccountConnected": True}, "accountLinkURL": "https://example.test/link",
        "startAt": (now - timedelta(days=1)).isoformat(),
        "endAt": (now + timedelta(days=1)).isoformat(),
        "allow": {"channels": [], "isEnabled": True}, "timeBasedDrops": [drop],
    }


def metadata(data):
    return {key: deepcopy(data[key]) for key in ("id", "name", "game", "status")}


def response(data):
    return {"data": {"user": {"dropCampaign": data}}}


def client(*, ongoing=(), available=(), details=()):
    twitch = MagicMock()
    twitch.settings = _settings("Wanted")
    twitch.get_auth = AsyncMock(return_value=SimpleNamespace(user_id=42))
    twitch._drops, twitch._campaigns, twitch.inventory = {}, {}, []
    twitch._mnt_triggers, twitch._mnt_task = deque(), None
    twitch._state = State.INVENTORY_FETCH
    twitch.gui.inv.add_campaign = AsyncMock()
    twitch._maintenance_service.run_maintenance_task = AsyncMock()
    twitch.gql_request = AsyncMock(side_effect=[
        {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": list(ongoing), "gameEventDrops": []}}}},
        {"data": {"currentUser": {"dropCampaigns": list(available)}}},
        list(details),
    ])
    return twitch


@pytest.mark.asyncio
async def test_mixed_detail_batch_returns_only_available_campaigns():
    good, missing = campaign_data("good"), campaign_data("missing")
    twitch = client()
    twitch.gql_request = AsyncMock(return_value=[response(good), response(None)])
    result = await InventoryService(twitch).fetch_campaigns([
        (good["id"], metadata(good)), (missing["id"], metadata(missing)),
    ])
    assert set(result) == {"good"}
    assert result["good"]["timeBasedDrops"] == good["timeBasedDrops"]


@pytest.mark.asyncio
async def test_one_unavailable_new_campaign_does_not_block_real_inventory_refresh():
    good, missing = campaign_data("good", minutes=7), campaign_data("missing")
    twitch = client(available=[metadata(good), metadata(missing)], details=[response(good), response(None)])
    await InventoryService(twitch).fetch_inventory()
    await twitch._mnt_task
    assert set(twitch._campaigns) == {"good"}
    assert set(twitch._drops) == {"good-drop"}
    assert twitch._drops["good-drop"].real_current_minutes == 7
    twitch.gui.inv.add_campaign.assert_awaited_once()


@pytest.mark.asyncio
async def test_in_progress_data_survives_unavailable_details_with_catalog_metadata():
    known = campaign_data("known", minutes=17)
    available = metadata(known)
    del known["status"]
    twitch = client(ongoing=[known], available=[available], details=[response(None)])
    await InventoryService(twitch).fetch_inventory()
    await twitch._mnt_task
    assert set(twitch._campaigns) == {"known"}
    drop = twitch._drops["known-drop"]
    assert drop.real_current_minutes == 17 and drop.claim_id == "claim-known"
    assert not drop.is_claimed


@pytest.mark.asyncio
async def test_empty_chunk_does_not_authenticate_or_send_empty_batch():
    twitch = client()
    assert await InventoryService(twitch).fetch_campaigns([]) == {}
    twitch.get_auth.assert_not_awaited()
    twitch.gql_request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bare", [False, True])
async def test_single_campaign_accepts_list_or_single_response_without_mutating_sources(bare):
    data = campaign_data("one", minutes=9)
    catalog = metadata(data)
    catalog["name"] = "Catalog name"
    payload = response(data)
    payload = payload if bare else [payload]
    before = deepcopy((catalog, payload))
    twitch = client()
    twitch.gql_request = AsyncMock(return_value=payload)
    result = await InventoryService(twitch).fetch_campaigns([("one", catalog)])
    assert result["one"]["name"] == "Catalog name"
    assert result["one"]["timeBasedDrops"][0]["self"]["currentMinutesWatched"] == 9
    assert (catalog, payload) == before


@pytest.mark.asyncio
async def test_all_unavailable_details_do_not_create_catalog_only_placeholders():
    data = campaign_data("missing")
    twitch = client(available=[metadata(data)], details=[response(None)])
    await InventoryService(twitch).fetch_inventory()
    await twitch._mnt_task
    assert twitch.inventory == [] and twitch._campaigns == {} and twitch._drops == {}
    twitch.gui.inv.add_campaign.assert_not_awaited()


@pytest.mark.asyncio
async def test_returned_ids_not_response_order_match_catalog_metadata():
    first, second = campaign_data("first"), campaign_data("second")
    twitch = client()
    twitch.gql_request = AsyncMock(return_value=[response(second), response(first)])
    result = await InventoryService(twitch).fetch_campaigns([
        ("first", metadata(first)), ("second", metadata(second)),
    ])
    assert set(result) == {"first", "second"}
    assert result["first"]["timeBasedDrops"][0]["id"] == "first-drop"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    None, {}, {"data": None}, {"data": {}}, {"data": {"user": None}},
    {"data": {"user": {}}}, response([]), response({}), response({"id": False}),
    response({"id": ["one"]}),
])
async def test_malformed_response_is_not_disguised_as_unavailable_campaign(payload):
    twitch = client()
    twitch.gql_request = AsyncMock(return_value=payload)
    with pytest.raises(GQLException, match="^Invalid campaign details response$"):
        await InventoryService(twitch).fetch_campaigns([("one", metadata(campaign_data("one")))])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["missing", "excess", "duplicate", "foreign"])
async def test_incomplete_or_unexpected_batches_are_rejected_with_fixed_error(kind):
    first, second = campaign_data("first"), campaign_data("second")
    if kind == "missing":
        replies = [response(first)]
    elif kind == "excess":
        replies = [response(first), response(second), response(None)]
    elif kind == "duplicate":
        replies = [response(first), response(first)]
    else:
        replies = [response(first), response({"id": "private-canary-value"})]
    twitch = client()
    twitch.gql_request = AsyncMock(return_value=replies)
    with pytest.raises(GQLException) as caught:
        await InventoryService(twitch).fetch_campaigns([("first", metadata(first)), ("second", metadata(second))])
    assert str(caught.value) == "Invalid campaign details response"
    assert "private-canary-value" not in str(caught.value)


@pytest.mark.asyncio
async def test_failed_refresh_preserves_previous_live_inventory_and_does_not_publish_partial_result():
    data = campaign_data("new")
    twitch = client(available=[metadata(data)], details=[{"data": {"user": None}}])
    previous = DropsCampaign(twitch, campaign_data("previous"), {})
    twitch.inventory[:] = [previous]
    twitch._campaigns["previous"] = previous
    twitch._drops["previous-drop"] = previous
    with pytest.raises(GQLException):
        await InventoryService(twitch).fetch_inventory()
    assert twitch.inventory == [previous]
    assert twitch._campaigns == {"previous": previous} and twitch._drops == {"previous-drop": previous}
    twitch.gui.inv.clear.assert_not_called()
    twitch.gui.inv.add_campaign.assert_not_awaited()


@pytest.mark.asyncio
async def test_fresh_inventory_progress_and_claim_state_keep_priority_over_details():
    known = campaign_data("known", minutes=19)
    details = campaign_data("known", minutes=0)
    details["self"]["isAccountConnected"] = False
    catalog = metadata(known)
    del known["status"]
    twitch = client(ongoing=[known], available=[catalog], details=[response(details)])
    await InventoryService(twitch).fetch_inventory()
    await twitch._mnt_task
    assert twitch._campaigns["known"].linked is True
    assert twitch._drops["known-drop"].real_current_minutes == 19
    assert twitch._drops["known-drop"].claim_id == "claim-known"


@pytest.mark.asyncio
async def test_known_expired_claim_is_not_lost_when_details_are_unavailable():
    known = campaign_data("known", minutes=60)
    catalog = metadata(known)
    ended = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    known["endAt"] = known["timeBasedDrops"][0]["endAt"] = ended
    del known["status"]
    twitch = client(ongoing=[known], available=[catalog], details=[response(None)])
    await InventoryService(twitch).fetch_inventory()
    await twitch._mnt_task
    drop = twitch._drops["known-drop"]
    assert drop.claim_id == "claim-known" and drop.can_claim
    assert not drop.is_claimed and not drop.campaign.active


@pytest.mark.asyncio
async def test_complete_retained_inventory_needs_no_catalog_or_detail_lookup():
    known = campaign_data("known", minutes=60)
    known["status"] = "EXPIRED"
    ended = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    known["endAt"] = known["timeBasedDrops"][0]["endAt"] = ended
    twitch = client(ongoing=[known])
    await InventoryService(twitch).fetch_inventory()
    await twitch._mnt_task
    assert twitch._drops["known-drop"].claim_id == "claim-known"
    assert twitch._drops["known-drop"].can_claim and not twitch._campaigns["known"].active
    assert twitch.gql_request.await_count == 2
    twitch.get_auth.assert_not_awaited()


@pytest.mark.asyncio
async def test_unavailable_campaign_is_retried_on_next_normal_refresh():
    data = campaign_data("one")
    twitch = client(available=[metadata(data)], details=[response(None)])
    service = InventoryService(twitch)
    await service.fetch_inventory()
    await twitch._mnt_task
    assert twitch.inventory == []
    twitch.gql_request.side_effect = [
        {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": [], "gameEventDrops": []}}}},
        {"data": {"currentUser": {"dropCampaigns": [metadata(data)]}}},
        [response(data)],
    ]
    await service.fetch_inventory()
    await twitch._mnt_task
    assert set(twitch._campaigns) == {"one"}


@pytest.mark.asyncio
async def test_cancelled_detail_request_still_propagates_cancellation():
    twitch = client()
    twitch.gql_request = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await InventoryService(twitch).fetch_campaigns([("one", metadata(campaign_data("one")))])


@pytest.mark.asyncio
async def test_real_gql_server_error_nullification_reaches_inventory_without_crashing():
    good, missing = campaign_data("good"), campaign_data("missing")
    bodies = [
        {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": [], "gameEventDrops": []}}}},
        {"data": {"currentUser": {"dropCampaigns": [metadata(good), metadata(missing)]}}},
        [response(good), {**response(missing), "errors": [{"message": "server error", "path": ["user", "dropCampaign"]}]}],
    ]

    class Request:
        async def __aenter__(self):
            return SimpleNamespace(json=AsyncMock(return_value=deepcopy(bodies.pop(0))))

        async def __aexit__(self, *args):
            return False

    http = SimpleNamespace(request=MagicMock(side_effect=lambda *args, **kwargs: Request()))
    auth = SimpleNamespace(validate=AsyncMock(return_value=SimpleNamespace(
        browser_active=False, headers=MagicMock(return_value={}),
    )))
    twitch = client()
    twitch.gql_request = GQLClient(http, auth, ClientType.WEB).request
    await InventoryService(twitch).fetch_inventory()
    await twitch._mnt_task
    assert set(twitch._campaigns) == {"good"}
    assert http.request.call_count == 3


@pytest.mark.asyncio
async def test_one_unavailable_campaign_does_not_block_other_detail_chunks():
    data = {f"campaign-{key}": campaign_data(f"campaign-{key}") for key in range(21)}
    twitch = client()

    async def request(operations):
        if isinstance(operations, list):
            await asyncio.sleep(0)
            return [response(None if op["variables"]["dropID"] == "campaign-3" else data[op["variables"]["dropID"]])
                    for op in operations]
        if operations["operationName"] == "Inventory":
            return {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": [], "gameEventDrops": []}}}}
        return {"data": {"currentUser": {"dropCampaigns": [metadata(item) for item in data.values()]}}}

    twitch.gql_request = AsyncMock(side_effect=request)
    await InventoryService(twitch).fetch_inventory()
    await twitch._mnt_task
    assert set(twitch._campaigns) == set(data) - {"campaign-3"}
    assert twitch.gui.inv.add_campaign.await_count == 20
