"""Update checks use semantic precedence without contacting GitHub or Twitch."""

import asyncio
import importlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import httpx
import pytest
import tomllib
from packaging.requirements import Requirement

from src.web.auth import WebAuth


web = importlib.import_module("src.web.app")
versions = importlib.import_module("src.version")
RELEASES = "https://github.com/rangermix/TwitchDropsMiner/releases"


def test_runtime_dependency_excludes_lenient_parser_versions():
    project = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    requirements = [Requirement(value) for value in project["project"]["dependencies"]]
    parser = next(requirement for requirement in requirements if requirement.name == "semver")
    assert "3.0.4" not in parser.specifier
    assert "3.1.0" in parser.specifier


@pytest.fixture
def release_api(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "web_auth", WebAuth(tmp_path / "auth.json", public_base_url="http://testserver"))
    monkeypatch.setattr(web, "twitch_client", None)
    response = MagicMock(status=200)
    response.json = AsyncMock()
    response.__aenter__.return_value = response
    response.__aexit__.return_value = False
    session = MagicMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = False
    session.get.return_value = response
    monkeypatch.setattr(aiohttp, "ClientSession", MagicMock(return_value=session))
    return response, session


async def check_version(release_api, monkeypatch, installed, latest):
    response, _ = release_api
    monkeypatch.setattr(versions, "__version__", installed)
    response.json.return_value = {"tag_name": latest, "html_url": RELEASES + "/tag/" + str(latest)}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url="http://testserver") as browser:
        result = await browser.get("/api/version")
    assert result.status_code == 200
    return result.json()


@pytest.mark.asyncio
@pytest.mark.parametrize(("installed", "latest", "update"), [
    ("2.9.0", "v2.10.0", True),
    ("2.10.0", "v2.9.0", False),
    ("2.3.9", "v2.3.10", True),
    ("2.3.10", "v2.3.9", False),
    ("9.0.0", "v10.0.0", True),
    ("10.0.0", "v9.0.0", False),
    ("2.3.0", "v2.3.0", False),
    ("2.3.0-rc.2", "v2.3.0-rc.10", True),
    ("2.3.0-rc.10", "v2.3.0-rc.2", False),
    ("2.3.0-rc.1", "v2.3.0", True),
    ("2.3.0", "v2.3.0-rc.1", False),
    ("2.3.0-alpha.9", "v2.3.0-beta.1", True),
    ("2.3.0+local.9", "v2.3.0+release.10", False),
    ("2.3.0+local.9", "v2.3.0", False),
    ("2.3.0", "v2.3.0+build.1", False),
    ("2.3.0", "2.4.0", True),
])
async def test_update_endpoint_compares_semantic_precedence(release_api, monkeypatch, installed, latest, update):
    result = await check_version(release_api, monkeypatch, installed, latest)
    assert result == {
        "current_version": installed, "latest_version": latest.removeprefix("v"),
        "update_available": update, "download_url": RELEASES + "/tag/" + latest,
    }
    _, session = release_api
    session.get.assert_called_once_with("https://api.github.com/repos/rangermix/TwitchDropsMiner/releases/latest", timeout=5)
    session.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", [
    None, False, 123, [], {}, "", "v", "vv2.3.0", "2.3", "02.3.0", "2.3.0\nprivate-version",
    "v2.4.0\n", "v2.4.0-rc.1\n", "v2\u0661.4.0", "v2.4.0-1\u0661",
])
async def test_invalid_release_tag_is_unknown_not_an_update(release_api, monkeypatch, caplog, tag):
    result = await check_version(release_api, monkeypatch, "2.3.0", tag)
    assert result == {
        "current_version": "2.3.0", "latest_version": None,
        "update_available": False, "download_url": RELEASES,
    }
    assert "private-version" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("installed", ["custom-local-build", "2.3.0\n", "2\u0661.3.0"])
async def test_non_semver_source_build_does_not_offer_an_unproven_upgrade(release_api, monkeypatch, installed):
    result = await check_version(release_api, monkeypatch, installed, "v22.0.0")
    assert result["current_version"] == installed
    assert result["latest_version"] is None and result["update_available"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http", "json", "timeout", "cancel"])
async def test_failed_update_lookup_preserves_safe_response_and_cancellation(release_api, monkeypatch, failure):
    response, session = release_api
    monkeypatch.setattr(versions, "__version__", "2.3.0")
    if failure == "http":
        response.status = 503
    else:
        response.json.side_effect = {
            "json": ValueError("Invalid JSON"), "timeout": TimeoutError(), "cancel": asyncio.CancelledError(),
        }[failure]
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await web.get_version()
    else:
        result = await web.get_version()
        assert result == {"current_version": "2.3.0", "latest_version": None,
                          "update_available": False, "download_url": RELEASES}
    session.__aexit__.assert_awaited_once()
