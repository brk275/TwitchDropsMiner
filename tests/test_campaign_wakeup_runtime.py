"""Offline inventory registration -> maintenance delivery -> real miner selection.

Only transport, UI publication and the idle watch worker are synthetic. Campaigns
are installed by InventoryService, and boundaries are delivered by the real
MaintenanceService, not test calls to request_games_update. No sessions or saved
settings are loaded. These tests establish local selection, not Twitch progress.
"""

import asyncio
import socket
from collections import OrderedDict, deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from src.config import GQL_OPERATIONS, State
from src.core.client import Twitch
from src.models.channel import Channel, Stream
from src.models.claim_state import ClaimState
from src.services.channel_service import ChannelService
from src.services.deadline_service import DeadlineService
from src.services.inventory_service import InventoryService
from src.services.maintenance import MaintenanceService
from src.services.stream_selector import StreamSelector
from src.services.watch_service import WatchService
from src.utils import AwaitableValue


BASE = datetime(2030, 1, 1, tzinfo=timezone.utc)
ADMISSION = BASE + timedelta(minutes=30, microseconds=1)


def at(minutes):
    return BASE + timedelta(minutes=minutes)


def drop_data(key, *, start=90, end=150, benefit="DIRECT_ENTITLEMENT", parents=()):
    return {
        "id": key, "name": key + " reward",
        "startAt": at(start).isoformat(), "endAt": at(end).isoformat(),
        "requiredMinutesWatched": 15,
        "preconditionDrops": [{"id": parent} for parent in parents],
        "benefitEdges": [{"benefit": {
            "id": key + "-benefit", "name": key + " benefit",
            "distributionType": benefit, "imageAssetURL": "https://example.test/reward.png",
        }}],
        "self": {"isClaimed": False, "dropInstanceID": None, "currentMinutesWatched": 0},
    }


def campaign_data(name="High", game_id=1, *, start=90, end=150, drops=None, linked=True):
    return {
        "id": name, "name": name, "game": {"id": str(game_id), "name": name},
        "self": {"isAccountConnected": linked}, "accountLinkURL": "https://example.test/link",
        "startAt": at(start).isoformat(), "endAt": at(end).isoformat(),
        "status": "UPCOMING" if start > 0 else "ACTIVE",
        "allow": {"channels": [], "isEnabled": True},
        "timeBasedDrops": drops if drops is not None else [
            drop_data(name + "-drop", start=start, end=end),
        ],
    }


class ParkedEvent(asyncio.Event):
    """Observe the real state loop reaching an unset-event wait."""

    def __init__(self):
        super().__init__()
        self.parked = asyncio.Event()

    async def wait(self):
        if not self.is_set():
            self.parked.set()
        return await super().wait()


