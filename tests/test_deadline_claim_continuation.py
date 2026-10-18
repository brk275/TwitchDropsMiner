"""Public confirmed claims continue a conditional calendar, never rebase it."""

import asyncio
import copy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.auth.browser_session import BrowserIdentity
from src.config import ClientType, State
from src.core.client import Twitch
from src.drop_history import DropHistory
from src.models.drop import TimedDrop
from src.services.message_handlers import MessageHandlerService
from tests.test_deadline_forecast import NOW
from tests.test_deadline_planner import HORIZON
from tests.test_deadline_queue import runtime as queue_runtime
from tests.test_drop_ignore_policy import _drop


@pytest.fixture
def runtime(monkeypatch):
    return queue_runtime.__wrapped__(monkeypatch)


def prepare_claim(run, drop, tmp_path, monkeypatch, *, status="ELIGIBLE_FOR_ALL"):
    run.twitch.drop_history = DropHistory(tmp_path)
    monkeypatch.setattr(run.twitch, "gql_request", AsyncMock(return_value={
        "data": {"claimDropRewards": {"status": status}},
    }))
    run.twitch.gui.broadcast_wanted_items_now = AsyncMock()
    monkeypatch.setattr(drop, "_send_telegram_notification", AsyncMock())
    monkeypatch.setattr(drop, "_send_discord_notification", AsyncMock())


def stop_at_policy_replay(run, monkeypatch):
    """Observe real policy activation, then stop before unrelated fetch/network work."""
    entered = asyncio.Event()
    records = []
    activate = run.twitch._activate_pending_games_update

    def observe():
        activate()
        if run.twitch._state in (State.GAMES_UPDATE, State.INVENTORY_FETCH):
            records.append((run.twitch._state, run.twitch._games_update_pending,
                            run.twitch._deadline_claim_update_pending))
            entered.set()
            run.twitch.close()

    monkeypatch.setattr(run.twitch, "_activate_pending_games_update", observe)
    return entered, records


async def reach_protected_after_claim(run, tmp_path, monkeypatch):
    certificate = await run.reserve()
    assert (await run.tick(3)).action == "watch"
    urgent = run.urgent.timed_drops["urgent"]
    prepare_claim(run, urgent, tmp_path, monkeypatch)
    run.move(60)
    assert await urgent.claim() is True
    assert (await run.next()).action == "stop"
    first, protected = certificate.slots
    assert (await run.tick((first.until - NOW).total_seconds(), refresh=True)).action == "wait"
    assert (await run.tick((protected.connect_at - NOW).total_seconds(), refresh=True)).action == "watch"
    high = run.high.timed_drops["reward"]
    prepare_claim(run, high, tmp_path, monkeypatch)
    return certificate, high


@pytest.mark.asyncio
async def test_public_claim_keeps_original_calendar_and_booked_idle(runtime, tmp_path, monkeypatch):
    async with runtime() as run:
        certificate = await run.reserve()
        first, protected = certificate.slots
        assert (await run.tick(3)).action == "watch"
        drop = run.urgent.timed_drops["urgent"]
        prepare_claim(run, drop, tmp_path, monkeypatch)
        observation = drop.progress_observation
        run.move(60)

        assert await drop.claim() is True
        assert run.executor._reservation is not None
        assert run.executor._reservation.certificate is certificate
        assert drop.progress_observation == observation
        assert run.twitch.drop_history.total_count == 1
        assert (await run.next()).action == "stop"
        assert run.twitch.watching_channel.get_with_default(None) is None
        assert (await run.tick(61)).action == "wait"
        assert run.twitch.watch.call_count == 1
        assert (await run.tick((first.until - NOW).total_seconds(), refresh=True)).action == "wait"
        assert run.executor._reservation.certificate is certificate
        assert (await run.tick((protected.connect_at - NOW).total_seconds(), refresh=True)).action == "watch"
        assert [call.args[0].id for call in run.twitch.watch.call_args_list] == [20, 10]
        assert run.twitch.settings.games_to_watch == ["High", "Urgent"]
        assert protected.connect_at > NOW + timedelta(hours=2)


