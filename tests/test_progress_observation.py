"""Direct Twitch readings are distinct from sibling inference and local estimates."""

import asyncio
import importlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.message_handlers import MessageHandlerService
from src.services.watch_service import WatchService
from src.utils.async_helpers import AwaitableValue
from src.web.managers.campaigns import CampaignProgressManager
from src.web.managers.inventory import InventoryManager
from tests.test_drop_ignore_policy import _campaign, _drop
from tests.test_special_game_watch import _channel


FIRST = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
SECOND = FIRST + timedelta(minutes=2)
MISSING = object()


def make_campaign(minutes=MISSING, *, claimed=False):
    data = _drop("drop", "Reward", required_minutes=60)
    if minutes is not MISSING:
        data["self"] = {
            "isClaimed": claimed, "dropInstanceID": None,
            "currentMinutesWatched": minutes,
        }
    with patch("src.models.drop.datetime", wraps=datetime) as clock:
        clock.now.return_value = FIRST
        return _campaign([data, _drop("sibling", "Sibling", required_minutes=60)])


@pytest.mark.parametrize("minutes", [0, 7, 60])
def test_inventory_records_direct_minutes_including_zero(minutes):
    drop = make_campaign(minutes).timed_drops["drop"]

    assert drop.observed_minutes == minutes
    assert drop.progress_observation == {
        "minutes": minutes, "source": "inventory", "received_at": FIRST.isoformat(),
    }


@pytest.mark.parametrize("minutes", [MISSING, None, True, False, "7", -1, 61, 1.5])
def test_missing_or_invalid_inventory_reading_is_unknown_not_confirmed_zero(minutes):
    drop = make_campaign(minutes).timed_drops["drop"]

    assert drop.observed_minutes is None
    assert drop.progress_observation is None


def test_claim_normalization_does_not_fabricate_a_minutes_observation():
    drop = make_campaign(5, claimed=True).timed_drops["drop"]

    assert drop.is_claimed
    assert drop.real_current_minutes == 60
    assert drop.observed_minutes == 5


def test_partial_inventory_self_without_minutes_is_unknown():
    data = _drop("drop", "Reward", required_minutes=60)
    data["self"] = {"isClaimed": False, "dropInstanceID": None}
    drop = _campaign([data]).timed_drops["drop"]

    assert drop.real_current_minutes == 0
    assert drop.observed_minutes is None
    assert drop.progress_observation is None


@pytest.mark.parametrize("source", ["websocket", "current_drop"])
@pytest.mark.parametrize("minutes", [0, 4])
def test_tagged_reading_changes_only_the_reported_rewards_provenance(source, minutes):
    campaign = make_campaign(0)
    drop, sibling = campaign.drops
    with patch("src.models.drop.datetime", wraps=datetime) as clock:
        clock.now.return_value = SECOND
        drop.update_minutes(minutes, source=source)

    assert drop.observed_minutes == minutes
    assert drop.progress_observation == {
        "minutes": minutes, "source": source, "received_at": SECOND.isoformat(),
    }
    assert sibling.real_current_minutes == minutes
    assert sibling.observed_minutes is None
    assert sibling.progress_observation is None
    campaign._twitch.gui.inv.update_drop.assert_any_call(drop)


def test_estimates_and_untagged_inference_do_not_create_or_refresh_readings():
    campaign = make_campaign(0)
    drop, sibling = campaign.drops
    channel = _channel(campaign._twitch)
    original = drop.progress_observation

    campaign.bump_minutes(channel)
    drop.update_minutes(3)
    sibling.update_minutes(4)

    assert drop.real_current_minutes == sibling.real_current_minutes == 4
    assert drop.progress_observation == original
    assert sibling.progress_observation is None


@pytest.mark.parametrize("minutes", [-1, 61])
def test_out_of_bounds_update_does_not_mislabel_a_clamped_value_as_a_reading(minutes):
    drop = make_campaign(0).timed_drops["drop"]
    original = drop.progress_observation

    drop.update_minutes(minutes, source="websocket")

    assert drop.progress_observation == original


