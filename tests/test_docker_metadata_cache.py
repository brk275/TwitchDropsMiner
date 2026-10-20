"""Changing image labels must not invalidate dependency or source layers."""

from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("argument", ["BUILD_DATE", "VCS_REF", "VERSION"])
def test_metadata_arguments_follow_filesystem_layers(argument):
    lines = [line.lstrip() for line in (ROOT / "Dockerfile").read_text().splitlines()]
    declaration = lines.index(f"ARG {argument}")
    filesystem_steps = [
        index for index, line in enumerate(lines)
        if line.upper().startswith(("FROM ", "WORKDIR ", "ENV ", "RUN ", "COPY ", "ADD "))
    ]
    assert declaration > max(filesystem_steps), (
        f"Metadata-only {argument} must not enter dependency or source layer cache keys"
    )


def _build_options(filename, job):
    workflow = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())
    options = [
        step["with"] for step in workflow["jobs"][job]["steps"]
        if step.get("uses", "").startswith("docker/build-push-action@")
    ]
    assert len(options) == 1
    return options[0]


def test_published_images_explicitly_refresh_bases_and_packages():
    options = _build_options("docker-release.yml", "docker-build")
    assert options["push"] is True
    assert options.get("pull") is True
    assert options.get("no-cache") is True


def test_validation_builds_retain_cache_without_publishing():
    options = _build_options("validation.yml", "docker")
    assert options["push"] is False
    assert options.get("no-cache", False) is False
    assert options["cache-from"] == "type=gha"
    assert options["cache-to"] == "type=gha,mode=max"
