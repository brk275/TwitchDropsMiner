"""Partial reward history cannot establish completion of a multi-benefit drop."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from src.models.campaign import DropsCampaign
from src.models.drop import BaseDrop, TimedDrop
from src.services.inventory_service import InventoryService
from tests.test_detail_null_guards import cleanup, install_previous
from tests.test_imported_session_retry import running_miner
from tests.test_stale_progress import mining_state as mining_state
from tests.test_unavailable_campaign_details import campaign_data, metadata, response
from tests.test_watch_drop_filtering import _drop


def multi_reward(drop_id="new", ids=("first", "second", "third")):
    data = _drop(drop_id, "Multi reward", 60)
    edge = data["benefitEdges"][0]
    data["benefitEdges"] = []
    for benefit_id in ids:
        item = deepcopy(edge)
        item["benefit"]["id"] = benefit_id
        data["benefitEdges"].append(item)
    return data


@pytest.mark.parametrize("kind", [BaseDrop, TimedDrop])
@pytest.mark.parametrize("size,mask", [(size, mask) for size in (1, 2, 3) for mask in range(2 ** size)])
def test_every_required_benefit_must_have_an_in_window_award(mining_state, kind, size, mask):
    _, _, campaign = mining_state
    ids = ("first", "second", "third")[:size]
    data = multi_reward(ids=ids)
    awards = {key: datetime.now(timezone.utc) for i, key in enumerate(ids) if mask & (1 << i)}
    original = deepcopy(data)
    drop = kind(campaign, data, awards)
    complete = mask == 2 ** size - 1
    assert drop.is_claimed is complete and drop.claim_id is None
    assert data == original and campaign.progress_revision == 0
    if kind is TimedDrop:
        assert drop.real_current_minutes == (60 if complete else 0)


@pytest.mark.parametrize("kind", [BaseDrop, TimedDrop])
@pytest.mark.parametrize("boundary,offset,claimed", [
    ("start", -1, False), ("start", 0, True), ("start", 1, True),
    ("end", -1, True), ("end", 0, False), ("end", 1, False),
])
def test_all_awards_keep_existing_inclusive_start_exclusive_end(mining_state, kind, boundary, offset, claimed):
    _, _, campaign = mining_state
    data = multi_reward(ids=("first", "second"))
    stamp = datetime.fromisoformat(data["startAt" if boundary == "start" else "endAt"].replace("Z", "+00:00"))
    awards = {"first": datetime.now(timezone.utc), "second": stamp + timedelta(microseconds=offset)}
    assert kind(campaign, data, awards).is_claimed is claimed


@pytest.mark.parametrize("claimed", [False, True])
@pytest.mark.parametrize("has_awards", [False, True])
def test_explicit_claim_state_takes_precedence_over_reward_history(mining_state, claimed, has_awards):
    _, _, campaign = mining_state
    data = multi_reward()
    data["self"] = {"isClaimed": claimed, "dropInstanceID": "explicit", "currentMinutesWatched": 7}
    awards = {key: datetime.now(timezone.utc) for key in ("first", "second", "third")} if has_awards else {}
    drop = TimedDrop(campaign, data, awards)
    assert drop.is_claimed is claimed and drop.claim_id == "explicit"
    assert drop.real_current_minutes == (60 if claimed else 7)


@pytest.mark.parametrize("ids,awards", [
    ((), {}), ((), {"unrelated": datetime(2026, 6, 1, tzinfo=timezone.utc)}),
    (("first", "second"), {"unrelated": datetime(2026, 6, 1, tzinfo=timezone.utc)}),
])
def test_empty_or_unrelated_benefit_history_does_not_infer_claim(mining_state, ids, awards):
    _, _, campaign = mining_state
    assert not TimedDrop(campaign, multi_reward(ids=ids), awards).is_claimed


@pytest.mark.asyncio
@pytest.mark.parametrize("awarded,expected", [
    (("shared",), (False, False)),
    (("shared", "unique-one"), (True, False)),
    (("shared", "unique-two"), (False, True)),
    (("shared", "unique-one", "unique-two"), (True, True)),
])
async def test_real_refresh_does_not_hide_drops_from_partial_shared_rewards(tmp_path, monkeypatch, awarded, expected):
    async with running_miner(tmp_path, monkeypatch) as (twitch, peer, _):
        _, _, manager, _ = await install_previous(twitch)
        campaign = campaign_data("multi")
        campaign["timeBasedDrops"] = [
            multi_reward("one", ("shared", "unique-one")),
            multi_reward("two", ("shared", "unique-two")),
        ]
        awards = [{"id": key, "lastAwardedAt": datetime.now(timezone.utc).isoformat()} for key in awarded]
        peer.responses.extend([
            {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": [], "gameEventDrops": awards}}}},
            {"data": {"currentUser": {"dropCampaigns": [metadata(campaign)]}}},
            [response(campaign)],
        ])
        try:
            await InventoryService(twitch).fetch_inventory()
            await twitch._mnt_task
            assert tuple(twitch._drops[key].is_claimed for key in ("one", "two")) == expected
            assert tuple(twitch._drops[key].real_current_minutes for key in ("one", "two")) == tuple(60 if value else 0 for value in expected)
            shown, = manager.get_campaigns()
            assert tuple(drop["is_claimed"] for drop in shown["drops"]) == expected
            assert twitch._campaigns["multi"].mining_finished is all(expected)
            assert twitch.drop_history.total_count == 0
            assert twitch._campaigns["multi"].progress_revision == 0
            assert len(peer.calls) == 3 and not peer.responses
            assert twitch._browser.status()["state"] == "ready"
        finally:
            await cleanup(twitch)


@pytest.mark.parametrize("complete", [False, True])
def test_partial_benefit_history_cannot_unlock_a_dependent_drop(mining_state, complete):
    twitch, _, _ = mining_state
    data = campaign_data("chain")
    data["timeBasedDrops"] = [
        multi_reward("parent", ("shared", "unique")),
        _drop("child", "Child", 60, precondition_drop_ids=["parent"]),
    ]
    awards = {"shared": datetime.now(timezone.utc)}
    if complete:
        awards["unique"] = datetime.now(timezone.utc)
    campaign = DropsCampaign(twitch, data, awards)
    parent, child = campaign.timed_drops["parent"], campaign.timed_drops["child"]
    assert parent.is_claimed is complete and child.preconditions_met is complete
    assert not child.is_claimed and child.real_current_minutes == 0


def test_unrelated_award_outside_window_does_not_change_complete_own_benefits(mining_state):
    _, _, campaign = mining_state
    data = multi_reward(ids=("first", "second"))
    stamp = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=5, minutes=30)))
    awards = {"first": stamp, "second": stamp, "unrelated": datetime(2000, 1, 1, tzinfo=timezone.utc)}
    assert TimedDrop(campaign, data, awards).is_claimed