class Runtime:
    def __init__(self, monkeypatch):
        self.now = BASE
        self.opened = True
        self.records = []
        self.directory = {}
        self.sleeps = asyncio.Queue()
        self.tasks = set()
        self.trace = []
        self.fetch_gate = None
        self.run = self.timer = None

        class Clock:
            @staticmethod
            def now(tz=None):
                return self.now.astimezone(tz) if tz else self.now.replace(tzinfo=None)

        for module in (
            "src.models.campaign", "src.models.drop", "src.services.stream_selector",
            "src.services.inventory_service", "src.services.maintenance", "src.core.client",
        ):
            monkeypatch.setattr(module + ".datetime", Clock)
        # Replacing the module reference leaves the event loop and schedule sleep alone.
        monkeypatch.setattr("src.services.maintenance.asyncio", SimpleNamespace(sleep=self.sleep))

        owner = self.owner = Twitch.__new__(Twitch)
        owner.settings = SimpleNamespace(
            games_to_watch=["High"], priority_list_only=True,
            mining_benefits={"DIRECT_ENTITLEMENT": True, "BADGE": False},
            drop_name_blacklist=[], allow_unlinked_campaigns=False,
            minimum_refresh_interval_minutes=1440, mining_hours={"mode": "always"},
            save=MagicMock(),
        )
        owner._state = State.IDLE
        owner._state_change = ParkedEvent()
        owner._games_update_pending = owner._deadline_update_pending = False
        owner._inventory_loaded = owner._inventory_refresh_pending = False
        owner._clear_cache_pending = False
        owner._claim_state = ClaimState()
        owner.inventory, owner.wanted_games = [], []
        owner._campaigns, owner._drops = {}, {}
        owner._mnt_triggers = deque()
        owner.channels = OrderedDict()
        owner.watching_channel = AwaitableValue()
        owner._manual_target_channel = owner._manual_target_game = None
        owner._watching_task = owner._mnt_task = owner._schedule_task = None
        owner._watching_restart, owner._schedule_changed = asyncio.Event(), asyncio.Event()
        owner._resume_mining = asyncio.Event()
        owner._browser = None
        owner.gui = MagicMock()
        owner.gui.inv.add_campaign = AsyncMock()
        owner.gui.channels.get_selection.return_value = None
        owner.websocket = MagicMock(start=AsyncMock())
        owner.get_auth = AsyncMock(return_value=SimpleNamespace(user_id=42))
        owner._ensure_api_clients = MagicMock()
        owner.is_mining_time = lambda: self.opened
        owner.gql_request = AsyncMock(side_effect=self.gql)
        owner.request = MagicMock(side_effect=AssertionError("No HTTP permitted"))
        owner.get_live_streams = AsyncMock(side_effect=self.discover)
        owner.bulk_check_online = AsyncMock()
        owner.drop_history = SimpleNamespace(total_count=0, record=MagicMock())
        owner._stream_selector = StreamSelector()
        select = owner._stream_selector.get_wanted_games

        def observe_selection(*args):
            self.trace.append("selection")
            return select(*args)

        owner._stream_selector.get_wanted_games = MagicMock(side_effect=observe_selection)
        owner._channel_service = ChannelService(owner)
        owner._channel_service.filter_banned_channels = AsyncMock(
            side_effect=lambda channels, **kwargs: list(channels),
        )
        owner._watch_service = WatchService(owner)
        owner._watch_service.watch_loop = self.idle_watch
        owner._inventory_service = InventoryService(owner)
        owner.fetch_inventory = AsyncMock(wraps=owner._inventory_service.fetch_inventory)
        owner._maintenance_service = MaintenanceService(owner)
        owner._message_handler_service = MagicMock()
        owner._deadline_service = DeadlineService(owner, enabled=False)
        owner.request_games_update = MagicMock(wraps=owner.request_games_update)

    async def gql(self, operations):
        if isinstance(operations, list):
            assert all(
                op["operationName"] == GQL_OPERATIONS["CampaignDetails"]["operationName"]
                for op in operations
            )
            records = {record["id"]: record for record in self.records}
            return [{"data": {"user": {"dropCampaign": deepcopy(
                records[op["variables"]["dropID"]],
            )}}} for op in operations]
        name = operations["operationName"]
        if name == "Inventory":
            self.trace.append("inventory")
            if self.fetch_gate is not None:
                entered, release = self.fetch_gate
                entered.set()
                await release.wait()
            return {"data": {"currentUser": {"inventory": {
                "dropCampaignsInProgress": [], "gameEventDrops": [],
            }}}}
        assert name == GQL_OPERATIONS["Campaigns"]["operationName"], "Unexpected GQL operation"
        return {"data": {"currentUser": {"dropCampaigns": [
            {key: deepcopy(record[key]) for key in ("id", "name", "game", "status")}
            for record in self.records
        ]}}}

    def channel(self, name):
        game = self.owner._campaigns[name].game
        if game.id not in self.directory:
            channel = Channel(self.owner, id=game.id, login="fixture" + str(game.id), acl_based=False)
            channel._stream = Stream(
                channel, id=game.id, game={"id": str(game.id), "name": game.name},
                viewers=10, title="Offline fixture",
            )
            channel._stream.drops_enabled = True
            self.directory[game.id] = channel
        return self.directory[game.id]

    async def discover(self, game, **kwargs):
        assert kwargs == {"drops_enabled": True}
        return [self.channel(game.name)]

    async def idle_watch(self):
        self.tasks.add(asyncio.current_task())
        await asyncio.Event().wait()

    async def sleep(self, seconds):
        assert seconds >= 0
        task = asyncio.current_task()
        self.tasks.add(task)
        future = asyncio.get_running_loop().create_future()
        await self.sleeps.put(SimpleNamespace(
            deadline=self.now + timedelta(seconds=seconds), future=future, task=task,
        ))
        await future

    async def next_timer(self):
        while True:
            timer = await asyncio.wait_for(self.sleeps.get(), 2)
            if not timer.future.cancelled():
                self.timer = timer
                return timer

    async def settled(self):
        observer = asyncio.create_task(self.owner._state_change.parked.wait())
        try:
            done, _ = await asyncio.wait(
                (observer, self.run), timeout=2, return_when=asyncio.FIRST_COMPLETED,
            )
            if self.run.done():
                self.run.result()
                pytest.fail("Miner exited instead of reaching its next wait")
            assert observer in done, "Miner did not reach its next unset-event wait"
        finally:
            observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)
        assert not self.owner._state_change.is_set()

    async def start(self, *records):
        self.records = list(records)
        self.run = asyncio.create_task(self.owner._run())
        self.tasks.add(self.run)
        await self.settled()
        await self.next_timer()

    async def deliver(self, deadline, *, settled=True, wake_at=None):
        assert self.timer.deadline == deadline
        assert not self.timer.future.done()
        self.owner._state_change.parked.clear()
        self.now = wake_at if wake_at is not None else deadline
        self.timer.future.set_result(None)
        await self.next_timer()
        if settled:
            await self.settled()

    async def set_hours(self, opened):
        self.owner._state_change.parked.clear()
        self.opened = opened
        self.owner._schedule_changed.set()
        await self.settled()

    def assert_no_refresh(self):
        self.owner.fetch_inventory.assert_awaited_once()
        assert self.owner.gql_request.await_count == 3

    async def stop(self):
        self.owner.close()
        self.tasks.update(task for task in (
            self.owner._watching_task, self.owner._mnt_task, self.owner._schedule_task,
        ) if task is not None)
        for task in self.tasks:
            task.cancel()
        results = await asyncio.gather(*self.tasks, return_exceptions=True)
        assert all(task.done() for task in self.tasks)
        errors = [result for result in results
                  if result is not None and not isinstance(result, asyncio.CancelledError)]
        assert not errors, errors
        self.owner.request.assert_not_called()
        self.owner.settings.save.assert_not_called()
        self.owner.drop_history.record.assert_not_called()
        assert self.owner.drop_history.total_count == 0


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Offline runtime tests forbid network access and real sessions")

    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr("aiohttp.ClientSession", denied)


