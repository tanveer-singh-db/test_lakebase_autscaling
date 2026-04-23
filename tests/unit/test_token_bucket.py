"""Unit tests for _TokenBucket."""

from __future__ import annotations

import asyncio

import pytest

from lakebase_utils.lakebase_api_async import _TokenBucket


pytestmark = pytest.mark.unit


class TestTokenBucket:
    async def test_initial_capacity_burst(self):
        bucket = _TokenBucket(rate_per_sec=5, capacity=5)
        t0 = asyncio.get_event_loop().time()
        for _ in range(5):
            await bucket.acquire()
        elapsed = asyncio.get_event_loop().time() - t0
        assert elapsed < 0.05, f"5 burst acquires should be ~instant, got {elapsed:.3f}s"

    async def test_refill_rate(self):
        # capacity=1 means after the initial token, each acquire must wait 1/rate seconds.
        bucket = _TokenBucket(rate_per_sec=10, capacity=1)
        await bucket.acquire()                      # initial token, instant
        t0 = asyncio.get_event_loop().time()
        for _ in range(5):
            await bucket.acquire()                  # 5 more at 10/sec → ~0.5s
        elapsed = asyncio.get_event_loop().time() - t0
        assert 0.35 < elapsed < 0.8, f"5 acquires at 10/s should take ~0.5s, got {elapsed:.3f}s"

    async def test_concurrent_acquirers_share_bucket(self):
        # Three concurrent acquirers each take 3 tokens at 10/sec, capacity=1.
        # Total tokens = 9. Wall time should be ≈ 8/10 = 0.8 s (first is instant).
        bucket = _TokenBucket(rate_per_sec=10, capacity=1)

        async def worker():
            for _ in range(3):
                await bucket.acquire()

        t0 = asyncio.get_event_loop().time()
        await asyncio.gather(worker(), worker(), worker())
        elapsed = asyncio.get_event_loop().time() - t0
        assert 0.6 < elapsed < 1.2, f"expected ~0.8s, got {elapsed:.3f}s"

    async def test_invalid_rate_raises(self):
        with pytest.raises(ValueError):
            _TokenBucket(rate_per_sec=0)
        with pytest.raises(ValueError):
            _TokenBucket(rate_per_sec=-1)
