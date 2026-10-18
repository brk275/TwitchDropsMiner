"""Reward-history admission preserves known inventory on unusable award evidence."""

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from src.exceptions import GQLException
from src.services.inventory_service import InventoryService
from tests.test_detail_null_guards import cleanup, install_previous
from tests.test_imported_session_retry import running_miner
from tests.test_unavailable_campaign_details import campaign_data, metadata, response


BAD_HISTORY = [
    None, False, True, 0, "", "private-history", {}, {"id": "wrong shape"},
    [None], [False], [0], ["private-row"], [[]], [{}],
    [{"id": "benefit"}],
    *([{"id": value, "lastAwardedAt": "2026-10-18T12:00:00Z"}]
      for value in (None, False, 0, [], {}, "", " \t")),
    *([{"id": "benefit", "lastAwardedAt": value}]
      for value in (None, False, 0, [], {}, "", "private-award", "2026-10-18",
                    "2026-10-18T12:00:00", "2026-13-01T00:00:00Z", "2026-02-30T00:00:00Z")),
    [{"id": "same", "lastAwardedAt": "2026-10-18T12:00:00Z"},
     {"id": "same", "lastAwardedAt": "private-award"}],
    [{"id": "same", "lastAwardedAt": "private-award"},
     {"id": "same", "lastAwardedAt": "2026-10-18T12:00:00Z"}],
]


@pytest.mark.asyncio
@pytest.mark.parametrize("history", BAD_HISTORY)
async def test_bad_award_history_refuses_refresh_before_replacement(tmp_path, monkeypatch, history):
    async with running_miner(tmp_path, monkeypatch) as (twitch, peer, _):
        old, maintenance, manager, _ = await install_previous(twitch)
        before = deepcopy(manager.get_campaigns())
        original = deepcopy(history)
        peer.responses.extend([
            {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": [], "gameEventDrops": history}}}},
            {"data": {"currentUser": {"dropCampaigns": []}}},
        ])
        try:
            with pytest.raises(GQLException, match="^Invalid claimed benefit history$") as caught:
                await InventoryService(twitch).fetch_inventory()
            assert "private" not in str(caught.value)
            assert twitch.inventory == [old] and twitch._campaigns == {old.id: old}
            assert twitch._drops == old.timed_drops and twitch._mnt_task is maintenance
            assert list(twitch._mnt_triggers) == [old.ends_at]
            assert manager.get_campaigns() == before
            manager.clear.assert_not_called()
            maintenance.cancel.assert_not_called()
            assert len(peer.calls) == 1 and len(peer.responses) == 1
            assert twitch.drop_history.total_count == 0
            assert twitch._browser.status()["state"] == "ready" and not twitch._browser._lock.locked()
            assert history == original
        finally:
            await cleanup(twitch)


@pytest.mark.parametrize("stamp", [
    "2026-10-18T12:00:00Z", "2026-10-18T12:00:00+00:00",
    "2026-10-18T17:30:00+05:30", "2026-10-18T08:00:00-04:00",
    "2026-10-18T12:00:00.123456Z",
])
def test_aware_award_times_and_opaque_ids_remain_usable(stamp):
    records = [{"id": " opaque benefit id ", "lastAwardedAt": stamp}]
    before = deepcopy(records)
    result = InventoryService._claimed_benefits_by_id(records)
    assert list(result) == [" opaque benefit id "]
    assert result[" opaque benefit id "].astimezone(timezone.utc).replace(microsecond=0) == datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
    assert result[" opaque benefit id "].utcoffset() == datetime.fromisoformat(stamp.replace("Z", "+00:00")).utcoffset()
    assert records == before


def test_empty_history_and_existing_last_row_duplicate_policy_are_preserved():
    assert InventoryService._claimed_benefits_by_id([]) == {}
    rows = [{"id": "same", "lastAwardedAt": stamp} for stamp in
            ("2026-10-17T12:00:00Z", "2026-10-18T12:00:00Z")]
    for ordered in (rows, rows[::-1]):
        result = InventoryService._claimed_benefits_by_id(ordered)
        assert result["same"].isoformat() == ordered[-1]["lastAwardedAt"].replace("Z", "+00:00")


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("award_present", [False, True])
async def test_valid_history_keeps_explicit_state_and_existing_inference(tmp_path, monkeypatch, explicit, award_present):
    async with running_miner(tmp_path, monkeypatch) as (twitch, peer, _):
        _, _, manager, _ = await install_previous(twitch)
        data = campaign_data("fresh")
        if not explicit:
            del data["timeBasedDrops"][0]["self"]
        awards = [{"id": "benefit-fresh-drop", "lastAwardedAt": datetime.now(timezone.utc).isoformat()}] if award_present else []
        peer.responses.extend([
            {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": [], "gameEventDrops": awards}}}},
            {"data": {"currentUser": {"dropCampaigns": [metadata(data)]}}},
            [response(data)],
        ])
        try:
            await InventoryService(twitch).fetch_inventory()
            await twitch._mnt_task
            expected = award_present and not explicit
            drop = twitch._drops["fresh-drop"]
            assert drop.is_claimed is expected and drop.real_current_minutes == (60 if expected else 0)
            shown, = manager.get_campaigns()
            assert shown["drops"][0]["is_claimed"] is expected
            assert twitch.drop_history.total_count == 0 and drop.campaign.progress_revision == 0
            assert len(peer.calls) == 3 and not peer.responses
        finally:
            await cleanup(twitch)