@pytest_asyncio.fixture
async def runtime(monkeypatch):
    result = Runtime(monkeypatch)
    try:
        yield result
    finally:
        await result.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", [120, 1440])
@pytest.mark.parametrize("drop_only", [False, True])
async def test_empty_queue_admits_then_watches_at_real_start(runtime, interval, drop_only):
    owner = runtime.owner
    owner.settings.minimum_refresh_interval_minutes = interval
    data = campaign_data(end=110)
    if drop_only:
        data["startAt"] = at(-120).isoformat()
        data["status"] = "ACTIVE"
    await runtime.start(data)
    timer_owner = owner._mnt_task
    target = runtime.channel("High")
    assert owner._state is State.IDLE and owner.wanted_games == []
    assert not owner.watching_channel.has_value()
    owner.get_live_streams.assert_not_awaited()

    runtime.now = at(30)
    assert runtime.timer.deadline == ADMISSION
    assert owner.wanted_games == [] and not owner.can_watch(target)
    await runtime.deliver(ADMISSION)
    assert [game.name for game in owner.wanted_games] == ["High"]
    assert owner.channels[target.id] is target
    assert not owner.can_watch(target) and not owner.watching_channel.has_value()
    owner.get_live_streams.assert_awaited_once()

    await runtime.deliver(at(90))
    assert owner.watching_channel.get_with_default(None) is target
    assert owner.get_active_campaign(target).first_mining_drop(target).id == "High-drop"
    assert owner._mnt_task is timer_owner
    assert runtime.timer.deadline == at(110)
    assert owner.request_games_update.call_count == 2
    runtime.assert_no_refresh()
    assert owner.settings.games_to_watch == ["High"]


@pytest.mark.asyncio
async def test_fallback_continues_until_start_and_returns_after_expiry(runtime):
    owner = runtime.owner
    owner.settings.priority_list_only = False
    await runtime.start(
        campaign_data(), campaign_data("Other", 2, start=-120, end=2000),
    )
    fallback = runtime.channel("Other")
    target = runtime.channel("High")
    assert owner.watching_channel.get_with_default(None) is fallback
    assert [game.name for game in owner.wanted_games] == ["Other"]
    await runtime.deliver(ADMISSION)
    assert [game.name for game in owner.wanted_games] == ["High", "Other"]
    assert owner.channels[target.id] is target
    assert owner.watching_channel.get_with_default(None) is fallback
    assert not owner.can_watch(target)
    await runtime.deliver(at(90))
    assert owner.watching_channel.get_with_default(None) is target
    await runtime.deliver(at(150))
    assert owner.watching_channel.get_with_default(None) is fallback
    assert [game.name for game in owner.wanted_games] == ["Other"]
    assert runtime.timer.deadline == at(1440)
    assert owner.request_games_update.call_count == 3
    assert owner._drops["High-drop"].real_current_minutes == 0
    assert not owner._drops["High-drop"].is_claimed
    assert owner.settings.games_to_watch == ["High"]
    runtime.assert_no_refresh()


