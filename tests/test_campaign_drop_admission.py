"""Campaign drop collections must not collapse invalid or duplicate identities."""

from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from src.exceptions import MinerException
from src.models.campaign import DropsCampaign
from src.services.inventory_service import InventoryService
from tests.test_detail_null_guards import cleanup, install_previous
from tests.test_imported_session_retry import running_miner
from tests.test_unavailable_campaign_details import campaign_data, metadata, response
from tests.test_watch_drop_filtering import _drop


INVALID_DROPS = [
    None, False, True, 0, 1, "", "private-drop-list", {}, {"id": "not a list"},
    [None], [False], [0], ["private-row"], [[]], [{}],
    *([{"id": value}] for value in (None, False, 0, [], {}, "", " \t")),
]


@pytest.mark.parametrize("drops", INVALID_DROPS)
def test_invalid_drop_collection_is_refused_without_mutating_input(drops):
    data = campaign_data("bad")
    data["timeBasedDrops"] = drops
    original = deepcopy(data)
    twitch = MagicMock()
    with pytest.raises(MinerException, match="^Invalid campaign drop list$") as caught:
        DropsCampaign(twitch, data, {})
    assert "private" not in str(caught.value)
    assert data == original


@pytest.mark.parametrize("claimed_first", [False, True])
def test_duplicate_valid_rewards_cannot_silently_overwrite_each_other(claimed_first):
    data = campaign_data("duplicate")
    first, second = deepcopy(data["timeBasedDrops"][0]), deepcopy(data["timeBasedDrops"][0])
    first["name"], second["name"] = "First reward", "Second reward"
    first["self"]["isClaimed"] = claimed_first
    second["self"]["isClaimed"] = not claimed_first
    data["timeBasedDrops"] = [first, second]
    original = deepcopy(data)
    with pytest.raises(MinerException, match="^Invalid campaign drop list$"):
        DropsCampaign(MagicMock(), data, {})
    assert data == original


@pytest.mark.asyncio
@pytest.mark.parametrize("drops", [*INVALID_DROPS, "duplicate-valid"])
async def test_bad_detail_drop_collection_preserves_previous_inventory(tmp_path, monkeypatch, drops):
    async with running_miner(tmp_path, monkeypatch) as (twitch, peer, _):
        old, maintenance, manager, _ = await install_previous(twitch)
        before = deepcopy(manager.get_campaigns())
        new = campaign_data("new", minutes=7)
        if drops == "duplicate-valid":
            new["timeBasedDrops"] *= 2
        else:
            new["timeBasedDrops"] = drops
        peer.responses.extend([
            {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": [], "gameEventDrops": []}}}},
            {"data": {"currentUser": {"dropCampaigns": [metadata(new)]}}},
            [response(new)],
        ])
        try:
            with pytest.raises(MinerException, match="^Invalid campaign drop list$"):
                await InventoryService(twitch).fetch_inventory()
            assert twitch.inventory == [old] and twitch._campaigns == {old.id: old}
            assert twitch._drops == old.timed_drops and twitch._mnt_task is maintenance
            assert list(twitch._mnt_triggers) == [old.ends_at]
            assert manager.get_campaigns() == before
            manager.clear.assert_not_called()
            maintenance.cancel.assert_not_called()
            assert twitch.drop_history.total_count == 0
            assert len(peer.calls) == 3 and not peer.responses
            assert twitch._browser.status()["state"] == "ready"
        finally:
            await cleanup(twitch)


@pytest.mark.parametrize("drop_id", ["normal", "0", " opaque id ", "drop-with-dashes"])
def test_exact_drop_ids_and_original_order_are_preserved(drop_id):
    data = campaign_data("valid")
    first, second = _drop(drop_id, "First", 60), _drop("second", "Second", 0)
    second["preconditionDrops"] = [{"id": drop_id}]
    data["timeBasedDrops"] = [first, second]
    original = deepcopy(data)
    model = DropsCampaign(MagicMock(), data, {})
    assert list(model.timed_drops) == [drop_id, "second"]
    assert model.timed_drops[drop_id].id == drop_id
    assert model.timed_drops["second"].precondition_drops == [drop_id]
    assert not model.timed_drops["second"].is_watch_drop
    assert data == original


def test_empty_drop_list_remains_a_valid_empty_campaign():
    data = campaign_data("empty")
    data["timeBasedDrops"] = []
    assert DropsCampaign(MagicMock(), data, {}).timed_drops == {}


def test_missing_drop_list_is_not_invented_as_empty():
    data = campaign_data("missing")
    del data["timeBasedDrops"]
    with pytest.raises(MinerException, match="^Invalid campaign drop list$"):
        DropsCampaign(MagicMock(), data, {})
