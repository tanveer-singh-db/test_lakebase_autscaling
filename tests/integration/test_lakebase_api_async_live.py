"""Live smoke tests for AsyncLakebaseDataApiClient.

Requires:
    LAKEBASE_API_URL          (base URL from the Data API UI)
    Databricks SDK auth on the current shell (profile, env vars, or notebook ambient)
    The authenticated identity must be a provisioned Postgres role for the project
    with SELECT on `probe_schema.probe_table`.

Skipped automatically when LAKEBASE_API_URL isn't set. Run with:

    pytest tests/integration/ -v -m integration
"""

from __future__ import annotations

import asyncio

import pytest


pytestmark = pytest.mark.integration


class TestAsyncClientLive:
    async def test_get_returns_rows(self, async_client, integration_config):
        lb = integration_config["lakebase"]
        rows = await async_client.get(lb["probe_schema"], lb["probe_table"], params={"limit": 5})
        assert isinstance(rows, list)
        assert len(rows) <= 5

    async def test_paginate_yields_rows(self, async_client, integration_config):
        lb = integration_config["lakebase"]
        n = 0
        async for _ in async_client.paginate(
            lb["probe_schema"], lb["probe_table"], page_size=2, max_rows=6
        ):
            n += 1
        assert n <= 6

    async def test_fetch_all_returns_list(self, async_client, integration_config):
        lb = integration_config["lakebase"]
        rows = await async_client.fetch_all(
            lb["probe_schema"], lb["probe_table"], page_size=5, max_rows=10
        )
        assert isinstance(rows, list)
        assert len(rows) <= 10

    async def test_concurrent_fan_out(self, async_client, integration_config):
        lb = integration_config["lakebase"]
        results = await asyncio.gather(
            *[async_client.get(lb["probe_schema"], lb["probe_table"], params={"limit": 1})
              for _ in range(10)]
        )
        assert len(results) == 10
        for r in results:
            assert isinstance(r, list)
