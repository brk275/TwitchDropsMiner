"""Explicitly loaded, synthetic-only startup diagnostics, never a production hook."""

import asyncio
import json
import os
from types import SimpleNamespace

import pytest
import pytest_asyncio


ALLOWED_TESTS = frozenset("tests/test_server_renewal.py::" + name for name in (
    "test_owned_browser_pipe_reaches_fake_chromium_on_fds_3_and_4",
    "test_real_owned_chromium_opens_no_tcp_listener",
    "test_real_chromium_closed_target_fails_page_commands_fast",
))
STDERR_LIMIT = 16384


async def drain_bounded(reader, limit=STDERR_LIMIT):
    """Drain the pipe fully even after the retained diagnostic prefix is full."""
    retained = bytearray()
    while chunk := await reader.read(4096):
        retained.extend(chunk[:max(0, limit - len(retained))])
    return retained.decode("utf-8", errors="replace")


def pytest_addoption(parser):
    parser.addoption("--diagnostic-display", choices=("auto", "headless"), default="auto")


@pytest_asyncio.fixture(autouse=True)
async def synthetic_browser_diagnostics(request, monkeypatch):
    if (os.environ.get("TDM_SYNTHETIC_BROWSER_DIAGNOSTICS") != "1"
            or request.node.nodeid not in ALLOWED_TESTS):
        pytest.fail("Startup diagnostics require an explicitly allowed synthetic test")

    from src.auth import server_renewal

    spawn = asyncio.create_subprocess_exec
    processes = []
    readers = []
    if request.config.getoption("--diagnostic-display") == "headless":
        # Change only the owning module's binding, not global shutil behavior.
        monkeypatch.setattr(server_renewal, "shutil", SimpleNamespace(which=lambda _: None))

    async def record(*args, **kwargs):
        kwargs["stderr"] = asyncio.subprocess.PIPE
        process = await spawn(*args, **kwargs)
        processes.append((process, list(kwargs.get("pass_fds", ()))))
        readers.append(asyncio.create_task(drain_bounded(process.stderr)))
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record)
    try:
        yield
    finally:
        try:
            errors = await asyncio.wait_for(asyncio.gather(*readers), timeout=5)
        except TimeoutError:
            errors = ["diagnostic_stderr_drain_timeout"] * len(processes)
        for (process, descriptors), stderr in zip(processes, errors, strict=True):
            print("SYNTHETIC_BROWSER_DIAGNOSTIC " + json.dumps({
                "test": request.node.name,
                "display": request.config.getoption("--diagnostic-display"),
                "exit_status": process.returncode,
                "inherited_descriptors": descriptors,
                "stderr_prefix": stderr,
            }))
