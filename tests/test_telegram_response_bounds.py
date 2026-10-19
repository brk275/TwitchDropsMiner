"""Telegram outcomes must not require unbounded response-body consumption."""

import asyncio
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp import web

from src.diagnostics import Diagnostics
from src.drop_history import DropHistory
from src.services.telegram_service import TelegramNotifier
from tests.test_watch_drop_filtering import _campaign, _drop


TOKEN = "synthetic-bounded-token"
CHAT = "synthetic-bounded-recipient"
MESSAGE = "synthetic-bounded-message"


@pytest.fixture
def transport(monkeypatch):
    response = MagicMock()
    response.status = 403
    response.text = AsyncMock(side_effect=AssertionError("Unbounded text read"))
    response.read = AsyncMock(side_effect=AssertionError("Unbounded body read"))
    stream = io.BytesIO()
    response.content.read = AsyncMock(side_effect=lambda size: stream.read(size))
    request = MagicMock()
    request.__aenter__ = AsyncMock(return_value=response)
    request.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.post.return_value = request
    monkeypatch.setattr("src.services.telegram_service.aiohttp.ClientSession", lambda: session)
    return SimpleNamespace(response=response, stream=stream, request=request, session=session)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 403, 429, 500])
@pytest.mark.parametrize("size", [0, 64, 262144, 262145, 524288])
async def test_diagnostics_bound_consumption_for_every_status(transport, tmp_path, status, size):
    diagnostics = Diagnostics(tmp_path)
    body = b"x" * size
    transport.stream.write(body)
    transport.stream.seek(0)
    transport.response.status = status

    assert await TelegramNotifier(TOKEN, CHAT, diagnostics=diagnostics)._send_message(MESSAGE) is (status == 200)

    reads = transport.response.content.read.await_args_list
    assert reads and all(0 < call.args[0] <= 65536 for call in reads)
    assert transport.stream.tell() == min(size, diagnostics.MAX_BODY_BYTES + 1)
    sample = diagnostics.apis["telegram"]["samples"][0]
    assert sample["status"] == status
    assert sample["body_bytes"] == min(size, diagnostics.MAX_BODY_BYTES + 1)
    assert sample["outcome"] == ("truncated" if size > diagnostics.MAX_BODY_BYTES else "response")
    assert sample["response"] is None
    transport.response.text.assert_not_awaited()
    transport.response.read.assert_not_awaited()
    transport.request.__aexit__.assert_awaited_once()
    transport.session.__aexit__.assert_awaited_once()
    transport.session.post.assert_called_once()
    assert transport.session.post.call_args.kwargs["timeout"].total == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 403, 429, 500])
async def test_without_diagnostics_no_body_is_requested(transport, status):
    transport.response.status = status
    assert await TelegramNotifier(TOKEN, CHAT)._send_message(MESSAGE) is (status == 200)
    transport.response.content.read.assert_not_awaited()
    transport.response.text.assert_not_awaited()
    transport.response.read.assert_not_awaited()
    transport.request.__aexit__.assert_awaited_once()
    transport.session.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 403])
@pytest.mark.parametrize("error", [aiohttp.ClientPayloadError, TimeoutError])
async def test_optional_capture_failure_preserves_http_outcome(transport, tmp_path, status, error):
    diagnostics = Diagnostics(tmp_path)
    transport.response.status = status
    transport.response.content.read.side_effect = error(MESSAGE)
    assert await TelegramNotifier(TOKEN, CHAT, diagnostics=diagnostics)._send_message(MESSAGE) is (status == 200)
    sample = diagnostics.apis["telegram"]["samples"][0]
    assert sample["status"] == status and sample["response"] is None
    assert all(value not in json.dumps(diagnostics.apis) for value in (TOKEN, CHAT, MESSAGE))
    transport.session.post.assert_called_once()
    transport.request.__aexit__.assert_awaited_once()
    transport.session.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 403])
async def test_cancellation_during_capture_propagates_and_closes(transport, tmp_path, caplog, status):
    diagnostics = Diagnostics(tmp_path)
    transport.response.status = status
    transport.response.content.read.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await TelegramNotifier(TOKEN, CHAT, diagnostics=diagnostics)._send_message(MESSAGE)
    assert not diagnostics.apis
    assert not [record for record in caplog.records if record.name == "TwitchDrops"]
    transport.request.__aexit__.assert_awaited_once()
    transport.session.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("diagnostic", [False, True])
async def test_rejected_open_ended_loopback_response_does_not_wait_for_eof(monkeypatch, tmp_path, diagnostic):
    diagnostics = Diagnostics(tmp_path) if diagnostic else None
    release = asyncio.Event()
    sent = asyncio.Event()
    calls = []

    async def respond(request):
        calls.append(await request.json())
        response = web.StreamResponse(status=429)
        await response.prepare(request)
        await response.write(b"x" * (Diagnostics.MAX_BODY_BYTES + 1))
        sent.set()
        await release.wait()
        return response

    app = web.Application()
    app.router.add_post("/bot{token}/sendMessage", respond)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    task = None
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        monkeypatch.setattr("src.services.telegram_service.TELEGRAM_API", f"http://127.0.0.1:{runner.addresses[0][1]}/bot")
        task = asyncio.create_task(TelegramNotifier(TOKEN, CHAT, diagnostics=diagnostics)._send_message(MESSAGE))
        await asyncio.wait_for(sent.wait(), timeout=3)
        assert await asyncio.wait_for(asyncio.shield(task), timeout=3) is False
        assert not release.is_set()
        assert calls == [{"chat_id": CHAT, "text": MESSAGE, "parse_mode": "HTML"}]
        if diagnostics is not None:
            sample = diagnostics.apis["other_http"]["samples"][0]
            assert sample["status"] == 429 and sample["outcome"] == "truncated"
            assert sample["body_bytes"] == Diagnostics.MAX_BODY_BYTES + 1
    finally:
        release.set()
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runner.cleanup()


@pytest.mark.asyncio
async def test_large_rejection_preserves_real_claim_history_and_discord(transport, tmp_path, monkeypatch):
    drop = _campaign("bounded-telegram", [_drop("reward", "Reward", 30)]).timed_drops["reward"]
    twitch = drop._twitch
    twitch.settings.telegram_bot_token, twitch.settings.telegram_chat_id = TOKEN, CHAT
    twitch.diagnostics = Diagnostics(tmp_path)
    twitch.drop_history = DropHistory(tmp_path)
    twitch.gui.broadcast_wanted_items_now = AsyncMock()
    twitch.gql_request = AsyncMock(return_value={"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}})
    transport.stream.write(b"x" * (Diagnostics.MAX_BODY_BYTES * 2))
    transport.stream.seek(0)
    drop.update_claim("synthetic-claim")
    discord = AsyncMock()
    monkeypatch.setattr(drop, "_send_discord_notification", discord)
    revision = drop.campaign.progress_revision

    assert await drop.claim() is True
    assert await drop.claim() is True
    assert drop.is_claimed and drop.current_minutes == 30
    assert drop.campaign.progress_revision == revision + 1
    assert DropHistory(tmp_path).total_count == 1
    twitch.gql_request.assert_awaited_once()
    transport.session.post.assert_called_once()
    discord.assert_awaited_once_with()
    assert transport.stream.tell() == Diagnostics.MAX_BODY_BYTES + 1