@pytest.mark.asyncio
async def test_delayed_wakeup_skips_expired_target_without_interrupting_fallback(runtime):
    owner = runtime.owner
    owner.settings.priority_list_only = False
    await runtime.start(
        campaign_data(), campaign_data("Other", 2, start=-120, end=2000),
    )
    fallback = runtime.channel("Other")
    assert owner.watching_channel.get_with_default(None) is fallback
    await runtime.deliver(ADMISSION, wake_at=at(160))
    assert owner.watching_channel.get_with_default(None) is fallback
    assert [game.name for game in owner.wanted_games] == ["Other"]
    assert all(call.args[0].name == "Other" for call in owner.get_live_streams.await_args_list)
    owner.request_games_update.assert_called_once_with()
    assert runtime.timer.deadline == at(1440)
    runtime.assert_no_refresh()


@pytest.mark.asyncio
async def test_future_target_admission_starts_disabled_type_prerequisite_before_it_expires(runtime):
    owner = runtime.owner
    await runtime.start(campaign_data(start=-120, end=180, drops=[
        drop_data("prerequisite", start=-120, end=45, benefit="BADGE"),
        drop_data("target", parents=("prerequisite",)),
    ]))
    assert owner.wanted_games == []
    await runtime.deliver(ADMISSION)
    channel = runtime.channel("High")
    assert owner.watching_channel.get_with_default(None) is channel
    campaign = owner.get_active_campaign(channel)
    assert campaign.first_mining_drop(channel).id == "prerequisite"
    assert runtime.now < owner._drops["prerequisite"].ends_at
    assert not owner._drops["target"].can_earn(channel)
    assert owner.settings.mining_benefits["BADGE"] is False
    await runtime.deliver(at(45))
    assert owner.wanted_games == [] and not owner.watching_channel.has_value()
    await runtime.deliver(at(90))
    assert not owner.watching_channel.has_value()
    assert all(not drop.is_claimed and drop.real_current_minutes == 0 for drop in campaign.drops)
    runtime.assert_no_refresh()


@pytest.mark.asyncio
@pytest.mark.parametrize("preference", ["benefit", "link", "ignore", "games"])
async def test_disabled_known_campaign_keeps_future_wakeup_after_preference_enable(runtime, preference):
    owner = runtime.owner
    data = campaign_data(linked=preference != "link")
    if preference == "benefit":
        data["timeBasedDrops"][0]["benefitEdges"][0]["benefit"]["distributionType"] = "BADGE"
    elif preference == "ignore":
        owner.settings.drop_name_blacklist = ["reward"]
    elif preference == "games":
        owner.settings.games_to_watch = ["Unlisted"]
    await runtime.start(data)
    assert owner.wanted_games == []
    runtime.now = at(10)
    # No settings wakeup or refetch is used to stand in for the future timer.
    if preference == "benefit":
        owner.settings.mining_benefits["BADGE"] = True
    elif preference == "link":
        owner.settings.allow_unlinked_campaigns = True
    elif preference == "ignore":
        owner.settings.drop_name_blacklist = []
    else:
        owner.settings.games_to_watch = ["High"]
    await runtime.deliver(ADMISSION)
    assert [game.name for game in owner.wanted_games] == ["High"]
    assert not owner.watching_channel.has_value()
    await runtime.deliver(at(90))
    assert owner.watching_channel.get_with_default(None) is runtime.channel("High")
    assert owner.request_games_update.call_count == 2
    runtime.assert_no_refresh()


@pytest.mark.asyncio
@pytest.mark.parametrize("expires", [False, True])
async def test_boundary_preserves_manual_intent_until_its_work_expires(runtime, expires):
    owner = runtime.owner
    owner.settings.games_to_watch = ["High", "Manual"]
    await runtime.start(
        campaign_data(), campaign_data("Manual", 2, start=-120, end=90 if expires else 2000),
    )
    manual = runtime.channel("Manual")
    assert owner.watching_channel.get_with_default(None) is manual
    owner.enter_manual_mode(manual)
    await runtime.deliver(ADMISSION)
    assert owner.is_manual_mode()
    assert [game.name for game in owner.wanted_games] == ["Manual", "High"]
    assert owner.watching_channel.get_with_default(None) is manual
    await runtime.deliver(at(90))
    assert owner.is_manual_mode() is not expires
    expected = runtime.channel("High") if expires else manual
    assert owner.watching_channel.get_with_default(None) is expected
    assert owner.wanted_games[0] == expected.game
    assert owner.settings.games_to_watch == ["High", "Manual"]
    runtime.assert_no_refresh()


