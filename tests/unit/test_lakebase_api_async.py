"""Unit tests for AsyncLakebaseDataApiClient."""

from __future__ import annotations

import asyncio
import re
import time
from unittest.mock import patch

import pytest
from aioresponses import aioresponses

from lakebase_utils.lakebase_api_async import (
    AsyncLakebaseDataApiClient,
    LakebaseDataApiError,
)


pytestmark = pytest.mark.unit

BASE = "https://x.example.com"


def _url_re(path: str) -> re.Pattern:
    """aioresponses matches exact URLs — use a regex so querystrings are ignored."""
    return re.compile(rf"^{re.escape(BASE)}{re.escape(path)}(\?.*)?$")


def _client(**overrides):
    kwargs = dict(
        base_url=BASE,
        auth_mode="oauth_token",
        token="tok",
        max_attempts=3,
        base_backoff=0.01,
        max_backoff=0.05,
    )
    kwargs.update(overrides)
    return AsyncLakebaseDataApiClient(**kwargs)


class TestConstructor:
    def test_url_from_parts(self, clean_env):
        c = AsyncLakebaseDataApiClient(
            host="ep-x.example.com", workspace_id="9",
            database="db", auth_mode="oauth_token", token="t",
        )
        assert c.base_url == "https://ep-x.example.com/api/2.0/workspace/9/rest/db"

    def test_missing_url_raises(self, clean_env):
        with pytest.raises(ValueError, match="base URL is required"):
            AsyncLakebaseDataApiClient(auth_mode="oauth_token", token="t")

    def test_missing_token(self, clean_env):
        with pytest.raises(ValueError, match="auth_mode='oauth_token' requires"):
            AsyncLakebaseDataApiClient(base_url=BASE, auth_mode="oauth_token")

    def test_sp_oauth_missing_fields(self, clean_env):
        with pytest.raises(ValueError, match="client_id, client_secret"):
            AsyncLakebaseDataApiClient(base_url=BASE, auth_mode="sp_oauth")

    def test_bogus_mode(self, clean_env):
        with pytest.raises(ValueError, match="unknown auth_mode"):
            AsyncLakebaseDataApiClient(base_url=BASE, auth_mode="wat")


class TestAuthHeader:
    async def test_static_token_bypasses_sdk(self, clean_env):
        c = _client()
        header = await c._auth_header()
        assert header == {"Authorization": "Bearer tok", "Accept": "application/json"}
        await c.close()

    async def test_sdk_token_is_cached_within_ttl(self, clean_env, mock_workspace_client):
        with patch("lakebase_utils._common._make_ws", return_value=mock_workspace_client):
            c = AsyncLakebaseDataApiClient(base_url=BASE, auth_mode="user_oauth")
        await c._auth_header()
        await c._auth_header()
        await c._auth_header()
        # First call mints; subsequent calls within TTL reuse the cached header.
        assert mock_workspace_client.config.authenticate.call_count == 1
        await c.close()

    async def test_sdk_refresh_after_ttl(self, clean_env, mock_workspace_client):
        with patch("lakebase_utils._common._make_ws", return_value=mock_workspace_client):
            c = AsyncLakebaseDataApiClient(base_url=BASE, auth_mode="user_oauth")
        await c._auth_header()          # cache populated
        # Force expiry and verify SDK is called again.
        c._auth_expires_at = time.monotonic() - 1
        await c._auth_header()
        assert mock_workspace_client.config.authenticate.call_count == 2
        await c.close()


