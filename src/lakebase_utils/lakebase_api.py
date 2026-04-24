"""Client for the Lakebase Data API (PostgREST-compatible REST).

URL shape:  {base_url}/{schema}/{table}
Auth:       Authorization: Bearer <azure-databricks-oauth-token>

The Data API accepts a plain Azure Databricks OAuth token. The authenticated
identity must have a corresponding Postgres role created via the
`databricks_auth` extension — otherwise PostgREST returns `PGRST301`. See
`docs/fix_data_api_auth.md` / `src/provision_data_api_role.sql`.

**Do not** use the project owner identity — `authenticator` can't assume
elevated roles.

`requests` isn't preinstalled on DBR; in a notebook run `%pip install requests`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterator

import requests

from ._common import resolve_auth, resolve_base_url


_REFRESH_MARGIN = timedelta(minutes=5)


class LakebaseDataApiClient:
    """Thin client for the Lakebase Data API.

    URL: supply `base_url` directly, or build it from pieces via
    `host` + `workspace_id` + `database`, or let it fall back to the
    `LAKEBASE_API_URL` env var.

    Auth: pick a mode via `auth_mode`, or omit it for auto-detect (token kwarg
    or env var → SDK ambient auth). Explicit modes:

        oauth_token:  `token` (or LAKEBASE_API_TOKEN env). Static; rebuild
                      the client after ~1h.
        user_oauth:   SDK-based user auth; optional `profile` / `workspace_host`
                      (omit in a notebook — ambient auth is automatic).
        sp_oauth:     SDK-based service-principal M2M; requires
                      `client_id` + `client_secret` [+ `workspace_host`].

    `endpoint_path` is required for notebook-ambient auth:
    `WorkspaceClient().config.authenticate()` in a notebook returns the
    runtime's session credential, which is NOT a JWT and fails the Data
    API's validation ("Provided authentication token is not a valid JWT
    encoding"). Setting `endpoint_path` tells the client to mint a proper
    JWT via `w.postgres.generate_database_credential(endpoint=...)` instead.
    It's optional for SP M2M and user OAuth from a CLI profile — those
    already produce JWTs through `config.authenticate()`.

    Methods:
      get(schema, table, params=None)            -> list[dict]   (one page)
      paginate(schema, table, params=None, ...)  -> Iterator[dict] (all rows)
      fetch_all(schema, table, params=None, ...) -> list[dict]   (all rows)
    """

    def __init__(
        self,
        *,
        # URL, pick one of: base_url | host+workspace_id+database | LAKEBASE_API_URL env
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
        # Required when running under notebook ambient auth — see class docstring.
        endpoint_path: str | None = None,
        default_page_size: int = 1000,
        timeout: int = 30,
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
        self._default_page_size = default_page_size
        self._timeout = timeout
        self._session = requests.Session()

    def _token(self) -> str:
        if self._static_token:
            return self._static_token
        if self._endpoint_path:
            return self._endpoint_scoped_token()
        return self._ws.config.authenticate()["Authorization"].split(" ", 1)[1]

    def _endpoint_scoped_token(self) -> str:
        """Mint a JWT via `generate_database_credential`, cached until near expiry."""
        if self._cached_cred is None or self._is_expiring(self._cached_cred):
            self._cached_cred = self._ws.postgres.generate_database_credential(
                endpoint=self._endpoint_path,
            )
        return self._cached_cred.token

    @staticmethod
    def _is_expiring(cred) -> bool:
        exp = getattr(cred, "expire_time", None)
        if exp is None:
            return True
        # SDK returns a proto-style Timestamp with `.seconds`; accept datetime too.
        if hasattr(exp, "seconds"):
            exp = datetime.fromtimestamp(exp.seconds, tz=timezone.utc)
        elif exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp - datetime.now(timezone.utc) <= _REFRESH_MARGIN

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"}

    def get(
        self,
        schema: str,
        table: str,
        *,
        params: dict | None = None,
        timeout: int | None = None,
    ) -> list[dict]:
        """GET one page. Pass PostgREST params like
        `{"select": "id,name", "id": "gte.2", "order": "id.desc", "limit": 10}`.
        """
        url = f"{self.base_url}/{schema}/{table}"
        r = self._session.get(url, headers=self._headers(), params=params, timeout=timeout or self._timeout)
        r.raise_for_status()
        return r.json()

    def paginate(
        self,
        schema: str,
        table: str,
        *,
        params: dict | None = None,
        page_size: int | None = None,
        max_rows: int | None = None,
        timeout: int | None = None,
    ) -> Iterator[dict]:
        """Yield all rows, paginating via `limit`/`offset` until a short page.

        `params` are forwarded to each request; any `limit`/`offset` in it
        are overridden by the paginator. Stop early with `max_rows`.
        """
        size = page_size or self._default_page_size
        offset = 0
        yielded = 0
        base_params = dict(params or {})
        # Caller-specified limit/offset would fight the paginator.
        base_params.pop("limit", None)
        base_params.pop("offset", None)

        while True:
            page_params = {**base_params, "limit": size, "offset": offset}
            page = self.get(schema, table, params=page_params, timeout=timeout)
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

    def fetch_all(
        self,
        schema: str,
        table: str,
        *,
        params: dict | None = None,
        page_size: int | None = None,
        max_rows: int | None = None,
        timeout: int | None = None,
    ) -> list[dict]:
        """Convenience: `list(paginate(...))`."""
        return list(self.paginate(
            schema, table,
            params=params, page_size=page_size, max_rows=max_rows, timeout=timeout,
        ))

    def close(self) -> None:
        self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def __repr__(self) -> str:
        return f"LakebaseDataApiClient(base_url={self.base_url!r})"