def test_serialized_observation_is_a_copy():
    drop = make_campaign(7).timed_drops["drop"]
    exposed = drop.progress_observation
    exposed["minutes"] = 55

    assert drop.observed_minutes == 7


def test_unknown_source_does_not_enter_public_provenance():
    drop = make_campaign(0).timed_drops["drop"]
    original = drop.progress_observation

    drop.update_minutes(4, source="private-canary-unknown-source")

    assert drop.progress_observation == original


@pytest.mark.asyncio
async def test_successful_claim_does_not_fabricate_a_direct_minutes_reading():
    campaign = make_campaign(5)
    twitch = campaign._twitch
    twitch.settings.telegram_bot_token = ""
    twitch.settings.telegram_chat_id = ""
    twitch.gui.broadcast_wanted_items_now = AsyncMock()
    drop = campaign.timed_drops["drop"]
    drop._claim = AsyncMock(return_value=True)

    assert await drop.claim()

    assert drop.is_claimed and drop.current_minutes == 60
    assert drop.observed_minutes == 5
    assert drop.progress_observation["source"] == "inventory"


@pytest.mark.asyncio
async def test_unchanged_reading_refreshes_cached_inventory_and_progress_payloads(monkeypatch):
    campaign = make_campaign(0)
    drop = campaign.timed_drops["drop"]
    broadcaster = SimpleNamespace(emit=AsyncMock())
    inventory = InventoryManager(broadcaster, MagicMock())
    campaign._twitch.gui.inv = inventory
    await inventory.add_campaign(campaign)
    with patch("src.models.drop.datetime", wraps=datetime) as clock:
        clock.now.return_value = SECOND
        drop.update_minutes(0, source="websocket")
    await asyncio.sleep(0)

    expected = {"minutes": 0, "source": "websocket", "received_at": SECOND.isoformat()}
    assert inventory.get_campaigns()[0]["drops"][0]["progress_observation"] == expected
    web = importlib.import_module("src.web.app")
    monkeypatch.setattr(web, "gui_manager", SimpleNamespace(inv=inventory))
    assert (await web.get_campaigns())["campaigns"][0]["drops"][0]["progress_observation"] == expected

    progress = CampaignProgressManager(broadcaster)
    progress.update(drop, 3600)
    await asyncio.sleep(0)
    emitted = [args.args[1] for args in broadcaster.emit.await_args_list if args.args[0] == "drop_progress"]
    assert emitted[-1]["progress_observation"] == expected
    assert progress.get_current_drop()["progress_observation"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["websocket", "current_drop"])
async def test_transport_tags_an_unchanged_reading(source):
    campaign = make_campaign(0)
    twitch = campaign._twitch
    channel = _channel(twitch)
    selected = AwaitableValue()
    selected.set(channel)
    twitch.watching_channel = selected
    twitch._drops = dict(campaign.timed_drops)
    twitch.is_mining_time.return_value = True

    with patch("src.models.drop.datetime", wraps=datetime) as timestamp:
        timestamp.now.return_value = SECOND
        if source == "websocket":
            await MessageHandlerService(twitch).process_drops(1, {
                "type": "drop-progress",
                "data": {"drop_id": "drop", "current_progress_min": 0, "required_progress_min": 60},
            })
        else:
            clock = [0.0]
            twitch.gql_request = AsyncMock(return_value={"data": {"currentUser": {
                "dropCurrentSession": {"dropID": "drop", "currentMinutesWatched": 0},
            }}})
            service = WatchService(twitch)

            async def advance(delay):
                clock[0] += delay
                if clock[0] >= 30:
                    raise asyncio.CancelledError

            with (
                patch.object(type(channel), "send_watch", new=AsyncMock(return_value=True)),
                patch("src.services.watch_service.monotonic", side_effect=lambda: clock[0]),
                patch.object(service, "watch_sleep", side_effect=advance),
                pytest.raises(asyncio.CancelledError),
            ):
                await service.watch_loop()

    drop = campaign.timed_drops["drop"]
    assert drop.progress_observation == {
        "minutes": 0, "source": source, "received_at": SECOND.isoformat(),
    }
    assert campaign.timed_drops["sibling"].progress_observation is None
