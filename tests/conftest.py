"""Shared pytest fixtures (both unit and integration)."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_workspace_client() -> MagicMock:
    """A minimal stand-in for `databricks.sdk.WorkspaceClient` with a working
    `.config.authenticate()` returning an Authorization header.
    """
    ws = MagicMock()
    ws.config.authenticate.return_value = {"Authorization": "Bearer mocked-sdk-token"}
    return ws


@pytest.fixture
def clean_env(monkeypatch):
    """Strip env vars that any client auto-loads (LAKEBASE_*, DATABRICKS_*).

    Use this in unit tests that build a client with explicit kwargs so the
    local shell's env doesn't leak into behaviour.
    """
    for var in list(os.environ):
        if var.startswith(("LAKEBASE_", "DATABRICKS_")):
            monkeypatch.delenv(var, raising=False)
    yield