class TestGet:
    async def test_returns_json_rows(self, clean_env):
        c = _client()
        with aioresponses() as m:
            m.get(_url_re("/public/widgets"), payload=[{"id": 1}, {"id": 2}])
            rows = await c.get("public", "widgets", params={"limit": 5})
        assert rows == [{"id": 1}, {"id": 2}]
        await c.close()

    async def test_raises_with_pgrst_code(self, clean_env):
        c = _client()
        with aioresponses() as m:
            m.get(_url_re("/public/widgets"),
                  status=403,
                  body='{"code":"42501","message":"perm denied","hint":null}')
            with pytest.raises(LakebaseDataApiError) as exc:
                await c.get("public", "widgets")
        assert exc.value.status == 403
        assert exc.value.code == "42501"
        assert exc.value.message == "perm denied"
        await c.close()

    async def test_get_uses_schema_and_table_in_url(self, clean_env):
        c = _client()
        with aioresponses() as m:
            m.get(_url_re("/manual_tests/synced_cdf_source_table"), payload=[])
            await c.get("manual_tests", "synced_cdf_source_table",
                        params={"select": "customer_id,name"})
        await c.close()


class TestPaginate:
    async def test_iterates_pages_until_short_page(self, clean_env):
        c = _client()
        with aioresponses() as m:
            m.get(_url_re("/public/t"), payload=[{"id": 1}, {"id": 2}])
            m.get(_url_re("/public/t"), payload=[{"id": 3}])  # short → stop
            rows = [r async for r in c.paginate("public", "t", page_size=2)]
        assert [r["id"] for r in rows] == [1, 2, 3]
        await c.close()

    async def test_fetch_all_is_list(self, clean_env):
        c = _client()
        with aioresponses() as m:
            m.get(_url_re("/public/t"), payload=[{"id": 1}, {"id": 2}])
            m.get(_url_re("/public/t"), payload=[])
            rows = await c.fetch_all("public", "t", page_size=2)
        assert rows == [{"id": 1}, {"id": 2}]
        await c.close()

    async def test_honors_max_rows(self, clean_env):
        c = _client()
        with aioresponses() as m:
            m.get(_url_re("/public/t"), payload=[{"id": i} for i in range(3)])
            m.get(_url_re("/public/t"), payload=[{"id": i + 3} for i in range(3)])
            rows = [r async for r in c.paginate("public", "t", page_size=3, max_rows=4)]
        assert [r["id"] for r in rows] == [0, 1, 2, 3]
        await c.close()

    async def test_strips_caller_limit_offset(self, clean_env):
        c = _client()
        captured_params: list[dict] = []

        def _capture(_url, **kwargs):
            # aioresponses callback signature
            captured_params.append(dict(kwargs.get("params") or {}))

        with aioresponses() as m:
            m.get(_url_re("/public/t"), payload=[], callback=_capture)
            [_ async for _ in c.paginate("public", "t", page_size=5,
                                         params={"limit": 999, "offset": 42, "select": "id"})]

        # The request params that reached aiohttp — verify paginator overrode limit/offset.
        assert captured_params, "expected at least one request"
        sent = captured_params[0]
        assert sent["limit"] == 5 and sent["offset"] == 0 and sent["select"] == "id"
        await c.close()


class TestConcurrencyCap:
    async def test_semaphore_caps_inflight(self, clean_env):
        c = _client(max_concurrency=3)

        current = 0
        peak = 0
        lock = asyncio.Lock()

        async def _tracking_callback(url, **kwargs):
            nonlocal current, peak
            async with lock:
                current += 1
                if current > peak:
                    peak = current
            await asyncio.sleep(0.02)
            async with lock:
                current -= 1

        with aioresponses() as m:
            # `repeat=True` lets a single matcher serve all 12 concurrent calls.
            m.get(_url_re("/public/t"), payload=[], callback=_tracking_callback, repeat=True)

            await asyncio.gather(*[c.get("public", "t") for _ in range(12)])

        assert peak <= 3, f"expected peak concurrency ≤ 3, got {peak}"
        await c.close()


class TestContextManager:
    async def test_closes_on_exit(self, clean_env):
        async with _client() as c:
            with aioresponses() as m:
                m.get(_url_re("/public/t"), payload=[])
                await c.get("public", "t")
        # After context exit the session should be closed.
        assert c._closed is True

    async def test_use_after_close_raises(self, clean_env):
        c = _client()
        await c.close()
        with pytest.raises(RuntimeError, match="is closed"):
            await c.get("public", "t")
