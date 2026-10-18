"""Slow refresh work must not renew an older Twitch progress report."""

import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.api import GQLClient
from src.config import GQL_OPERATIONS, ClientType
from src.models.campaign import DropsCampaign
from src.services.inventory_service import InventoryService, _ProgressReceipts
from tests.test_imported_session import bundle_data, catalog, imported, transport
from tests.test_unavailable_campaign_details import campaign_data, client, metadata, response


FIRST = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
LATER = FIRST + timedelta(minutes=10)


@pytest.fixture
def clock(monkeypatch):
    current = [FIRST]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0].replace(tzinfo=None) if tz is None else current[0].astimezone(tz)

    for module in ("src.models.drop", "src.models.campaign", "src.services.inventory_service",
                   "src.utils.gql_response_observer"):
        monkeypatch.setattr(f"{module}.datetime", Clock)
    return current


async def refresh(twitch):
    try:
        await InventoryService(twitch).fetch_inventory()
    finally:
        if isinstance(twitch._mnt_task, asyncio.Task):
            await twitch._mnt_task


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_details", [False, True])
@pytest.mark.parametrize("minutes", [0, 17])
@pytest.mark.parametrize("claimed", [False, True])
async def test_inventory_report_keeps_receipt_time_after_slow_details(clock, missing_details, minutes, claimed):
    primary, secondary = campaign_data("one", minutes=minutes), campaign_data("one", minutes=24)
    primary["timeBasedDrops"][0]["self"]["isClaimed"] = claimed
    catalog = metadata(primary)
    before = deepcopy((primary, secondary, catalog))
    twitch = client()

    async def request(operation):
        if isinstance(operation, list):
            clock[0] = LATER
            return [response(None if missing_details else secondary)]
        if operation["operationName"] == GQL_OPERATIONS["Inventory"]["operationName"]:
            return {"data": {"currentUser": {"inventory": {
                "dropCampaignsInProgress": [primary], "gameEventDrops": [],
            }}}}
        return {"data": {"currentUser": {"dropCampaigns": [catalog]}}}

    twitch.gql_request = AsyncMock(side_effect=request)
    await refresh(twitch)
    drop = twitch._drops["one-drop"]
    assert drop.real_current_minutes == (60 if claimed else minutes)
    assert drop.is_claimed is claimed
    assert drop.progress_observation == {
        "minutes": minutes, "source": "inventory", "received_at": FIRST.isoformat(),
    }
    assert (primary, secondary, catalog) == before


@pytest.mark.asyncio
async def test_early_details_batch_is_not_renewed_by_later_batch(clock):
    records = [campaign_data(str(index), minutes=index) for index in range(21)]
    twitch = client()
    first_done = asyncio.Event()

    async def request(operation):
        if isinstance(operation, list):
            ids = [item["variables"]["dropID"] for item in operation]
            if "0" in ids:
                first_done.set()
            else:
                await first_done.wait()
                clock[0] = LATER
            return [response(records[int(key)]) for key in ids]
        if operation["operationName"] == GQL_OPERATIONS["Inventory"]["operationName"]:
            return {"data": {"currentUser": {"inventory": {
                "dropCampaignsInProgress": [], "gameEventDrops": [],
            }}}}
        return {"data": {"currentUser": {"dropCampaigns": [metadata(item) for item in records]}}}

    twitch.gql_request = AsyncMock(side_effect=request)
    await refresh(twitch)
    assert len(twitch._drops) == 21
    assert twitch._drops["0-drop"].progress_observation["received_at"] == FIRST.isoformat()
    assert twitch._drops["20-drop"].progress_observation["received_at"] == LATER.isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["inventory", "catalog", "details"])
async def test_timestamp_follows_selected_drop_rows_not_campaign_metadata(clock, source):
    data = campaign_data("one", minutes=7)
    catalog = metadata(data)
    primary = deepcopy(data)
    if source != "inventory":
        del primary["timeBasedDrops"]
    if source == "catalog":
        catalog["timeBasedDrops"] = deepcopy(data["timeBasedDrops"])
    twitch = client()
    middle = FIRST + timedelta(minutes=5)

    async def request(operation):
        if isinstance(operation, list):
            clock[0] = LATER
            return [response(data)]
        if operation["operationName"] == GQL_OPERATIONS["Inventory"]["operationName"]:
            return {"data": {"currentUser": {"inventory": {
                "dropCampaignsInProgress": [primary], "gameEventDrops": [],
            }}}}
        clock[0] = middle
        return {"data": {"currentUser": {"dropCampaigns": [catalog]}}}

    twitch.gql_request = AsyncMock(side_effect=request)
    await refresh(twitch)
    expected = {"inventory": FIRST, "catalog": middle, "details": LATER}[source]
    assert twitch._drops["one-drop"].progress_observation == {
        "minutes": 7, "source": "inventory", "received_at": expected.isoformat(),
    }


@pytest.mark.parametrize("minutes", [None, True, False, "7", -1, 61, 1.5, "missing"])
def test_bad_or_missing_minutes_remain_unknown_with_a_receipt(clock, minutes):
    data = campaign_data("one", minutes=minutes)
    if minutes == "missing":
        del data["timeBasedDrops"][0]["self"]["currentMinutesWatched"]
    receipts = _ProgressReceipts()
    receipts.record(data, FIRST)
    clock[0] = LATER
    drop = DropsCampaign(client(), data, {}, progress_receipts=receipts.for_campaign(data)).timed_drops["one-drop"]
    assert drop.progress_observation is None


