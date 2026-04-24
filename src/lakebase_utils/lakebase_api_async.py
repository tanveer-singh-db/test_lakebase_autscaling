"""Async client for the Lakebase Data API (PostgREST-compatible REST).

Mirrors `LakebaseDataApiClient` (sync) with asyncio + aiohttp, plus:
  * concurrency cap via `asyncio.Semaphore`
  * optional req/sec cap via a token bucket
  * retries with exponential backoff + jitter, honouring `Retry-After` on 429

Auth plumbing is shared with the sync client via `_common`. The Databricks
SDK is sync; we call `WorkspaceClient.config.authenticate()` inside
`asyncio.to_thread` and cache the resulting header for ~10 min (the SDK
refreshes its own underlying credential silently).
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, AsyncIterator

import aiohttp

from ._common import resolve_auth, resolve_base_url


_RETRYABLE_STATUSES: tuple[int, ...] = (408, 425, 429, 500, 502, 503, 504)
_AUTH_CACHE_TTL_SECONDS = 600.0  # 10 min; SDK refreshes internally as needed
_CRED_REFRESH_MARGIN = timedelta(minutes=5)


def _cred_is_expiring(cred) -> bool:
    """True if a `DatabaseCredential` is within 5 min of its `expire_time`."""
    exp = getattr(cred, "expire_time", None)
    if exp is None:
        return True
    if hasattr(exp, "seconds"):
        exp = datetime.fromtimestamp(exp.seconds, tz=timezone.utc)
    elif exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp - datetime.now(timezone.utc) <= _CRED_REFRESH_MARGIN


class LakebaseDataApiError(Exception):
    """Raised on non-retryable Lakebase Data API error responses.

    Exposes `.status` (HTTP status), `.body` (raw response body), and the
    parsed PostgREST fields `.code`, `.message`, `.hint` when available, so
    callers can match on e.g. `err.code == "PGRST301"` without regex.
    """

    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        self.code: str | None = None
        self.message: str | None = None
        self.hint: str | None = None
        try:
            parsed = json.loads(body) if body else {}
            if isinstance(parsed, dict):
                self.code = parsed.get("code")
                self.message = parsed.get("message")
                self.hint = parsed.get("hint")
        except (ValueError, TypeError):
            pass
        msg = f"HTTP {status}"
        if self.code:
            msg += f" [{self.code}]"
        if self.message:
            msg += f": {self.message}"
        super().__init__(msg)


class _TokenBucket:
    """Non-blocking token bucket with fractional refill.

    `rate_per_sec` tokens are added per second up to `capacity`. Each
    `acquire()` call consumes one token, awaiting refill if empty.
    """

    def __init__(self, rate_per_sec: float, capacity: int | None = None):
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be > 0")
        self._rate = float(rate_per_sec)
        self._capacity = float(capacity if capacity is not None else max(1, int(rate_per_sec)))
        self._tokens = self._capacity
        self._last = asyncio.get_event_loop().time()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = asyncio.get_event_loop().time()
                self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) / self._rate
            await asyncio.sleep(wait)


def _parse_retry_after(value: str | None) -> float | None:
    """Parse an HTTP Retry-After header (either delta-seconds or HTTP-date).

    Returns the delay in seconds, or None if parsing fails.
    """
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        if dt is None:
            return None
        delta = dt.timestamp() - time.time()
        return max(0.0, delta)
    except (TypeError, ValueError):
        return None


def _backoff_delay(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff with small additive jitter."""
    exp = min(cap, base * (2 ** (attempt - 1)))
    return exp + random.uniform(0.0, base)


