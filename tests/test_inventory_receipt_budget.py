"""Discarded GraphQL retries must not grow receipt retention indefinitely."""

import asyncio
import gc
import json as json_lib
import weakref
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.services.inventory_service as inventory_module
from src.api import GQLClient
from src.config import ClientType
from src.models.campaign import DropsCampaign
from src.services.inventory_service import _ProgressReceipts
from src.web.managers.inventory import InventoryManager
from tests.test_inventory_receipt_times import FIRST, LATER, refresh
from tests.test_inventory_receipt_times import clock as clock
from tests.test_unavailable_campaign_details import campaign_data, client, metadata, response


def test_default_receipt_budget_is_finite_and_exhaustion_is_unknown():
    rows = [{"id": str(index), "self": {"currentMinutesWatched": 0}} for index in range(8193)]
    data = {"id": "one", "timeBasedDrops": rows}
    receipts = _ProgressReceipts()
    receipts.record(data, FIRST)
    assert len(receipts._rows) <= 8192
    assert receipts.for_campaign(data) == {}


@pytest.mark.parametrize("claimed", [False, True])
def test_exhaustion_releases_old_receipts_and_cannot_restart_in_same_refresh(monkeypatch, clock, claimed):
    monkeypatch.setattr(_ProgressReceipts, "_MAX_ROWS", 1, raising=False)
    first, second = campaign_data("one", minutes=0), campaign_data("two", minutes=7)
    second["timeBasedDrops"][0]["self"]["isClaimed"] = claimed
    receipts = _ProgressReceipts()
    receipts.record(first, FIRST)
    saved = [weakref.ref(receipt) for receipt in receipts._rows.values()]
    assert receipts.for_campaign(first) == {"one-drop": FIRST}
    receipts.record(first, LATER)
    assert receipts.for_campaign(first) == {"one-drop": FIRST}
    receipts.record(second, LATER)
    gc.collect()
    assert all(reference() is None for reference in saved)
    assert receipts._rows == {}
    for _ in range(20):
        receipts.record_response(response(first), LATER)
        receipts.record(second, LATER)
    assert receipts._rows == {} and receipts.for_campaign(first) == receipts.for_campaign(second) == {}
    drop = DropsCampaign(client(), second, {}, progress_receipts=receipts.for_campaign(second)).timed_drops["two-drop"]
    assert drop.real_current_minutes == (60 if claimed else 7) and drop.is_claimed is claimed
    serialized = InventoryManager._serialize_drop(drop)
    assert serialized["current_minutes"] == (60 if claimed else 7)
    assert serialized["progress_observation"] is None
    fresh = _ProgressReceipts()
    fresh.record(first, LATER)
    assert fresh.for_campaign(first) == {"one-drop": LATER}


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["service unavailable", "service timeout", "context deadline exceeded"])
@pytest.mark.parametrize("finish", ["success", "cancel"])
async def test_repeated_discarded_batches_keep_finite_receipts_without_changing_retries(
    monkeypatch, clock, error, finish,
):
    original = _ProgressReceipts
    ledgers, high_water, references = [], [0], []
    attempts, calls = [0], []
    rounds = 12
    stopped = asyncio.Event()

    class Ledger(original):
        _MAX_ROWS = 4

        def __init__(self):
            super().__init__()
            ledgers.append(self)

        def record(self, data, stamp):
            super().record(data, stamp)
            high_water[0] = max(high_water[0], len(self._rows))
            references.extend(weakref.ref(receipt) for receipt in self._rows.values())

    class Limiter:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(inventory_module, "_ProgressReceipts", Ledger)
    monkeypatch.setattr("src.api.gql_client.ExponentialBackoff", lambda **kwargs: iter([0] * (rounds + 1)))
    good, other = campaign_data("one", minutes=7), campaign_data("two", minutes=9)

    @asynccontextmanager
    async def request(*args, json, **kwargs):
        calls.append(json)
        if isinstance(json, list):
            attempts[0] += 1
            if attempts[0] <= rounds:
                payload = [response(good), {"errors": [{"message": error}]}]
            else:
                if finish == "cancel":
                    stopped.set()
                    await asyncio.Event().wait()
                clock[0] = LATER
                payload = [response(good), response(other)]
        elif json["operationName"] == "Inventory":
            payload = {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": [], "gameEventDrops": []}}}}
        else:
            payload = {"data": {"currentUser": {"dropCampaigns": [metadata(good), metadata(other)]}}}

        async def decode():
            return json_lib.loads(json_lib.dumps(payload))

        yield SimpleNamespace(json=decode)

    twitch = client()
    identity = SimpleNamespace(browser_active=False, headers=lambda **kwargs: {})
    gql = GQLClient(SimpleNamespace(request=request), SimpleNamespace(validate=AsyncMock(return_value=identity)), ClientType.WEB)
    gql._qgl_limiter = Limiter()
    twitch.gql_request = gql.request
    parent = asyncio.create_task(refresh(twitch))
    try:
        if finish == "cancel":
            await asyncio.wait_for(stopped.wait(), 2)
            parent.cancel()
            with pytest.raises(asyncio.CancelledError):
                await parent
            assert twitch.inventory == [] and twitch._drops == {}
            twitch.gui.inv.add_campaign.assert_not_awaited()
        else:
            await asyncio.wait_for(parent, 2)
            assert {drop.id: drop.real_current_minutes for drop in twitch._drops.values()} == {"one-drop": 7, "two-drop": 9}
            assert all(drop.progress_observation is None for drop in twitch._drops.values())
        assert attempts[0] == rounds + 1
        assert len(calls) == rounds + 3
        assert all(len(body) == 2 for body in calls if isinstance(body, list))
        assert high_water[0] <= 4 and ledgers[0]._rows == {}
        gc.collect()
        assert all(reference() is None for reference in references)
        if finish == "success":
            await refresh(twitch)
            assert len(calls) == rounds + 6 and len(ledgers) == 2
            assert all(drop.progress_observation == {
                "minutes": drop.real_current_minutes, "source": "inventory", "received_at": LATER.isoformat(),
            } for drop in twitch._drops.values())
    finally:
        parent.cancel()
        await asyncio.gather(parent, return_exceptions=True)
