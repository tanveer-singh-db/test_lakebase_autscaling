"""Session-scoped fixtures for integration tests.

Tests are skipped entirely unless `LAKEBASE_API_URL` is set (directly or via
`tests/test_config.yaml`). Databricks auth is resolved by the SDK itself —
honouring, in order:

    DATABRICKS_CONFIG_PROFILE env var > `auth.profile` in test_config.yaml
        > DATABRICKS_* env vars > the DEFAULT profile in ~/.databrickscfg.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio
import yaml

from lakebase_utils.lakebase_api_async import AsyncLakebaseDataApiClient


@pytest.fixture(scope="session")
def integration_config() -> dict:
    cfg_path = Path(__file__).parent.parent / "test_config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
    if not isinstance(cfg, dict):
        cfg = {}

    lb = cfg.setdefault("lakebase", {})
    lb["api_url"] = os.environ.get("LAKEBASE_API_URL") or lb.get("api_url") or ""
    lb.setdefault("probe_schema", "public")
    lb.setdefault("probe_table", "databricks_list_roles")

    auth = cfg.setdefault("auth", {})
    # Env wins over yaml; empty string means "let the SDK decide".
    auth["profile"] = os.environ.get("DATABRICKS_CONFIG_PROFILE") or auth.get("profile") or ""

    if not lb["api_url"]:
        pytest.skip("LAKEBASE_API_URL not set — skipping integration tests")
    return cfg


@pytest_asyncio.fixture(scope="function")
async def async_client(integration_config) -> AsyncLakebaseDataApiClient:
    kwargs = {"base_url": integration_config["lakebase"]["api_url"]}
    profile = integration_config["auth"]["profile"]
    if profile:
        # Pin the CLI profile so the SDK mints tokens as that identity.
        # auth_mode stays None (auto) — the client uses SDK-backed auth
        # because no static token is present.
        kwargs["profile"] = profile

    client = AsyncLakebaseDataApiClient(**kwargs)
    try:
        yield client
    finally:
        await client.close()
