from collections import deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config.constants import State
from src.models.campaign import DropsCampaign
from src.models.drop import BaseDrop, TimedDrop
from src.services.inventory_service import InventoryService
from src.web.managers.inventory import InventoryManager


START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = datetime(2099, 1, 1, tzinfo=timezone.utc)
AWARDED = START + timedelta(hours=1)


def drop_data(benefit_ids=("first", "second", "third"), *, drop_id="drop"):
    return {
        "id": drop_id,
        "name": f"Drop {drop_id}",
        "benefitEdges": [
            {
                "benefit": {
                    "id": bid,
                    "name": f"Reward {bid}",
                    "distributionType": "DIRECT_ENTITLEMENT",
                    "imageAssetURL": "https://example.test/reward.png",
                }
            }
            for bid in benefit_ids
        ],
        "startAt": START.isoformat(),
        "endAt": END.isoformat(),
        "preconditionDrops": [],
        "requiredMinutesWatched": 60,
    }


def campaign_data(drops):
    return {
        "id": "campaign",
        "name": "Campaign",
        "game": {
            "id": "1",
            "name": "Test Game",
            "displayName": "Test Game",
            "boxArtURL": "https://example.test/game.jpg",
        },
        "self": {"isAccountConnected": True},
        "accountLinkURL": "https://example.test/link",
        "startAt": START.isoformat(),
        "endAt": END.isoformat(),
        "status": "ACTIVE",
        "allow": {"channels": [], "isEnabled": True},
        "timeBasedDrops": drops,
    }


@pytest.mark.parametrize("kind", [BaseDrop, TimedDrop])
@pytest.mark.parametrize(
    "count,mask", [(count, mask) for count in range(1, 4) for mask in range(1 << count)]
)
def test_inference_requires_every_benefit(kind, count, mask):
    ids = tuple(f"benefit-{i}" for i in range(count))
    awards = {bid: AWARDED for i, bid in enumerate(ids) if mask & (1 << i)}
    original_awards = awards.copy()
    data = drop_data(ids)
    original_data = deepcopy(data)
    twitch = MagicMock()

    drop = kind(SimpleNamespace(_twitch=twitch), data, awards)

    claimed = mask == (1 << count) - 1
    assert drop.is_claimed is claimed
    assert drop.claim_id is None
    if kind is TimedDrop:
        assert drop.current_minutes == (60 if claimed else 0)
        assert drop.extra_current_minutes == 0
    assert awards == original_awards
    assert data == original_data
    assert twitch.mock_calls == []


@pytest.mark.parametrize("kind", [BaseDrop, TimedDrop])
@pytest.mark.parametrize(
    "stamp,claimed",
    [
        (START - timedelta(microseconds=1), False),
        (START, True),
        (AWARDED, True),
        (END - timedelta(microseconds=1), True),
        (END, False),
        (END + timedelta(microseconds=1), False),
    ],
)
def test_every_award_must_be_in_the_drop_window(kind, stamp, claimed):
    drop = kind(
        SimpleNamespace(_twitch=MagicMock()),
        drop_data(),
        {"first": AWARDED, "second": stamp, "third": AWARDED},
    )
    assert drop.is_claimed is claimed


@pytest.mark.parametrize("kind", [BaseDrop, TimedDrop])
@pytest.mark.parametrize("claimed", [False, True])
@pytest.mark.parametrize("awards", [{}, dict.fromkeys(("first", "second", "third"), AWARDED)])
def test_explicit_claim_state_takes_precedence(kind, claimed, awards):
    data = drop_data()
    data["self"] = {
        "isClaimed": claimed,
        "dropInstanceID": "opaque-claim-id",
        "currentMinutesWatched": 17,
    }
    drop = kind(SimpleNamespace(_twitch=MagicMock()), data, awards)
    assert drop.is_claimed is claimed
    assert drop.claim_id == "opaque-claim-id"
    if kind is TimedDrop:
        assert drop.current_minutes == (60 if claimed else 17)


@pytest.mark.parametrize("kind", [BaseDrop, TimedDrop])
@pytest.mark.parametrize(
    "ids,awards",
    [
        ((), {}),
        ((), {"unrelated": AWARDED}),
        (("first",), {"unrelated": AWARDED}),
        (("first", "second"), {"first": AWARDED, "unrelated": AWARDED}),
    ],
)
def test_empty_or_unrelated_history_cannot_complete_drop(kind, ids, awards):
    drop = kind(SimpleNamespace(_twitch=MagicMock()), drop_data(ids), awards)
    assert drop.is_claimed is False


def test_offsets_and_unrelated_out_of_window_awards():
    offset = timezone(timedelta(hours=5, minutes=30))
    drop = TimedDrop(
        SimpleNamespace(_twitch=MagicMock()),
        drop_data((" first ", "second")),
        {
            " first ": START.astimezone(offset),
            "second": AWARDED,
            "unrelated": END + timedelta(days=1),
        },
    )
    assert drop.is_claimed is True
    assert drop.claim_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [False, True])
async def test_inventory_refresh_preserves_shared_reward_and_prerequisite_state(complete):
    parent = drop_data(("shared", "new"), drop_id="parent")
    child = drop_data(("child-benefit",), drop_id="child")
    child["preconditionDrops"] = [{"id": "parent"}]
    raw = campaign_data([parent, child])
    history = [{"id": "shared", "lastAwardedAt": AWARDED.isoformat()}]
    if complete:
        history.append({"id": "new", "lastAwardedAt": AWARDED.isoformat()})
    broadcaster = SimpleNamespace(emit=AsyncMock())
    gui = SimpleNamespace(
        status=SimpleNamespace(update=MagicMock()),
        inv=InventoryManager(broadcaster, MagicMock()),
    )
    twitch = SimpleNamespace(
        gui=gui,
        settings=SimpleNamespace(drop_name_blacklist=[]),
        _drops={},
        _campaigns={},
        inventory=[],
        _mnt_triggers=deque(),
        _mnt_task=None,
        _maintenance_service=SimpleNamespace(run_maintenance_task=AsyncMock()),
        _state=State.INVENTORY_FETCH,
        gql_request=AsyncMock(
            side_effect=[
                {
                    "data": {
                        "currentUser": {
                            "inventory": {
                                "dropCampaignsInProgress": [raw],
                                "gameEventDrops": history,
                            }
                        }
                    }
                },
                {"data": {"currentUser": {"dropCampaigns": []}}},
            ]
        ),
        drop_history=MagicMock(),
    )

    await InventoryService(twitch).fetch_inventory()
    await twitch._mnt_task

    campaign = twitch.inventory[0]
    assert isinstance(campaign, DropsCampaign)
    assert campaign.timed_drops["parent"].is_claimed is complete
    assert campaign.timed_drops["parent"].current_minutes == (60 if complete else 0)
    assert campaign.timed_drops["child"].preconditions_met is complete
    assert campaign.timed_drops["child"].can_earn() is complete
    assert campaign.claimed_drops == int(complete)
    displayed = gui.inv.get_campaigns()[0]
    assert displayed["claimed_drops"] == int(complete)
    assert displayed["drops"][0]["is_claimed"] is complete
    assert displayed["drops"][0]["current_minutes"] == (60 if complete else 0)
    assert displayed["finished"] is False
    twitch.drop_history.record.assert_not_called()
    assert twitch.gql_request.await_count == 2