class AsyncLakebaseDataApiClient:
    """Async client for the Lakebase Data API.

    URL: supply `base_url` directly, or build it from pieces via
    `host` + `workspace_id` + `database`, or let it fall back to the
    `LAKEBASE_API_URL` env var.

    Auth: pick a mode via `auth_mode`, or omit it for auto-detect. Same four
    modes as the sync client: `oauth_token`, `user_oauth`, `sp_oauth`, None.

    Resilience:
      * `max_concurrency` — cap on in-flight requests (asyncio.Semaphore).
      * `max_requests_per_second` — optional req/sec cap (token bucket).
      * `max_attempts` / `base_backoff` / `max_backoff` / `retry_statuses`
        — retry policy; honours `Retry-After` on matching responses.
    """

    def __init__(
        self,
        *,
        # URL
        base_url: str | None = None,
        host: str | None = None,
        workspace_id: str | None = None,
        database: str | None = None,
        # Auth
        auth_mode: str | None = None,
        token: str | None = None,
        profile: str | None = None,
        workspace_host: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        # Required when running under notebook ambient auth — see sync client
        # docstring for why `config.authenticate()` alone isn't enough there.
        endpoint_path: str | None = None,
        # Resilience
        max_concurrency: int = 10,
        max_requests_per_second: float | None = None,
        max_attempts: int = 5,
        base_backoff: float = 0.5,
        max_backoff: float = 30.0,
        retry_statuses: tuple[int, ...] = _RETRYABLE_STATUSES,
        # Tuning
        default_page_size: int = 1000,
        timeout: float = 30.0,
    ):
        self.base_url = resolve_base_url(base_url, host, workspace_id, database)
        self.auth_mode = auth_mode
        self._static_token, self._ws = resolve_auth(
            auth_mode,
            token=token, profile=profile, workspace_host=workspace_host,
            client_id=client_id, client_secret=client_secret,
        )
        self._endpoint_path = endpoint_path
        self._cached_cred = None  # DatabaseCredential with .token / .expire_time

        self._max_attempts = max_attempts
        self._base_backoff = base_backoff
        self._max_backoff = max_backoff
        self._retry_statuses = tuple(retry_statuses)
        self._default_page_size = default_page_size
        self._timeout = timeout

        self._sem = asyncio.Semaphore(max_concurrency)
        self._bucket: _TokenBucket | None = (
            _TokenBucket(max_requests_per_second) if max_requests_per_second else None
        )

        self._token_lock = asyncio.Lock()
        self._cached_auth_header: dict[str, str] | None = None
        self._auth_expires_at: float = 0.0

        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        self._closed = False

    async def _auth_header(self) -> dict[str, str]:
        if self._static_token:
            return {
                "Authorization": f"Bearer {self._static_token}",
                "Accept": "application/json",
            }
        async with self._token_lock:
            # Endpoint-scoped credential produces a proper JWT — required for
            # notebook ambient auth (which otherwise returns a non-JWT session
            # credential that PostgREST rejects).
            if self._endpoint_path:
                if self._cached_cred is None or _cred_is_expiring(self._cached_cred):
                    self._cached_cred = await asyncio.to_thread(
                        self._ws.postgres.generate_database_credential,
                        endpoint=self._endpoint_path,
                    )
                return {
                    "Authorization": f"Bearer {self._cached_cred.token}",
                    "Accept": "application/json",
                }
            # Fallback: the SDK's current workspace bearer. Only works outside
            # notebooks (SP M2M, user-OAuth CLI profile).
            now = time.monotonic()
            if self._cached_auth_header and now < self._auth_expires_at:
                return self._cached_auth_header
            raw = await asyncio.to_thread(self._ws.config.authenticate)
            self._cached_auth_header = {
                "Authorization": raw["Authorization"],
                "Accept": "application/json",
            }
            self._auth_expires_at = now + _AUTH_CACHE_TTL_SECONDS
            return self._cached_auth_header

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._closed:
            raise RuntimeError("AsyncLakebaseDataApiClient is closed")
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
        return self._session

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        timeout: float | None = None,
    ) -> Any:
        session = await self._ensure_session()
        effective_timeout = aiohttp.ClientTimeout(total=timeout if timeout is not None else self._timeout)

        attempt = 0
        while True:
            attempt += 1
            await self._sem.acquire()
            try:
                if self._bucket is not None:
                    await self._bucket.acquire()
                headers = await self._auth_header()
                try:
                    async with session.request(
                        method, url,
                        params=params,
                        headers=headers,
                        timeout=effective_timeout,
                    ) as resp:
                        if resp.status in self._retry_statuses and attempt < self._max_attempts:
                            retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                            delay = (
                                retry_after
                                if retry_after is not None
                                else _backoff_delay(attempt, self._base_backoff, self._max_backoff)
                            )
                            await asyncio.sleep(delay)
                            continue
                        if resp.status >= 400:
                            body = await resp.text()
                            raise LakebaseDataApiError(resp.status, body)
                        return await resp.json()
                except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, asyncio.TimeoutError) as exc:
                    if attempt >= self._max_attempts:
                        raise
                    await asyncio.sleep(_backoff_delay(attempt, self._base_backoff, self._max_backoff))
                    continue
            finally:
                self._sem.release()

    async def get(
        self,
        schema: str,
        table: str,
        *,
        params: dict | None = None,
        timeout: float | None = None,
    ) -> list[dict]:
        """GET one page. Pass PostgREST query params via `params`."""
        url = f"{self.base_url}/{schema}/{table}"
        return await self._request("GET", url, params=params, timeout=timeout)

    async def paginate(
        self,
        schema: str,
        table: str,
        *,
        params: dict | None = None,
        page_size: int | None = None,
        max_rows: int | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[dict]:
        """Yield all rows, paginating via `limit`/`offset`.

        Any caller-provided `limit`/`offset` in `params` is ignored — the
        paginator drives both. Everything else (`select`, filters, `order`)
        carries through to every page.
        """
        size = page_size or self._default_page_size
        base = {k: v for k, v in (params or {}).items() if k not in ("limit", "offset")}
        offset = 0
        yielded = 0
        while True:
            page = await self.get(
                schema, table,
                params={**base, "limit": size, "offset": offset},
                timeout=timeout,
            )
            if not page:
                return
            for row in page:
                yield row
                yielded += 1
                if max_rows is not None and yielded >= max_rows:
                    return
            if len(page) < size:
                return
            offset += size

    async def fetch_all(
        self,
        schema: str,
        table: str,
        *,
        params: dict | None = None,
        page_size: int | None = None,
        max_rows: int | None = None,
        timeout: float | None = None,
    ) -> list[dict]:
        """Convenience wrapper: `list(paginate(...))`."""
        return [
            row async for row in self.paginate(
                schema, table,
                params=params, page_size=page_size,
                max_rows=max_rows, timeout=timeout,
            )
        ]

    async def close(self) -> None:
        """Close the underlying aiohttp session. Idempotent."""
        self._closed = True
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> "AsyncLakebaseDataApiClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    def __repr__(self) -> str:
        return f"AsyncLakebaseDataApiClient(base_url={self.base_url!r})"
