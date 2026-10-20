"""Shared test fixtures for the DanyAPI test suite."""

import asyncio as _asyncio
import logging
import os
from unittest.mock import patch

import pytest

# Importing danyapi runs configure(), which attaches the file handler configured
# in .env. Without this, test output (e.g. _log_rate_limit_hint probes) lands in
# the real application log and is indistinguishable from a production incident.
# Blank the path before the first danyapi import so no file handler is created.
os.environ["DANYAPI_LOG_FILE"] = ""


def _detach_file_handler() -> None:
    """Drop a file handler configure() may already have attached."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "name", None) == "danyapi-file":
            root.removeHandler(handler)
            handler.close()


@pytest.fixture(scope="session", autouse=True)
def _no_file_log():
    # Safety net for the case where danyapi was imported before the env var above
    # took effect (e.g. a plugin imported it during startup).
    _detach_file_handler()
    yield
    _detach_file_handler()


@pytest.fixture(autouse=True)
def instant_sleep():
    """Skip all asyncio.sleep > 0 (retry backoff, human delay); keep sleep(0) as a yield point."""
    real_sleep = _asyncio.sleep

    async def fast_sleep(delay):
        if delay and delay > 0:
            return  # skip the wait entirely
        await real_sleep(delay)

    with patch("asyncio.sleep", side_effect=fast_sleep):
        yield


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path_factory, monkeypatch):
    """Point the on-disk cache at a per-test directory.

    Without this, any test that builds a real store (usage tracker, session
    registry, or the app lifespan via TestClient) writes into the live instance
    cache under the temp dir, which is shared state between the test run and a
    running server.
    """
    from danyapi import store as store_mod

    root = tmp_path_factory.mktemp("cache")
    monkeypatch.setattr(store_mod.settings, "cache_dir", str(root))
    yield root
