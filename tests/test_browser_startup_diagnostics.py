"""Diagnostic capture drains fully without retaining unbounded output."""

import asyncio

import pytest

from tests.browser_startup_diagnostics import ALLOWED_TESTS, drain_bounded


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,limit,expected", [
    (b"", 4, ""), (b"abcdefgh", 4, "abcd"),
    (b"x" * 20000, 5, "xxxxx"), (b"\xff", 4, "\ufffd"),
], ids=["empty", "bounded", "fully-drained", "invalid-utf8"])
async def test_capture_is_bounded_and_consumes_entire_stream(payload, limit, expected):
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    assert await drain_bounded(reader, limit) == expected
    assert reader.at_eof()


def test_only_three_no_auth_browser_cases_are_allowed():
    assert len(ALLOWED_TESTS) == 3
    assert all(name.startswith("tests/test_server_renewal.py::test_") for name in ALLOWED_TESTS)
