"""Progress reads use detached GQL models, then guarded owner-only commits."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from src.exceptions import ExitRequest, GQLException
from src.models.drop import TimedDrop
from src.services.inventory_progress import InventoryProgressReader
from src.services.inventory_service import InventoryService
from tests.test_deadline_execution import execution as execution_state
from tests.test_deadline_forecast import NOW
from tests.test_drop_ignore_policy import _drop
from tests.test_unavailable_campaign_details import metadata, response


def wire(campaign, *, minutes=7, claimed=False):
    """Serialize fixture metadata to fake transport, not a live progress mutation."""
    return {
        "id": campaign.id, "name": campaign.name, "status": "ACTIVE",
        "game": {"id": str(campaign.game.id), "name": campaign.game.name},
        "self": {"isAccountConnected": campaign.linked}, "accountLinkURL": campaign.link_url,
        "startAt": campaign.starts_at.isoformat(), "endAt": campaign.ends_at.isoformat(),
        "allow": {"channels": [], "isEnabled": True},
        "timeBasedDrops": [{
            "id": drop.id, "name": drop.name, "requiredMinutesWatched": drop.required_minutes,
            "startAt": drop.starts_at.isoformat(), "endAt": drop.ends_at.isoformat(),
            "preconditionDrops": [{"id": key} for key in drop.precondition_drops],
            "benefitEdges": [{"benefit": {
                "id": benefit.id, "name": benefit.name, "distributionType": benefit.type.name,
                "imageAssetURL": "https://example.test/reward.png",
            }} for benefit in drop.benefits],
            "self": {"dropInstanceID": drop.claim_id, "isClaimed": claimed, "currentMinutesWatched": minutes},
        } for drop in campaign.drops],
    }


def transport(twitch, payloads):
    twitch.get_auth = AsyncMock(return_value=twitch._auth_state)
    twitch.gql_request = AsyncMock(side_effect=[
        {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": deepcopy(payloads), "gameEventDrops": []}}}},
        {"data": {"currentUser": {"dropCampaigns": [metadata(data) for data in payloads]}}},
        [response(deepcopy(data)) for data in payloads],
    ])


@pytest.fixture
def progress(monkeypatch):
    state = execution_state.__wrapped__(monkeypatch)
    twitch, _, _, executor, _, _ = state
    twitch._inventory_service = InventoryService(twitch)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            now = executor._utc_clock()
            return now.replace(tzinfo=None) if tz is None else now.astimezone(tz)

    monkeypatch.setattr("src.services.inventory_service.datetime", Clock)
    reader = InventoryProgressReader(twitch, clock=executor._tick, utc_clock=executor._utc_clock)
    transport(twitch, [wire(campaign) for campaign in twitch.inventory])
    return state, reader


def prepare(progress):
    state, reader = progress
    state[0]._run_task = asyncio.current_task()
    request = reader.prepare()
    assert request is not None
    return request


@pytest.mark.asyncio
async def test_real_collection_stays_detached_until_owner_commits_all_reports(progress):
    state, reader = progress
    twitch, high, urgent, _, _, move = state
    request = prepare(progress)
    move(2)
    batch = await asyncio.create_task(reader.read(request))
    assert [drop.real_current_minutes for campaign in twitch.inventory for drop in campaign.drops] == [0, 0]
    twitch.gui.inv.update_drop.assert_not_called()
    seen = []
    for campaign in twitch.inventory:
        for drop in campaign.drops:
            drop._on_state_changed = lambda: seen.append(tuple(d.real_current_minutes for c in twitch.inventory for d in c.drops))
    assert reader.apply(batch) == "applied"
    assert seen == [(7, 7), (7, 7)]
    for campaign in (high, urgent):
        assert campaign.progress_revision == 1
        for drop in campaign.drops:
            assert drop.progress_observation == {"minutes": 7, "source": "inventory", "received_at": (NOW + timedelta(seconds=2)).isoformat()}
    twitch.watch.assert_not_called()
    twitch.stop_watching.assert_not_called()


@pytest.mark.asyncio
async def test_read_receipt_is_not_restamped_at_owner_consumption(progress):
    state, reader = progress
    request = prepare(progress)
    state[5](2)
    batch = await reader.read(request)
    state[5](10)
    assert reader.apply(batch) == "applied"
    assert state[1].timed_drops["reward"].progress_observation["received_at"] == (NOW + timedelta(seconds=2)).isoformat()


@pytest.mark.asyncio
async def test_newer_campaign_observation_wins_while_other_campaign_refreshes(progress):
    state, reader = progress
    request = prepare(progress)
    state[5](2)
    batch = await reader.read(request)
    high = state[1].timed_drops["reward"]
    high.update_minutes(9, source="websocket")
    assert reader.apply(batch) == "applied"
    assert high.real_current_minutes == 9 and high.progress_observation["source"] != "inventory"
    assert state[2].timed_drops["urgent"].real_current_minutes == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["rank", "models", "auth", "account", "manual", "refresh", "name", "link"])
async def test_live_context_changes_discard_collected_data(progress, change):
    state, reader = progress
    twitch, high = state[:2]
    batch = await reader.read(prepare(progress))
    if change == "rank":
        twitch.settings.games_to_watch.reverse()
    elif change == "models":
        twitch.inventory = list(twitch.inventory)
    elif change == "auth":
        twitch._auth_state.context_epoch = object()
    elif change == "account":
        twitch._auth_state.user_id += 1
    elif change == "manual":
        twitch._manual_target_game = high.game
    elif change == "refresh":
        twitch._inventory_refresh_pending = True
    elif change == "name":
        high.name = "Changed"
    else:
        high.link_url = "https://example.test/changed"
    assert reader.apply(batch) == "superseded"
    assert high.timed_drops["reward"].real_current_minutes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "new", "name", "claim", "deadline", "benefit", "prerequisite"])
async def test_server_metadata_or_claim_changes_request_reload_without_partial_commits(progress, change):
    state, reader = progress
    request = prepare(progress)
    payloads = [wire(campaign) for campaign in state[0].inventory]
    if change == "missing":
        payloads.pop()
    elif change == "new":
        item = deepcopy(payloads[1])
        item["id"] = "new"
        payloads.append(item)
    elif change == "name":
        payloads[1]["name"] = "changed"
    elif change == "claim":
        payloads[1]["timeBasedDrops"][0]["self"]["isClaimed"] = True
    elif change == "deadline":
        payloads[1]["endAt"] = (NOW + timedelta(hours=5)).isoformat()
    elif change == "benefit":
        payloads[1]["timeBasedDrops"][0]["benefitEdges"][0]["benefit"]["id"] = "changed"
    else:
        payloads[1]["timeBasedDrops"][0]["preconditionDrops"] = [{"id": "new"}]
    transport(state[0], payloads)
    batch = await reader.read(request)
    assert reader.apply(batch) == "inventory_changed"
    assert [d.real_current_minutes for c in state[0].inventory for d in c.drops] == [0, 0]
    assert all(not d.is_claimed for c in state[0].inventory for d in c.drops)


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [None, True, -1, 9999, "7", 1.2])
async def test_invalid_raw_minutes_never_renew_the_live_observation(progress, minutes):
    state, reader = progress
    request = prepare(progress)
    payloads = [wire(campaign, minutes=minutes) for campaign in state[0].inventory]
    transport(state[0], payloads)
    batch = await reader.read(request)
    assert reader.apply(batch) in ("unchanged", "unavailable")
    assert all(d.progress_observation["received_at"] == NOW.isoformat() for c in state[0].inventory for d in c.drops)


@pytest.mark.asyncio
async def test_zero_is_a_direct_observation_and_claimed_normalization_is_not(progress):
    state, reader = progress
    request = prepare(progress)
    state[5](2)
    transport(state[0], [wire(campaign, minutes=0) for campaign in state[0].inventory])
    batch = await reader.read(request)
    assert reader.apply(batch) == "applied"
    assert all(d.progress_observation["minutes"] == 0 for c in state[0].inventory for d in c.drops)


@pytest.mark.asyncio
async def test_background_tasks_cannot_prepare_or_apply(progress):
    state, reader = progress
    request = prepare(progress)
    batch = await reader.read(request)

    async def foreign():
        assert reader.prepare() is None
        assert reader.apply(batch) == "superseded"

    await asyncio.create_task(foreign())
    assert state[1].timed_drops["reward"].real_current_minutes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds", [-1, 15.001])
async def test_batch_age_and_backwards_clocks_discard_without_publishing(progress, seconds):
    state, reader = progress
    batch = await reader.read(prepare(progress))
    state[5](seconds)
    assert reader.apply(batch) == "superseded"
    assert state[1].timed_drops["reward"].real_current_minutes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [GQLException("unavailable"), TimeoutError(), ExitRequest(), RuntimeError("unexpected")])
async def test_expected_unavailability_does_not_swallow_exit_or_unexpected_errors(progress, error):
    state, reader = progress
    request = prepare(progress)
    state[0].gql_request = AsyncMock(side_effect=error)
    if isinstance(error, (ExitRequest, RuntimeError)):
        with pytest.raises(type(error)):
            await reader.read(request)
    else:
        assert reader.apply(await reader.read(request)) == "unavailable"


@pytest.mark.asyncio
async def test_retired_auth_rejected_before_any_network_request(progress):
    state, reader = progress
    request = prepare(progress)
    state[0]._auth_state.context_epoch = object()
    assert (await reader.read(request)).signature is None
    state[0].gql_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_each_sibling_uses_its_own_report_without_shared_delta_inference(progress):
    state, reader = progress
    twitch, high = state[:2]
    raw = _drop("sibling", "Sibling", required_minutes=120)
    raw["self"] = {"isClaimed": False, "currentMinutesWatched": 0, "dropInstanceID": "claim-sibling"}
    sibling = TimedDrop(high, raw, {})
    high.timed_drops[sibling.id] = twitch._drops[sibling.id] = sibling
    request = prepare(progress)
    payloads = [wire(campaign) for campaign in twitch.inventory]
    payloads[0]["timeBasedDrops"][1]["self"]["currentMinutesWatched"] = 17
    transport(twitch, payloads)
    assert reader.apply(await reader.read(request)) == "applied"
    assert high.timed_drops["reward"].real_current_minutes == 7
    assert sibling.real_current_minutes == sibling.progress_observation["minutes"] == 17
    assert high.progress_revision == 1


@pytest.mark.asyncio
async def test_reentrant_auth_retirement_stops_remaining_gui_publication(progress):
    state, reader = progress
    twitch = state[0]
    batch = await reader.read(prepare(progress))
    published = []

    def retire():
        published.append("first")
        twitch._auth_state.context_epoch = object()

    state[1].timed_drops["reward"]._on_state_changed = retire
    state[2].timed_drops["urgent"]._on_state_changed = lambda: published.append("retired")
    assert reader.apply(batch) == "superseded"
    assert published == ["first"]
    # Confirmed direct reports were committed before any callback, not rolled back.
    assert [d.real_current_minutes for c in twitch.inventory for d in c.drops] == [7, 7]


@pytest.mark.asyncio
async def test_validation_cpu_cannot_extend_batch_lifetime(progress, monkeypatch):
    state, reader = progress
    batch = await reader.read(prepare(progress))
    signature = reader._signature

    def slow(campaigns):
        value = signature(campaigns)
        state[5](16)
        return value

    monkeypatch.setattr(reader, "_signature", slow)
    assert reader.apply(batch) == "superseded"
    assert state[1].timed_drops["reward"].real_current_minutes == 0


@pytest.mark.asyncio
async def test_server_claim_is_not_hidden_by_newer_campaign_minutes(progress):
    state, reader = progress
    request = prepare(progress)
    payloads = [wire(campaign) for campaign in state[0].inventory]
    payloads[0]["timeBasedDrops"][0]["self"]["isClaimed"] = True
    transport(state[0], payloads)
    batch = await reader.read(request)
    drop = state[1].timed_drops["reward"]
    drop.update_minutes(9, source="websocket")
    assert reader.apply(batch) == "inventory_changed"
    assert drop.real_current_minutes == 9 and not drop.is_claimed
    assert state[2].timed_drops["urgent"].real_current_minutes == 0