@pytest.mark.asyncio
async def test_claim_validation_processing_cannot_outlive_original_settlement_budget(runtime, tmp_path, monkeypatch):
    async with runtime() as run:
        certificate = await run.reserve()
        assert (await run.tick(3)).action == "watch"
        urgent = run.urgent.timed_drops["urgent"]
        prepare_claim(run, urgent, tmp_path, monkeypatch)
        run.move(60)
        assert await urgent.claim() is True
        assert (await run.next()).action == "stop"
        first, protected = certificate.slots
        assert (await run.tick((first.until - NOW).total_seconds(), refresh=True)).action == "wait"
        assert (await run.tick((protected.connect_at - NOW).total_seconds(), refresh=True)).action == "watch"
        drop = run.high.timed_drops["reward"]
        prepare_claim(run, drop, tmp_path, monkeypatch)
        completion = next(item for item in certificate.completions if item.drop_id == protected.drop_id)
        limit = (completion.claim_ready_at - NOW).total_seconds()
        run.move(limit - 0.1, refresh=True)
        entered, records = stop_at_policy_replay(run, monkeypatch)
        recognizes = run.executor.recognizes_claim
        checked = []

        def slow_claim(*args):
            result = recognizes(*args)
            if asyncio.current_task() is run.twitch._run_task:
                checked.append(result)
                run.move(limit + 0.1)
            return result

        monkeypatch.setattr(run.executor, "recognizes_claim", slow_claim)
        assert await drop.claim() is True
        assert await run.next() is None
        assert checked == [True]
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert records == [(State.GAMES_UPDATE, True, False)]
        assert run.executor._reservation is None


@pytest.mark.parametrize("status", ["ELIGIBLE_FOR_ALL", "DROP_INSTANCE_ALREADY_CLAIMED"])
@pytest.mark.asyncio
async def test_websocket_claim_and_duplicate_skip_only_owned_legacy_followup(runtime, tmp_path, monkeypatch, status):
    async with runtime() as run:
        certificate = await run.reserve()
        assert (await run.tick(3)).action == "watch"
        drop = run.urgent.timed_drops["urgent"]
        prepare_claim(run, drop, tmp_path, monkeypatch, status=status)
        run.move(60)
        sleeper = AsyncMock(side_effect=AssertionError("Legacy claim polling must not run"))
        monkeypatch.setattr("src.services.message_handlers.asyncio", SimpleNamespace(sleep=sleeper))
        run.twitch.request_inventory_refresh.reset_mock()
        restart = MagicMock(wraps=run.twitch.restart_watching)
        monkeypatch.setattr(run.twitch, "restart_watching", restart)
        message = {"type": "drop-claim", "data": {"drop_id": drop.id, "drop_instance_id": "test-confirmation"}}
        handler = MessageHandlerService(run.twitch)

        await handler.process_drops(42, message)
        await handler.process_drops(42, message)
        assert run.twitch.owns_deadline_claim_followup(drop) is True
        assert (await run.next()).action == "stop"
        assert run.twitch.owns_deadline_claim_followup(drop) is True
        assert run.executor._reservation.certificate is certificate
        assert run.twitch.drop_history.total_count == 1
        run.twitch.gql_request.assert_awaited_once()
        run.twitch.gui.broadcast_wanted_items_now.assert_awaited_once()
        drop._send_telegram_notification.assert_awaited_once()
        drop._send_discord_notification.assert_awaited_once()
        sleeper.assert_not_awaited()
        restart.assert_not_called()
        run.twitch.request_inventory_refresh.assert_not_called()
        assert run.twitch._deadline_claim_update_pending is True
        assert run.twitch._games_update_pending is False
        assert not run.queue._pending_claims and list(run.queue._handled_claims.values()) == [drop]


