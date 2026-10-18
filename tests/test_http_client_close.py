"""Cookie persistence failures must not leave the HTTP connector open."""

import errno
import pickle
from types import SimpleNamespace
from unittest.mock import MagicMock

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from src.api.http_client import HTTPClient
from src.config import ClientType, State


@pytest_asyncio.fixture
async def transport(tmp_path, monkeypatch):
    monkeypatch.setattr("src.api.http_client.COOKIES_PATH", tmp_path / "cookies.jar")
    client = HTTPClient(
        SimpleNamespace(connection_quality=1, proxy=""),
        MagicMock(),
        SimpleNamespace(_state=State.IDLE),
        ClientType.ANDROID_APP,
    )
    session = await client.get_session()
    try:
        yield client, session
    finally:
        # A failed regression still closes the actual session without hiding the assertion.
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    PermissionError("test cookie write denied"),
    OSError(errno.ENOSPC, "test filesystem full"),
    pickle.PicklingError("test cookie serialization failure"),
])
async def test_save_failure_closes_session_and_preserves_error(transport, monkeypatch, failure):
    client, session = transport
    connector = session.connector
    save = MagicMock(side_effect=failure)
    monkeypatch.setattr(session.cookie_jar, "save", save)

    with pytest.raises(type(failure)) as raised:
        await client.close()

    assert raised.value is failure
    assert session.closed and connector.closed
    assert client._session is None
    save.assert_called_once()
    await client.close()
    save.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("destination", ["directory", "missing_parent"])
async def test_real_save_failure_closes_used_connection(transport, tmp_path, monkeypatch, destination):
    client, session = transport
    connector = session.connector
    target = tmp_path if destination == "directory" else tmp_path / "absent" / "cookies.jar"
    monkeypatch.setattr("src.api.http_client.COOKIES_PATH", target)

    async def respond(request):
        return web.Response(text="owned test response")

    application = web.Application()
    application.router.add_get("/", respond)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    try:
        await site.start()
        host, port = runner.addresses[0]
        async with client.request("GET", f"http://{host}:{port}/") as response:
            assert await response.text() == "owned test response"
        assert not session.closed and not connector.closed

        with pytest.raises(OSError):
            await client.close()

        assert session.closed and connector.closed
        assert client._session is None
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_successful_close_keeps_legacy_cookie_save(transport, tmp_path):
    client, session = transport
    session.cookie_jar.update_cookies({"test-cookie": "test-value"}, ClientType.ANDROID_APP.CLIENT_URL)
    connector = session.connector

    await client.close()

    saved = aiohttp.CookieJar()
    saved.load(tmp_path / "cookies.jar")
    assert saved.filter_cookies(ClientType.ANDROID_APP.CLIENT_URL)["test-cookie"].value == "test-value"
    assert session.closed and connector.closed
    assert client._session is None
    await client.close()


@pytest.mark.asyncio
async def test_browser_close_preserves_legacy_cookie_file(transport, tmp_path, monkeypatch):
    client, session = transport
    target = tmp_path / "cookies.jar"
    target.write_bytes(b"unchanged legacy test data")
    client.enable_browser_mode(ClientType.WEB)
    save = MagicMock(side_effect=AssertionError("Browser mode must not save legacy cookies"))
    monkeypatch.setattr(session.cookie_jar, "save", save)

    await client.close()

    assert target.read_bytes() == b"unchanged legacy test data"
    save.assert_not_called()
    assert session.closed and client._session is None


@pytest.mark.asyncio
async def test_close_without_session_does_not_create_one(transport):
    client, session = transport
    await session.close()
    client._session = None
    client.get_session = MagicMock(side_effect=AssertionError("Do not create a session during cleanup"))

    await client.close()

    client.get_session.assert_not_called()
    assert client._session is None
