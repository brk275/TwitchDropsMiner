"""Mixed test runners must not orphan a pytest-created event loop."""

import os
import subprocess
import sys
from textwrap import dedent

import pytest


BOUNDARIES = {
    "asyncio_run": """
def test_boundary():
    asyncio.run(asyncio.sleep(0))
""",
    "asyncio_runner": """
def test_boundary():
    with asyncio.Runner() as runner:
        runner.run(asyncio.sleep(0))
        runner.run(asyncio.sleep(0))
""",
    "asyncio_runner_error": """
def test_boundary():
    async def fail():
        await asyncio.sleep(0)
        raise ValueError("expected runner failure")

    with pytest.raises(ValueError, match="expected runner failure"):
        with asyncio.Runner() as runner:
            runner.run(fail())
""",
    "unittest": """
class TestBoundary(unittest.IsolatedAsyncioTestCase):
    async def test_boundary(self):
        await asyncio.sleep(0)
""",
    "pytest_only": """
def test_boundary():
    pass
""",
    "intentional_leak": """
def test_boundary():
    asyncio.new_event_loop()
""",
}


@pytest.mark.parametrize("boundary", BOUNDARIES)
def test_mixed_runner_loop_ownership(tmp_path, boundary):
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "conftest.py").write_text(dedent("""
        import asyncio

        created_loops = []

        class TrackingPolicy(asyncio.DefaultEventLoopPolicy):
            def __init__(self):
                super().__init__()
                self.current = None

            def new_event_loop(self):
                loop = super().new_event_loop()
                created_loops.append(loop)
                return loop

            def set_event_loop(self, loop):
                super().set_event_loop(loop)
                self.current = loop

        policy = TrackingPolicy()
        previous_policy = None

        def pytest_sessionstart(session):
            global previous_policy
            previous_policy = asyncio.get_event_loop_policy()
            asyncio.set_event_loop_policy(policy)

        def pytest_sessionfinish(session, exitstatus):
            # Cleanup follows the ownership assertion, including its failure path.
            for loop in created_loops:
                if not loop.is_closed():
                    loop.close()
            asyncio.set_event_loop_policy(previous_policy)
    """), encoding="utf-8")
    before = dedent("""
        import asyncio
        import unittest

        import pytest

        from conftest import created_loops, policy

        @pytest.mark.asyncio
        async def test_before_boundary():
            await asyncio.sleep(0)
    """)
    after = dedent("""
        def test_no_orphaned_loops():
            assert created_loops
            orphaned = [loop for loop in created_loops
                        if not loop.is_closed() and loop is not policy.current]
            assert not orphaned, "event loop lost without being closed"
    """)
    (tmp_path / "test_boundary.py").write_text(before + BOUNDARIES[boundary] + after, encoding="utf-8")
    environment = dict(os.environ)
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        environment.pop(name, None)
    environment.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONASYNCIODEBUG="1",
                       PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([
        sys.executable, "-m", "pytest", "-p", "pytest_asyncio.plugin", "-p", "no:cacheprovider",
        "-c", "pytest.ini", "test_boundary.py", "-q", "--color=no", "--tb=short",
        "-W", "error::ResourceWarning", "-W", "error::pytest.PytestUnraisableExceptionWarning",
    ], cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30)
    output = result.stdout + result.stderr
    expected = 1 if boundary == "intentional_leak" else 0
    assert result.returncode == expected, output
    assert ("1 failed, 2 passed" if expected else "3 passed") in result.stdout, output
    if expected:
        assert "event loop lost without being closed" in result.stdout, output
    assert "ResourceWarning:" not in output, output
    assert "PytestUnraisableExceptionWarning:" not in output, output
