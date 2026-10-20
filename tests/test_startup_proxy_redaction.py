"""Startup diagnostics must not expose proxy credentials or private endpoints."""

import asyncio
import logging
import runpy
import signal
import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import truststore

from src.config import settings as settings_module
from src.core import client as client_module
from src.web import app as webapp
from src.web import gui_manager


@pytest.mark.parametrize("proxy", [
    "",
    "http://proxy.example.test:8080",
    "http://private-user:private-password@proxy.example.test:8080",
    "socks5://private%40user:private%3Apassword@proxy.example.test:1080",
    "http://proxy.example.test:8080/private-path?token=private-query",
    "private-proxy-marker\nInjected diagnostic line",
])
def test_real_startup_logs_only_proxy_presence(tmp_path, monkeypatch, caplog, capsys, proxy):
    settings = SimpleNamespace(language="", proxy=proxy,
                               minimum_refresh_interval_minutes=30, save=MagicMock())
    started, stopped = asyncio.Event(), asyncio.Event()

    async def serve(**kwargs):
        started.set()
        await stopped.wait()

    async def run():
        await asyncio.wait_for(started.wait(), 1)

    client = SimpleNamespace(run=AsyncMock(side_effect=run), shutdown=AsyncMock(),
                             close=MagicMock(), print=MagicMock())
    constructor = MagicMock(return_value=client)
    server = AsyncMock(side_effect=serve)
    stop = AsyncMock(side_effect=stopped.set)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings_module, "Settings", MagicMock(return_value=settings))
    monkeypatch.setattr(client_module, "Twitch", constructor)
    monkeypatch.setattr(truststore, "inject_into_ssl", MagicMock())
    monkeypatch.setattr(gui_manager, "WebGUIManager", MagicMock(return_value=object()))
    monkeypatch.setattr(webapp, "set_managers", MagicMock())
    monkeypatch.setattr(webapp, "run_server", server)
    monkeypatch.setattr(webapp, "shutdown_server", stop)
    caplog.set_level(logging.DEBUG, logger="TwitchDrops")
    logger = logging.getLogger("TwitchDrops")
    handlers, level = logger.handlers[:], logger.level
    signals = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}

    try:
        with warnings.catch_warnings(), pytest.raises(SystemExit) as exit_info:
            runpy.run_module("src", run_name="__main__")
        assert exit_info.value.code == 0
    finally:
        for handler in logger.handlers:
            if handler not in handlers:
                handler.close()
        logger.handlers[:] = handlers
        logger.setLevel(level)
        for signum, handler in signals.items():
            signal.signal(signum, handler)

    constructor.assert_called_once_with(settings)
    assert settings.proxy == proxy
    settings.save.assert_called_once_with()
    client.run.assert_awaited_once_with()
    client.shutdown.assert_awaited_once_with()
    server.assert_awaited_once_with(host="0.0.0.0", port=8080)
    stop.assert_awaited_once_with()
    truststore.inject_into_ssl.assert_called_once_with()

    expected = "Proxy: configured" if proxy else "Proxy: disabled"
    assert [record.getMessage() for record in caplog.records
            if record.getMessage().startswith("Proxy:")] == [expected]
    console = capsys.readouterr()
    logfile = (tmp_path / "logs" / "TDM.log").read_text(encoding="utf-8")
    for output in (caplog.text, console.out, console.err, logfile):
        if output:  # stderr is normally empty.
            assert expected in output
        if proxy:
            assert proxy not in output
            assert "private-" not in output and "private%" not in output
            assert "proxy.example.test" not in output
            assert "Injected diagnostic line" not in output
