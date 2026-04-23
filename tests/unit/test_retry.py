"""Unit tests for retry loop, backoff, and Retry-After parsing."""

from __future__ import annotations

import asyncio
import re
from unittest.mock import patch

import aiohttp
import pytest
from aioresponses import aioresponses

from lakebase_utils.lakebase_api_async import (
    AsyncLakebaseDataApiClient,
    LakebaseDataApiError,
    _backoff_delay,
    _parse_retry_after,
)


pytestmark = pytest.mark.unit

BASE = "https://x.example.com"

# Capture the real asyncio.sleep before any test patches it.
_REAL_SLEEP = asyncio.sleep


def _url_re(path: str) -> re.Pattern:
    return re.compile(rf"^{re.escape(BASE)}{re.escape(path)}(\?.*)?$")


async def _instant_sleep(*_args, **_kwargs):
    """Drop-in replacement for asyncio.sleep that yields control but returns instantly."""
    await _REAL_SLEEP(0)


def _client(**overrides):
    kwargs = dict(
        base_url=BASE,
        auth_mode="oauth_token",
        token="tok",
        max_attempts=5,
        base_backoff=0.01,
        max_backoff=0.05,
    )
    kwargs.update(overrides)
    return AsyncLakebaseDataApiClient(**kwargs)


class TestRetryAfterParsing:
    def test_delta_seconds(self):
        assert _parse_retry_after("5") == 5.0
        assert _parse_retry_after(" 2.5 ") == 2.5

    def test_none_and_empty(self):
        assert _parse_retry_after(None) is None
        assert _parse_retry_after("") is None

    def test_invalid_string(self):
        assert _parse_retry_after("not-a-date") is None

    def test_http_date(self):
        # Future HTTP-date should produce a non-negative delay.
        from email.utils import formatdate
        header = formatdate(timeval=None, usegmt=True)
        delay = _parse_retry_after(header)
        assert delay is not None
        assert delay >= 0.0


class TestBackoffDelay:
    def test_grows_exponentially(self):
        # With base=1, cap=1000, expected = min(cap, 1 * 2**(n-1)) + jitter[0, base)
        # attempt 1 → 1 + j     ∈ [1, 2)
        # attempt 3 → 4 + j     ∈ [4, 5)
        d1 = _backoff_delay(1, base=1.0, cap=1000.0)
        d3 = _backoff_delay(3, base=1.0, cap=1000.0)
        assert 1.0 <= d1 < 2.0
        assert 4.0 <= d3 < 5.0

    def test_respects_cap(self):
        d = _backoff_delay(20, base=1.0, cap=5.0)
        # min(cap, 1 * 2**19) = 5; plus jitter < base=1
        assert 5.0 <= d < 6.0


class TestRetryLoop:
    async def test_success_on_first_try(self, clean_env):
        c = _client()
        with aioresponses() as m:
            m.get(_url_re("/public/t"), payload=[{"id": 1}])
            rows = await c.get("public", "t")
        assert rows == [{"id": 1}]
        await c.close()

    async def test_retries_on_429_then_succeeds(self, clean_env):
        c = _client()
        with aioresponses() as m:
            m.get(_url_re("/public/t"), status=429, body="{}")
            m.get(_url_re("/public/t"), status=429, body="{}")
            m.get(_url_re("/public/t"), payload=[{"id": 99}])
            rows = await c.get("public", "t")
        assert rows == [{"id": 99}]
        await c.close()

    async def test_retry_after_header_honored(self, clean_env):
        c = _client()
        sleeps: list[float] = []

        async def spy_sleep(delay):
            sleeps.append(delay)
            await _REAL_SLEEP(0)

        with aioresponses() as m, patch("lakebase_utils.lakebase_api_async.asyncio.sleep", new=spy_sleep):
            m.get(_url_re("/public/t"), status=429, headers={"Retry-After": "3"}, body="{}")
            m.get(_url_re("/public/t"), payload=[])
            await c.get("public", "t")
        assert 3.0 in sleeps, f"expected retry-after=3 to drive a 3s sleep, got {sleeps}"
        await c.close()

    async def test_exhausts_after_max_attempts(self, clean_env):
        c = _client(max_attempts=3)
        with aioresponses() as m, patch("lakebase_utils.lakebase_api_async.asyncio.sleep", new=_instant_sleep):
            for _ in range(3):
                m.get(_url_re("/public/t"), status=503, body='{"code":"X","message":"down"}')
            with pytest.raises(LakebaseDataApiError) as exc_info:
                await c.get("public", "t")
        assert exc_info.value.status == 503
        await c.close()

    async def test_non_retryable_status_fails_fast(self, clean_env):
        c = _client()
        with aioresponses() as m:
            m.get(_url_re("/public/t"),
                  status=404,
                  body='{"code":"PGRST205","message":"not found","hint":null}')
            with pytest.raises(LakebaseDataApiError) as exc_info:
                await c.get("public", "t")
        assert exc_info.value.status == 404
        assert exc_info.value.code == "PGRST205"
        await c.close()

    async def test_network_error_retries_then_succeeds(self, clean_env):
        c = _client()
        with aioresponses() as m, patch("lakebase_utils.lakebase_api_async.asyncio.sleep", new=_instant_sleep):
            m.get(_url_re("/public/t"), exception=aiohttp.ClientConnectionError("boom"))
            m.get(_url_re("/public/t"), payload=[{"ok": True}])
            rows = await c.get("public", "t")
        assert rows == [{"ok": True}]
        await c.close()
