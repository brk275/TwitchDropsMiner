"""Notification failures must not echo credentials, recipients or message bodies."""

import asyncio
import importlib
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp import web
from yarl import URL

from src.diagnostics import Diagnostics
from src.drop_history import DropHistory
from src.services.telegram_service import TelegramNotifier
from src.web.managers.settings import TELEGRAM_TOKEN_MASK
from tests.test_watch_drop_filtering import _campaign, _drop


TOKEN, CHAT, MESSAGE = "synthetic-bot-token-canary", "synthetic-chat-canary", "synthetic-message-canary"
PRIVATE = f"https://api.telegram.org/bot{TOKEN}/sendMessage {CHAT} {MESSAGE}"
web_app = importlib.import_module("src.web.app")


def assert_private(caplog, diagnostics=None):
    records = [record for record in caplog.records if record.name == "TwitchDrops"]
    assert records
    assert all(not record.exc_info and not record.stack_info for record in records)
    # Check structured args too, before any optional downstream formatter/redactor.
    retained = repr([(record.msg, record.args, record.getMessage()) for record in records])
    if diagnostics is not None:
        retained += json.dumps(diagnostics.apis)
    assert all(secret not in retained for secret in (TOKEN, CHAT, MESSAGE))


@pytest.fixture
def transport(monkeypatch):
    response = MagicMock()
    response.status = 403
    response.text = AsyncMock(return_value=json.dumps({"ok": False, "description": PRIVATE}))
    request = MagicMock()
    request.__aenter__ = AsyncMock(return_value=response)
    request.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.post.return_value = request
    factory = MagicMock(return_value=session)
    monkeypatch.setattr("src.services.telegram_service.aiohttp.ClientSession", factory)
    return SimpleNamespace(response=response, request=request, session=session, factory=factory)


@pytest.mark.asyncio
@pytest.mark.parametrize("diagnostic", [False, True])
@pytest.mark.parametrize("boundary", ["open", "post", "response", "body", "close"])
async def test_transport_exceptions_are_redacted_before_logging(transport, caplog, tmp_path, diagnostic, boundary):
    caplog.set_level(logging.WARNING, logger="TwitchDrops")
    diagnostics = Diagnostics(tmp_path) if diagnostic else None
    error = aiohttp.ClientResponseError(
        request_info=SimpleNamespace(real_url=URL(f"https://api.telegram.org/bot{TOKEN}/sendMessage")),
        history=(), status=403, message=f"{CHAT} {MESSAGE}",
    )
    target = {"open": transport.session.__aenter__, "post": transport.session.post,
              "response": transport.request.__aenter__, "body": transport.response.text,
              "close": transport.session.__aexit__}[boundary]
    target.side_effect = error
    assert await TelegramNotifier(TOKEN, CHAT, diagnostics=diagnostics)._send_message(MESSAGE) is False
    assert_private(caplog, diagnostics)
    if boundary != "open":
        transport.session.__aexit__.assert_awaited_once()
    if boundary == "open":
        transport.session.post.assert_not_called()
    else:
        transport.session.post.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("diagnostic", [False, True])
async def test_rejected_body_is_not_logged_even_when_it_echoes_private_fields(transport, caplog, tmp_path, diagnostic):
    caplog.set_level(logging.WARNING, logger="TwitchDrops")
    diagnostics = Diagnostics(tmp_path) if diagnostic else None
    assert await TelegramNotifier(TOKEN, CHAT, diagnostics=diagnostics)._send_message(MESSAGE) is False
    assert_private(caplog, diagnostics)
    transport.response.text.assert_awaited_once()
    transport.session.__aexit__.assert_awaited_once()
    if diagnostics is not None:
        entry = diagnostics.apis["telegram"]
        assert entry["count"] == 1 and entry["samples"][0]["status"] == 403
        assert entry["samples"][0]["response"]["ok"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["notify_drop_claimed", "notify_drop_progress", "test_connection"])
async def test_outer_notification_errors_are_redacted(monkeypatch, caplog, method):
    caplog.set_level(logging.WARNING, logger="TwitchDrops")
    notifier = TelegramNotifier(TOKEN, CHAT)
    send = AsyncMock(side_effect=RuntimeError(PRIVATE))
    monkeypatch.setattr(notifier, "_send_message", send)
    drop = SimpleNamespace(campaign=SimpleNamespace(name="Campaign", game=SimpleNamespace(name="Game")),
                           name="Reward", benefits=[])
    assert await getattr(notifier, method)(*(() if method == "test_connection" else (drop,))) is False
    send.assert_awaited_once()
    assert_private(caplog)


