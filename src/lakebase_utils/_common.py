"""Shared helpers used by both `LakebaseDataApiClient` (sync) and
`AsyncLakebaseDataApiClient` (async).

Kept deliberately small and dependency-light. `WorkspaceClient` is imported
lazily inside `_make_ws` so callers that only use static-token auth don't
need the Databricks SDK installed.
"""

from __future__ import annotations

import os
from typing import Any


def _make_ws(**kwargs: Any):
    """Construct a WorkspaceClient from only the kwargs actually provided.

    Passing no kwargs lets the SDK use ambient auth (notebook runtime, env
    vars, `~/.databrickscfg`). Forcing a host when one isn't wanted breaks
    that path.
    """
    from databricks.sdk import WorkspaceClient

    return WorkspaceClient(**{k: v for k, v in kwargs.items() if v})


def resolve_base_url(
    base_url: str | None,
    host: str | None,
    workspace_id: str | None,
    database: str | None,
) -> str:
    """Resolve the Lakebase Data API base URL from explicit value,
    host+workspace_id+database parts, or the LAKEBASE_API_URL env var.

    Precedence: explicit `base_url` > parts > env var.
    """
    if base_url:
        return base_url.rstrip("/")
    if host and workspace_id and database:
        return f"https://{host}/api/2.0/workspace/{workspace_id}/rest/{database}".rstrip("/")
    env_url = os.environ.get("LAKEBASE_API_URL", "")
    if env_url:
        return env_url.rstrip("/")
    raise ValueError(
        "base URL is required — pass `base_url=...`, "
        "`host=... workspace_id=... database=...`, or set LAKEBASE_API_URL"
    )


def resolve_auth(
    auth_mode: str | None,
    *,
    token: str | None = None,
    profile: str | None = None,
    workspace_host: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> tuple[str | None, Any]:
    """Resolve auth configuration. Returns `(static_token, workspace_client)`.

    Exactly one of the two return fields is meaningful at a time:
    static_token is set for `oauth_token` and the auto "explicit token" path;
    workspace_client is set for `user_oauth` / `sp_oauth` and the auto
    "SDK ambient" path.

    `auth_mode=None` is the auto path: explicit `token` kwarg > env var >
    SDK ambient auth. Explicit modes validate their required fields and
    raise `ValueError` with a message naming exactly what's missing.
    """
    if auth_mode is None:
        # Auto: explicit token > env > SDK ambient
        static = token or os.environ.get("LAKEBASE_API_TOKEN")
        if static:
            return static, None
        ws = _make_ws(
            host=workspace_host, profile=profile,
            client_id=client_id, client_secret=client_secret,
        )
        return None, ws

    if auth_mode == "oauth_token":
        static = token or os.environ.get("LAKEBASE_API_TOKEN")
        if not static:
            raise ValueError("auth_mode='oauth_token' requires: token (or LAKEBASE_API_TOKEN env var)")
        return static, None

    if auth_mode == "user_oauth":
        ws = _make_ws(host=workspace_host, profile=profile)
        return None, ws

    if auth_mode == "sp_oauth":
        missing = [n for n, v in (("client_id", client_id), ("client_secret", client_secret)) if not v]
        if missing:
            raise ValueError(f"auth_mode='sp_oauth' requires: {', '.join(missing)}")
        ws = _make_ws(host=workspace_host, client_id=client_id, client_secret=client_secret)
        return None, ws

    raise ValueError(
        f"unknown auth_mode: {auth_mode!r} "
        "(expected 'oauth_token', 'user_oauth', 'sp_oauth', or None for auto)"
    )