@pytest.mark.parametrize("change", ["copy", "minutes", "boolean", "drop_id", "campaign_id", "unknown"])
def test_unrecognized_or_mutated_rows_cannot_borrow_a_receipt(clock, change):
    data = campaign_data("one", minutes=7)
    receipts = _ProgressReceipts()
    if change != "unknown":
        receipts.record(data, FIRST)
    if change == "copy":
        data = deepcopy(data)
    elif change in ("minutes", "boolean"):
        data["timeBasedDrops"][0]["self"]["currentMinutesWatched"] = 8 if change == "minutes" else True
    elif change == "drop_id":
        data["timeBasedDrops"][0]["id"] = "different-drop"
    elif change == "campaign_id":
        data["id"] = "different-campaign"
    clock[0] = LATER
    assert receipts.for_campaign(data) == {}
    assert next(iter(DropsCampaign(client(), data, {}, progress_receipts={}).drops)).progress_observation is None


def test_reused_row_retains_first_receipt_but_new_equal_report_gets_new_receipt(clock):
    data = campaign_data("one", minutes=0)
    receipts = _ProgressReceipts()
    receipts.record(data, FIRST)
    receipts.record(data, LATER)
    assert receipts.for_campaign(data) == {"one-drop": FIRST}
    fresh = deepcopy(data)
    receipts.record(fresh, LATER)
    assert receipts.for_campaign(fresh) == {"one-drop": LATER}


@pytest.mark.asyncio
async def test_subsequent_unchanged_network_report_is_a_new_receipt(clock):
    data = campaign_data("one", minutes=0)
    twitch = client(ongoing=[data])
    await refresh(twitch)
    old = twitch._drops["one-drop"]
    clock[0] = LATER
    twitch.gql_request = client(ongoing=[deepcopy(data)]).gql_request
    await refresh(twitch)
    new = twitch._drops["one-drop"]
    assert old is not new
    assert old.progress_observation["received_at"] == FIRST.isoformat()
    assert new.progress_observation == {"minutes": 0, "source": "inventory", "received_at": LATER.isoformat()}


@pytest.mark.asyncio
async def test_successful_detail_receipt_survives_partial_auth_retry(clock, tmp_path):
    provider_clock = [1000]
    wire = transport()
    session = imported(tmp_path, provider_clock, wire)
    await session.install(bundle_data())
    waiting = asyncio.Event()

    async def pending(value):
        if value:
            waiting.set()

    await session.authenticate(SimpleNamespace(import_pending=AsyncMock(side_effect=pending)))
    early, late = campaign_data("early", minutes=7), campaign_data("late", minutes=9)
    wire.request.side_effect = [
        {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": [], "gameEventDrops": []}}}},
        {"data": {"currentUser": {"dropCampaigns": [metadata(early), metadata(late)]}}},
        json.loads(json.dumps([response(early), {"errors": [{"message": "failed integrity check"}]}])),
    ]
    twitch = client()
    identity = SimpleNamespace(browser_active=True, _twitch=SimpleNamespace(_browser=session), user_id=42)
    twitch.get_auth = AsyncMock(return_value=identity)
    twitch.gql_request = GQLClient(None, SimpleNamespace(validate=AsyncMock(return_value=identity)), ClientType.WEB).request
    parent = asyncio.create_task(InventoryService(twitch).fetch_inventory())
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        assert not parent.done()
        first_batch = wire.request.await_args.kwargs["body"]
        assert len(first_batch) == 2
        clock[0], provider_clock[0] = LATER, 1600
        wire.request.side_effect = [
            {"client_id": ClientType.WEB.CLIENT_ID, "user_id": "42"}, catalog(),
            json.loads(json.dumps([response(late)])),
        ]
        await session.install(bundle_data(1600, "new-integrity"))
        await asyncio.wait_for(parent, 1)
        await twitch._mnt_task
        assert wire.request.await_args.kwargs["body"] == [first_batch[1]]
        assert wire.request.await_count == 8
        assert twitch._drops["early-drop"].progress_observation == {
            "minutes": 7, "source": "inventory", "received_at": FIRST.isoformat(),
        }
        assert twitch._drops["late-drop"].progress_observation == {
            "minutes": 9, "source": "inventory", "received_at": LATER.isoformat(),
        }
    finally:
        parent.cancel()
        await asyncio.gather(parent, return_exceptions=True)
        await session.close()


@pytest.mark.asyncio
async def test_direct_http_receipt_precedes_slow_response_teardown(clock):
    known = campaign_data("one", minutes=7)
    twitch = client()
    identity = SimpleNamespace(browser_active=False, headers=lambda **kwargs: {})

    @asynccontextmanager
    async def request(*args, json, **kwargs):
        if isinstance(json, list):
            payload = [response(known)]
        elif json["operationName"] == GQL_OPERATIONS["Inventory"]["operationName"]:
            payload = {"data": {"currentUser": {"inventory": {
                "dropCampaignsInProgress": [known], "gameEventDrops": [],
            }}}}
        else:
            payload = {"data": {"currentUser": {"dropCampaigns": [metadata(known)]}}}
        yield SimpleNamespace(json=AsyncMock(return_value=payload))
        clock[0] = LATER

    auth = SimpleNamespace(validate=AsyncMock(return_value=identity))
    twitch.gql_request = GQLClient(SimpleNamespace(request=request), auth, ClientType.WEB).request
    await refresh(twitch)
    assert twitch._drops["one-drop"].progress_observation == {
        "minutes": 7, "source": "inventory", "received_at": FIRST.isoformat(),
    }
