"""Shared test configuration — tests are hermetic to ambient environment.

iter91 (2026-10-03): pydantic Settings reads `.env` from CWD, so a worktree's
LANE .env (real keys, possibly stale values) silently changed suite behavior
(the "which .env am I testing against" disease). Evidence that blocking is
suite-safe: iter91 audit ran the full suite with and without a .env present —
identical red set (10) in both conditions. Tests pin their own Settings
explicitly; anything needing real credentials must opt in deliberately.
"""

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_env_file(monkeypatch):
    from diallux.config import Settings, get_settings

    monkeypatch.setattr(
        Settings, "model_config", {**Settings.model_config, "env_file": None}
    )
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