@pytest.mark.asyncio
async def test_closed_hours_defer_real_boundaries_and_refresh_before_reopening_selection(runtime):
    owner = runtime.owner
    await runtime.start(campaign_data())
    await runtime.set_hours(False)
    initial = owner._stream_selector.get_wanted_games.call_count
    await runtime.deliver(ADMISSION)
    await runtime.deliver(at(90))
    assert owner._state is State.IDLE and owner._games_update_pending
    assert owner._stream_selector.get_wanted_games.call_count == initial
    assert not owner._state_change.is_set() and not owner.watching_channel.has_value()
    owner.get_live_streams.assert_not_awaited()
    runtime.assert_no_refresh()

    runtime.now = at(100)
    runtime.trace.clear()
    await runtime.set_hours(True)
    await runtime.next_timer()
    assert runtime.trace == ["inventory", "selection"]
    assert owner.fetch_inventory.await_count == 2
    assert owner.watching_channel.get_with_default(None) is runtime.channel("High")
    assert not owner._games_update_pending and not owner._inventory_refresh_pending
    assert runtime.timer.deadline == at(150)


@pytest.mark.asyncio
@pytest.mark.parametrize("retained", [False, True])
async def test_delivered_boundary_cannot_preempt_in_flight_inventory_snapshot(runtime, retained):
    owner = runtime.owner
    await runtime.start(campaign_data())
    entered, release = asyncio.Event(), asyncio.Event()
    runtime.fetch_gate = entered, release
    initial = owner._stream_selector.get_wanted_games.call_count
    runtime.now = at(20)
    owner._state_change.parked.clear()
    assert owner.request_inventory_refresh()
    await asyncio.wait_for(entered.wait(), 2)
    assert owner._state is State.INVENTORY_FETCH
    await runtime.deliver(ADMISSION, settled=False)
    assert owner.request_games_update.call_count == 1
    assert owner._state is State.INVENTORY_FETCH and owner._games_update_pending
    assert owner._stream_selector.get_wanted_games.call_count == initial
    if not retained:
        runtime.records.clear()
    runtime.fetch_gate = None
    release.set()
    await runtime.settled()
    await runtime.next_timer()
    assert owner.fetch_inventory.await_count == 2
    assert owner._stream_selector.get_wanted_games.call_count == initial + 1
    assert [game.name for game in owner.wanted_games] == (["High"] if retained else [])
    assert not owner._games_update_pending and not owner._inventory_refresh_pending
    if retained:
        await runtime.deliver(at(90))
        assert owner.watching_channel.get_with_default(None) is runtime.channel("High")
    else:
        owner.get_live_streams.assert_not_awaited()
        assert not owner.watching_channel.has_value()


@pytest.mark.asyncio
@pytest.mark.parametrize("early_by", [timedelta(microseconds=1), timedelta(seconds=1)])
async def test_early_admission_wake_is_retained_until_due(runtime, early_by):
    owner = runtime.owner
    await runtime.start(campaign_data(start=-120, end=180, drops=[
        drop_data("preparation", start=-120, end=45, benefit="BADGE"),
        drop_data("wanted", parents=("preparation",)),
    ]))
    await runtime.deliver(ADMISSION, settled=False, wake_at=ADMISSION - early_by)
    assert runtime.timer.deadline == ADMISSION
    owner.request_games_update.assert_not_called()
    assert owner.wanted_games == []
    await runtime.deliver(ADMISSION)
    channel = runtime.channel("High")
    assert owner.watching_channel.get_with_default(None) is channel
    assert owner.get_active_campaign(channel).first_mining_drop(channel).id == "preparation"
    runtime.assert_no_refresh()


@pytest.mark.asyncio
async def test_late_maintenance_delivery_cannot_reactivate_exited_runtime(runtime):
    owner = runtime.owner
    await runtime.start(campaign_data())
    initial = owner._stream_selector.get_wanted_games.call_count
    owner.close()
    await asyncio.wait_for(runtime.run, 2)
    await runtime.deliver(ADMISSION, settled=False)
    assert owner._state is State.EXIT and not owner._games_update_pending
    owner.request_games_update.assert_called_once_with()
    assert owner._stream_selector.get_wanted_games.call_count == initial
    assert owner.wanted_games == [] and not owner.watching_channel.has_value()
    owner.get_live_streams.assert_not_awaited()
    runtime.assert_no_refresh()