@pytest.mark.asyncio
async def test_default_none_keeps_ordinary_claim_and_websocket_refresh(runtime, tmp_path, monkeypatch):
    async with runtime() as run:
        run.twitch._set_deadline_queue(None)
        drop = run.high.timed_drops["reward"]
        prepare_claim(run, drop, tmp_path, monkeypatch)
        run.twitch.gql_request.side_effect = [
            {"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}},
            {"data": {"currentUser": {"dropCurrentSession": None}}},
        ]
        sleeper = AsyncMock()
        monkeypatch.setattr("src.services.message_handlers.asyncio", SimpleNamespace(sleep=sleeper))
        request = MagicMock(wraps=run.twitch.request_games_update)
        monkeypatch.setattr(run.twitch, "request_games_update", request)
        run.twitch.request_inventory_refresh.reset_mock()

        await MessageHandlerService(run.twitch).process_drops(42, {
            "type": "drop-claim", "data": {"drop_id": drop.id, "drop_instance_id": "test-confirmation"},
        })
        request.assert_called_once_with()
        assert run.twitch._games_update_pending is True
        assert run.twitch._deadline_claim_update_pending is False
        assert run.twitch.gql_request.await_count == 2
        sleeper.assert_awaited_once_with(4)
        run.twitch.request_inventory_refresh.assert_called_once_with()
        assert run.twitch.owns_deadline_claim_followup(drop) is False


@pytest.mark.parametrize("status", ["DROP_INSTANCE_NOT_ELIGIBLE", "UNKNOWN_STATUS"])
@pytest.mark.asyncio
async def test_unconfirmed_claim_does_not_defer_or_manufacture_settlement(runtime, tmp_path, monkeypatch, status):
    async with runtime() as run:
        certificate = await run.reserve()
        assert (await run.tick(3)).action == "watch"
        drop = run.urgent.timed_drops["urgent"]
        prepare_claim(run, drop, tmp_path, monkeypatch, status=status)
        assert await drop.claim() is False
        assert drop.is_claimed is False and run.twitch.drop_history.total_count == 0
        assert run.executor._reservation.certificate is certificate
        assert not run.queue._pending_claims and not run.queue._handled_claims
        assert not getattr(run.twitch, "_deadline_claim_update_pending", False)
        assert run.twitch._games_update_pending is False
        assert run.twitch.owns_deadline_claim_followup(drop) is False


@pytest.mark.parametrize("boundary", ["before", "after"])
@pytest.mark.asyncio
async def test_claim_actor_cancellation_preserves_only_confirmed_continuation(runtime, tmp_path, monkeypatch, boundary):
    async with runtime() as run:
        certificate = await run.reserve()
        assert (await run.tick(3)).action == "watch"
        drop = run.urgent.timed_drops["urgent"]
        prepare_claim(run, drop, tmp_path, monkeypatch)
        entered = asyncio.Event()

        async def block(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        if boundary == "before":
            run.twitch.gql_request.side_effect = block
        else:
            run.twitch.gui.broadcast_wanted_items_now.side_effect = block
        task = asyncio.create_task(drop.claim())
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert drop.is_claimed is (boundary == "after")
        assert run.twitch.drop_history.total_count == int(boundary == "after")
        assert run.executor._reservation.certificate is certificate
        if boundary == "after":
            assert (await run.next()).action == "stop"
            assert await drop.claim() is True
            assert run.twitch.drop_history.total_count == 1
            assert run.twitch.owns_deadline_claim_followup(drop) is True
        else:
            assert not run.queue._pending_claims and not run.queue._handled_claims
            assert run.twitch.owns_deadline_claim_followup(drop) is False


@pytest.mark.parametrize("kind", ["foreign_model", "before_admission", "unbooked"])
@pytest.mark.asyncio
async def test_unrecognized_claim_replays_ordinary_policy(runtime, tmp_path, monkeypatch, kind):
    async with runtime() as run:
        extra = None
        if kind == "unbooked":
            extra = TimedDrop(run.urgent, _drop("ignored", "Ignored", required_minutes=30), {})
            extra.update_claim("test-ignored-claim")
            run.urgent.timed_drops[extra.id] = extra
            run.twitch._drops[extra.id] = extra
            run.twitch.settings.drop_name_blacklist = ["Ignored"]
        await run.reserve()
        if kind != "before_admission":
            assert (await run.tick(3)).action == "watch"
        drop = extra or run.urgent.timed_drops["urgent"]
        if kind == "foreign_model":
            drop = copy.copy(drop)
        prepare_claim(run, drop, tmp_path, monkeypatch)
        entered, records = stop_at_policy_replay(run, monkeypatch)
        assert await drop.claim() is True
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert records == [(State.GAMES_UPDATE, True, False)]
        assert run.executor._reservation is None and run.queue._timer is None
        assert run.twitch.owns_deadline_claim_followup(drop) is False


@pytest.mark.parametrize("change", ["drop_index", "inventory", "rank", "unknown_progress", "selected"])
@pytest.mark.asyncio
async def test_claim_intent_is_not_authority_after_context_changes(runtime, tmp_path, monkeypatch, change):
    async with runtime() as run:
        await run.reserve()
        assert (await run.tick(3)).action == "watch"
        drop = run.urgent.timed_drops["urgent"]
        prepare_claim(run, drop, tmp_path, monkeypatch)
        entered, records = stop_at_policy_replay(run, monkeypatch)
        assert await drop.claim() is True
        if change == "drop_index":
            run.twitch._drops = dict(run.twitch._drops)
        elif change == "inventory":
            run.twitch.inventory = list(run.twitch.inventory)
        elif change == "rank":
            run.twitch.settings.games_to_watch.reverse()
        elif change == "unknown_progress":
            run.high.timed_drops["reward"]._progress_observation = None
        else:
            run.twitch.gui._selected_channel_id = 10

            def selected():
                run.twitch.gui._selected_channel_id = None
                return run.twitch.channels[10]

            run.twitch.gui.channels.get_selection.side_effect = selected
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert records == [(State.GAMES_UPDATE, True, False)]
        assert run.executor._reservation is None and run.queue._timer is None
        assert not run.queue._pending_claims and not run.queue._handled_claims


@pytest.mark.parametrize("operation", ["cancel", "disable", "detach", "games", "restart", "manual", "inventory", "cache"])
@pytest.mark.asyncio
async def test_interrupted_continuation_replays_deferred_policy(runtime, tmp_path, monkeypatch, operation):
    async with runtime() as run:
        await run.reserve()
        assert (await run.tick(3)).action == "watch"
        drop = run.urgent.timed_drops["urgent"]
        prepare_claim(run, drop, tmp_path, monkeypatch)
        assert await drop.claim() is True
        assert (await run.next()).action == "stop"
        assert run.twitch._deadline_claim_update_pending is True
        entered, records = stop_at_policy_replay(run, monkeypatch)
        if operation == "cancel":
            run.queue.cancel()
        elif operation == "disable":
            assert not run.queue.request(horizon=HORIZON, enabled=False)
        elif operation == "detach":
            run.twitch._set_deadline_queue(None)
        elif operation == "games":
            run.twitch.request_games_update()
        elif operation == "restart":
            run.twitch.restart_watching()
        elif operation == "manual":
            run.twitch.enter_manual_mode(run.twitch.channels[10])
        else:
            Twitch.request_inventory_refresh(run.twitch, clear_cache=operation == "cache")
        await asyncio.wait_for(entered.wait(), timeout=2)
        expected = State.INVENTORY_FETCH if operation in ("inventory", "cache") else State.GAMES_UPDATE
        assert records == [(expected, True, False)]
        assert run.executor._reservation is None and run.queue._timer is None
        assert not run.queue._pending_claims and not run.queue._handled_claims


@pytest.mark.asyncio
async def test_close_discards_deferred_work_without_new_policy_or_claims(runtime, tmp_path, monkeypatch):
    async with runtime() as run:
        await run.reserve()
        assert (await run.tick(3)).action == "watch"
        drop = run.urgent.timed_drops["urgent"]
        prepare_claim(run, drop, tmp_path, monkeypatch)
        assert await drop.claim() is True
        assert (await run.next()).action == "stop"
        run.twitch.close()
        await asyncio.wait_for(run.root, timeout=2)
        assert run.twitch._deadline_claim_update_pending is False
        assert run.twitch._games_update_pending is False
        assert run.twitch.drop_history.total_count == 1
        assert not run.queue._pending_claims and not run.queue._handled_claims


@pytest.mark.asyncio
async def test_all_public_claims_finish_original_calendar_then_replay_policy(runtime, tmp_path, monkeypatch):
    async with runtime() as run:
        certificate = await run.reserve()
        first, protected = certificate.slots
        assert (await run.tick(3)).action == "watch"
        urgent = run.urgent.timed_drops["urgent"]
        prepare_claim(run, urgent, tmp_path, monkeypatch)
        run.move(60)
        assert await urgent.claim() is True
        assert (await run.next()).action == "stop"
        assert (await run.tick((first.until - NOW).total_seconds(), refresh=True)).action == "wait"
        assert (await run.tick((protected.connect_at - NOW).total_seconds(), refresh=True)).action == "watch"
        high = run.high.timed_drops["reward"]
        prepare_claim(run, high, tmp_path, monkeypatch)
        run.move((protected.connect_at - NOW).total_seconds() + 60)
        assert await high.claim() is True
        assert (await run.next()).action == "stop"
        assert run.executor._reservation.certificate is certificate
        assert (await run.tick((protected.until - NOW).total_seconds(), refresh=True)).action == "wait"
        entered, records = stop_at_policy_replay(run, monkeypatch)
        assert await run.tick((protected.until - NOW).total_seconds(), refresh=True) is None
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert records == [(State.GAMES_UPDATE, True, False)]
        assert run.twitch.drop_history.total_count == 2
        assert [call.args[0].id for call in run.twitch.watch.call_args_list] == [20, 10]
        assert run.twitch.settings.games_to_watch == ["High", "Urgent"]
        assert run.executor._reservation is None


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
@pytest.mark.asyncio
async def test_policy_hook_failure_cannot_lose_confirmed_claim_history(runtime, tmp_path, monkeypatch, error):
    async with runtime() as run:
        await run.reserve()
        assert (await run.tick(3)).action == "watch"
        drop = run.urgent.timed_drops["urgent"]
        prepare_claim(run, drop, tmp_path, monkeypatch)
        entered, records = stop_at_policy_replay(run, monkeypatch)

        def broken(_drop):
            raise error("synthetic claim policy failure")

        monkeypatch.setattr(run.twitch, "defer_deadline_claim", broken)
        with pytest.raises(error):
            await drop.claim()
        assert drop.is_claimed is True
        assert run.twitch.drop_history.total_count == 1
        assert drop.real_current_minutes == drop.required_minutes
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert records == [(State.GAMES_UPDATE, True, False)]
        assert run.executor._reservation is None
        assert await drop.claim() is True
        assert run.twitch.drop_history.total_count == 1
        run.twitch.gql_request.assert_awaited_once()
        drop._send_telegram_notification.assert_not_awaited()
        drop._send_discord_notification.assert_not_awaited()


@pytest.mark.parametrize("offset", [-0.1, 0, 0.1])
@pytest.mark.asyncio
async def test_last_claim_uses_original_inclusive_settlement_bound(runtime, tmp_path, monkeypatch, offset):
    async with runtime() as run:
        certificate, drop = await reach_protected_after_claim(run, tmp_path, monkeypatch)
        completion = next(item for item in certificate.completions if item.drop_id == certificate.slots[-1].drop_id)
        run.move((completion.claim_ready_at - NOW).total_seconds() + offset, refresh=True)
        entered, records = stop_at_policy_replay(run, monkeypatch)
        assert await drop.claim() is True
        step = await run.next()
        if offset <= 0:
            assert step.action == "stop"
            assert run.executor._reservation.certificate is certificate
            assert run.executor._reservation.bound.inputs.now == NOW
            assert not entered.is_set()
        else:
            assert step is None
            await asyncio.wait_for(entered.wait(), timeout=2)
            assert records == [(State.GAMES_UPDATE, True, False)]
            assert run.executor._reservation is None
        assert run.twitch.drop_history.total_count == 2


@pytest.mark.parametrize("lead", ["wall", "monotonic"])
@pytest.mark.asyncio
async def test_either_clock_closes_original_claim_budget(runtime, tmp_path, monkeypatch, lead):
    async with runtime() as run:
        certificate, drop = await reach_protected_after_claim(run, tmp_path, monkeypatch)
        completion = next(item for item in certificate.completions if item.drop_id == certificate.slots[-1].drop_id)
        run.move((completion.claim_ready_at - NOW).total_seconds() - 0.25, refresh=True)
        if lead == "wall":
            wall = run.executor._utc_clock() + timedelta(seconds=0.75)
            monkeypatch.setattr(run.executor, "_utc_clock", lambda: wall)
        else:
            tick = run.executor._clock() + 0.75
            monkeypatch.setattr(run.executor, "_clock", lambda: tick)
        entered, records = stop_at_policy_replay(run, monkeypatch)
        assert await drop.claim() is True
        assert await run.next() is None
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert records == [(State.GAMES_UPDATE, True, False)]
        assert run.executor._reservation is None


@pytest.mark.asyncio
async def test_renewal_replays_claim_policy_and_retires_followup_ownership(runtime, tmp_path, monkeypatch):
    async with runtime() as run:
        await run.reserve()
        assert (await run.tick(3)).action == "watch"
        drop = run.urgent.timed_drops["urgent"]
        prepare_claim(run, drop, tmp_path, monkeypatch)
        assert await drop.claim() is True
        assert (await run.next()).action == "stop"
        entered, records = stop_at_policy_replay(run, monkeypatch)
        auth = run.twitch._auth_state
        auth.browser_active = True
        auth.access_token, auth.device_id = "old-test-token", "test-device"
        run.twitch._client_type = ClientType.WEB
        run.twitch._http_client = MagicMock()
        run.twitch._gql_client = MagicMock()
        auth.accept_imported_identity(BrowserIdentity(
            42, "renewed-test-token", "test-device", ClientType.WEB.USER_AGENT,
        ))
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert records == [(State.GAMES_UPDATE, True, False)]
        assert run.twitch.owns_deadline_claim_followup(drop) is False
        assert not run.queue._pending_claims and not run.queue._handled_claims


@pytest.mark.asyncio
async def test_account_replacement_discards_old_deferred_claim_work(runtime, tmp_path, monkeypatch):
    async with runtime() as run:
        await run.reserve()
        assert (await run.tick(3)).action == "watch"
        drop = run.urgent.timed_drops["urgent"]
        prepare_claim(run, drop, tmp_path, monkeypatch)
        assert await drop.claim() is True
        assert (await run.next()).action == "stop"
        monkeypatch.setattr(run.twitch._inventory_service, "clear_cached_state", MagicMock())
        async with run.twitch.authentication_change():
            assert run.executor._reservation is None
            assert run.twitch.owns_deadline_claim_followup(drop) is False
            # Exit through the state API, not close(), so auth-finally must clear the flag.
            run.twitch.change_state(State.EXIT)
        assert run.twitch._deadline_claim_update_pending is False
        await asyncio.wait_for(run.root, timeout=2)
        assert not run.queue._pending_claims and not run.queue._handled_claims