@pytest.mark.asyncio
async def test_claim_wrapper_redacts_errors_and_preserves_confirmation_and_other_notifications(monkeypatch, caplog, tmp_path):
    caplog.set_level(logging.WARNING, logger="TwitchDrops")
    drop = _campaign("telegram-private", [_drop("reward", "Reward", 30)]).timed_drops["reward"]
    twitch = drop._twitch
    twitch.settings.telegram_bot_token, twitch.settings.telegram_chat_id = TOKEN, CHAT
    twitch.drop_history = DropHistory(tmp_path)
    twitch.gui.broadcast_wanted_items_now = AsyncMock()
    twitch.gql_request = AsyncMock(return_value={"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}})
    drop.update_claim("test-claim")
    observation = drop.progress_observation
    telegram = AsyncMock(side_effect=RuntimeError(PRIVATE))
    discord = AsyncMock()
    monkeypatch.setattr(TelegramNotifier, "notify_drop_claimed", telegram)
    monkeypatch.setattr(drop, "_send_discord_notification", discord)
    assert await drop.claim() is True
    assert await drop.claim() is True
    assert drop.is_claimed is True and drop.current_minutes == 30
    assert drop.progress_observation == observation
    assert DropHistory(tmp_path).total_count == 1
    twitch.gql_request.assert_awaited_once()
    telegram.assert_awaited_once_with(drop)
    discord.assert_awaited_once_with()
    assert_private(caplog)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["notify_drop_claimed", "notify_drop_progress", "test_connection"])
async def test_cancellation_still_propagates_without_an_error_log(monkeypatch, caplog, method):
    notifier = TelegramNotifier(TOKEN, CHAT)
    monkeypatch.setattr(notifier, "_send_message", AsyncMock(side_effect=asyncio.CancelledError(PRIVATE)))
    drop = SimpleNamespace(campaign=SimpleNamespace(name="Campaign", game=SimpleNamespace(name="Game")),
                           name="Reward", benefits=[])
    with pytest.raises(asyncio.CancelledError):
        await getattr(notifier, method)(*(() if method == "test_connection" else (drop,)))
    assert not [record for record in caplog.records if record.name == "TwitchDrops"]


@pytest.mark.asyncio
async def test_timeout_remains_a_fixed_redacted_outcome(transport, caplog, tmp_path):
    diagnostics = Diagnostics(tmp_path)
    transport.request.__aenter__.side_effect = TimeoutError(PRIVATE)
    assert await TelegramNotifier(TOKEN, CHAT, diagnostics=diagnostics)._send_message(MESSAGE) is False
    assert_private(caplog, diagnostics)
    assert diagnostics.apis["telegram"]["samples"][0]["outcome"] == "timeout"
    transport.session.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 403, 429, 500])
async def test_real_loopback_response_never_leaks_echoed_message_or_token(monkeypatch, caplog, status):
    caplog.set_level(logging.DEBUG, logger="TwitchDrops")
    calls = []

    async def respond(request):
        calls.append((request.method, request.path, await request.json()))
        return web.Response(status=status, text=PRIVATE)

    app = web.Application()
    app.router.add_post("/bot{token}/sendMessage", respond)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        monkeypatch.setattr("src.services.telegram_service.TELEGRAM_API", f"http://127.0.0.1:{port}/bot")
        assert await TelegramNotifier(TOKEN, CHAT)._send_message(MESSAGE) is (status == 200)
        assert calls == [("POST", f"/bot{TOKEN}/sendMessage", {"chat_id": CHAT, "text": MESSAGE, "parse_mode": "HTML"})]
        assert_private(caplog)
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["", TELEGRAM_TOKEN_MASK, TOKEN])
@pytest.mark.parametrize("boundary", ["construct", "test"])
async def test_connection_api_never_reflects_unexpected_errors(monkeypatch, token, boundary):
    stored = SimpleNamespace(telegram_bot_token=TOKEN, telegram_chat_id=CHAT, save=MagicMock())
    monkeypatch.setattr(web_app, "gui_manager", SimpleNamespace(settings=SimpleNamespace(_settings=stored)))
    monkeypatch.setattr(web_app, "twitch_client", None)
    if boundary == "construct":
        send = MagicMock(side_effect=RuntimeError(PRIVATE))
        monkeypatch.setattr("src.services.telegram_service.TelegramNotifier", send)
    else:
        send = AsyncMock(side_effect=RuntimeError(PRIVATE))
        monkeypatch.setattr(TelegramNotifier, "test_connection", send)
    response = await web_app.test_telegram(web_app.TelegramTestRequest(telegram_bot_token=token, telegram_chat_id=""))
    assert response["success"] is False and response["message"]
    assert all(secret not in json.dumps(response) for secret in (TOKEN, CHAT, MESSAGE))
    assert stored.telegram_bot_token == TOKEN and stored.telegram_chat_id == CHAT
    stored.save.assert_not_called()
    if boundary == "construct":
        send.assert_called_once_with(TOKEN, CHAT, diagnostics=None)
    else:
        send.assert_awaited_once_with()
