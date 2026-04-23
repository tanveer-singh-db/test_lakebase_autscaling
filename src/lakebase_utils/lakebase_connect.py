"""Client for Databricks Lakebase Autoscaling Postgres.

Supports four auth modes:

- **static**: plain Postgres `user` + `password` (e.g. the `authenticator` role).
- **oauth_token**: you already have an OAuth token (from the CLI, UI, another
  script, etc.). Pass `oauth_user` + `token`. No SDK auth, no refresh — the
  token is valid until its ~1h TTL; build a new client after that.
- **user_oauth**: Databricks user-OAuth. Pass the user's email as `oauth_user`.
  `WorkspaceClient()` uses ambient auth by default (notebook runtime, env
  vars, `~/.databrickscfg`). Optional `workspace_host` / `profile` overrides.
- **sp_oauth**: Databricks service-principal M2M. Pass `client_id` and
  `client_secret` (+ `workspace_host` if ambient auth won't find it).

For `user_oauth` and `sp_oauth`, tokens are minted via the typed SDK method
`w.postgres.generate_database_credential(endpoint=...)`, cached, and refreshed
automatically a few minutes before `expire_time`.

DBR 16.4 LTS / Serverless ship `psycopg2` and `databricks-sdk` preinstalled.
Locally: `pip install psycopg2-binary databricks-sdk`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import unquote, urlparse

import psycopg2
from psycopg2 import pool

log = logging.getLogger(__name__)

_REFRESH_MARGIN = timedelta(minutes=5)


def _split_statements(sql: str) -> list[str]:
    """Split SQL on `;` while respecting single quotes, double-quoted
    identifiers, and `--` / `/* */` comments. Dollar-quoted strings
    (`$$...$$`) are NOT handled — avoid those in scripts split this way.
    """
    stmts, buf = [], []
    i, n = 0, len(sql)
    in_sq = in_dq = in_line = in_block = False
    while i < n:
        c = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if in_line:
            if c == "\n":
                in_line = False
        elif in_block:
            if c == "*" and nxt == "/":
                buf.append("*/"); i += 2; in_block = False; continue
        elif in_sq:
            if c == "'":
                in_sq = False
        elif in_dq:
            if c == '"':
                in_dq = False
        else:
            if c == "-" and nxt == "-":
                in_line = True; buf.append("--"); i += 2; continue
            if c == "/" and nxt == "*":
                in_block = True; buf.append("/*"); i += 2; continue
            if c == "'":
                in_sq = True
            elif c == '"':
                in_dq = True
            elif c == ";":
                stmt = "".join(buf).strip()
                if stmt:
                    stmts.append(stmt)
                buf = []; i += 1
                continue
        buf.append(c); i += 1
    tail = "".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


def _build_workspace_client(
    *,
    workspace_host: str | None = None,
    profile: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
):
    """Construct a WorkspaceClient from only the kwargs actually provided.

    Passing no kwargs lets the SDK fall back to ambient auth (e.g. a Databricks
    notebook runtime). Forcing a host when none is wanted breaks that path.
    """
    from databricks.sdk import WorkspaceClient

    kwargs: dict[str, Any] = {}
    if workspace_host:
        kwargs["host"] = workspace_host
    if profile:
        kwargs["profile"] = profile
    if client_id:
        kwargs["client_id"] = client_id
    if client_secret:
        kwargs["client_secret"] = client_secret
    return WorkspaceClient(**kwargs)


class _CredentialCache:
    """Caches a Lakebase endpoint credential and re-mints before expiry."""

    def __init__(self, ws, endpoint_path: str):
        self._ws = ws
        self._endpoint_path = endpoint_path
        self._cred = None

    def token(self) -> str:
        if self._cred is None or self._is_expiring(self._cred):
            self._cred = self._ws.postgres.generate_database_credential(
                endpoint=self._endpoint_path,
            )
        return self._cred.token

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


class _OAuthConnectionPool(pool.ThreadedConnectionPool):
    """ThreadedConnectionPool that stamps a fresh OAuth token onto each new
    physical connection. Pooled connections reuse their original token until
    they're dropped; replacements get a fresh one from the credential cache.
    """

    def __init__(self, minconn, maxconn, *args, credential_cache: _CredentialCache, **kwargs):
        self._credential_cache = credential_cache
        super().__init__(minconn, maxconn, *args, **kwargs)

    def _connect(self, key=None):
        kwargs = {**self._kwargs, "password": self._credential_cache.token()}
        conn = psycopg2.connect(*self._args, **kwargs)
        if key is not None:
            self._used[key] = conn
            self._rused[id(conn)] = key
        else:
            self._pool.append(conn)
        return conn


class LakebaseAutoscalingClient:
    """Connect to Lakebase Autoscaling Postgres and run queries.

    Pick one of four auth modes via `auth_mode`:

        static:      user + password
        oauth_token: oauth_user + token                (token you minted elsewhere)
        user_oauth:  oauth_user + endpoint_path        [+ workspace_host, profile]
        sp_oauth:    client_id + client_secret + endpoint_path  [+ workspace_host]

    For `user_oauth` / `sp_oauth`, `endpoint_path` is required (e.g.
    `projects/my-project/branches/production/endpoints/primary`). Tokens are
    cached and refreshed a few minutes before they expire.
    """

    def __init__(
        self,
        host: str | None = None,
        database: str | None = None,
        *,
        auth_mode: str,
        # Alternative to host/database/port/user(/password): a full Postgres URL
        # of the shape `postgresql://[user[:password]@]host[:port]/database`.
        # Explicit kwargs (host, database, port, user, oauth_user, password)
        # win over values parsed from the URL, so you can override any piece.
        connection_string: str | None = None,
        port: int = 5432,
        sslmode: str = "require",
        # static
        user: str | None = None,
        password: str | None = None,
        # oauth_token
        token: str | None = None,
        # oauth (shared)
        endpoint_path: str | None = None,
        workspace_host: str | None = None,
        # user_oauth
        oauth_user: str | None = None,
        profile: str | None = None,
        # sp_oauth
        client_id: str | None = None,
        client_secret: str | None = None,
        # pool
        minconn: int = 1,
        maxconn: int = 5,
    ):
        if connection_string:
            u = urlparse(connection_string)
            host = host or u.hostname
            if u.port:
                port = u.port
            database = database or (u.path.lstrip("/") or None)
            url_user = unquote(u.username) if u.username else None
            url_pw = unquote(u.password) if u.password else None
            # Populate whichever "user" field the chosen auth_mode actually uses.
            user = user or url_user
            oauth_user = oauth_user or url_user
            password = password or url_pw

        if not host:
            raise ValueError("host is required (pass `host=...` or `connection_string=...`)")
        if not database:
            raise ValueError("database is required (pass `database=...` or `connection_string=...`)")

        self.host = host
        self.database = database
        self.auth_mode = auth_mode
        self._cred_cache: _CredentialCache | None = None

        common = {
            "host": host,
            "port": port,
            "dbname": database,
            "sslmode": sslmode,
        }

        if auth_mode == "static":
            missing = [n for n, v in (("user", user), ("password", password)) if not v]
            if missing:
                raise ValueError(f"auth_mode='static' requires: {', '.join(missing)}")
            self._pool = pool.ThreadedConnectionPool(
                minconn, maxconn, user=user, password=password, **common,
            )

        elif auth_mode == "oauth_token":
            missing = [n for n, v in (("oauth_user", oauth_user), ("token", token)) if not v]
            if missing:
                raise ValueError(f"auth_mode='oauth_token' requires: {', '.join(missing)}")
            # Token is used as the Postgres password. Valid until its ~1h TTL;
            # build a new client after that.
            self._pool = pool.ThreadedConnectionPool(
                minconn, maxconn, user=oauth_user, password=token, **common,
            )

        elif auth_mode == "user_oauth":
            missing = [n for n, v in (("oauth_user", oauth_user), ("endpoint_path", endpoint_path)) if not v]
            if missing:
                raise ValueError(f"auth_mode='user_oauth' requires: {', '.join(missing)}")
            ws = _build_workspace_client(workspace_host=workspace_host, profile=profile)
            self._cred_cache = _CredentialCache(ws, endpoint_path)
            self._pool = _OAuthConnectionPool(
                minconn, maxconn,
                user=oauth_user, **common,
                credential_cache=self._cred_cache,
            )

        elif auth_mode == "sp_oauth":
            missing = [n for n, v in (
                ("client_id", client_id),
                ("client_secret", client_secret),
                ("endpoint_path", endpoint_path),
            ) if not v]
            if missing:
                raise ValueError(f"auth_mode='sp_oauth' requires: {', '.join(missing)}")
            ws = _build_workspace_client(
                workspace_host=workspace_host,
                client_id=client_id,
                client_secret=client_secret,
            )
            self._cred_cache = _CredentialCache(ws, endpoint_path)
            self._pool = _OAuthConnectionPool(
                minconn, maxconn,
                user=client_id, **common,
                credential_cache=self._cred_cache,
            )

        else:
            raise ValueError(
                f"unknown auth_mode: {auth_mode!r} "
                "(expected 'static', 'oauth_token', 'user_oauth', or 'sp_oauth')"
            )

    def execute(self, sql: str) -> None:
        """Run DDL/DML. Multi-statement scripts are split on `;` and run in a
        single transaction; any failure rolls everything back.
        """
        conn = self._pool.getconn()
        try:
            with conn, conn.cursor() as cur:
                for stmt in _split_statements(sql):
                    cur.execute(stmt)
        finally:
            self._pool.putconn(conn)

    def fetch(self, sql: str) -> list[tuple]:
        """Run a SELECT and return the rows as a list of tuples."""
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                return cur.fetchall()
        finally:
            self._pool.putconn(conn)

    def select(self, sql: str, spark=None):
        """Run a SELECT and return a Spark DataFrame.

        `spark` defaults to the active SparkSession (the `spark` that Databricks
        notebooks bind as a global). Pass one explicitly outside a notebook.
        """
        if spark is None:
            from pyspark.sql import SparkSession
            spark = SparkSession.builder.getOrCreate()

        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
        finally:
            self._pool.putconn(conn)

        return spark.createDataFrame(rows, schema=cols)

    def close(self) -> None:
        """Close all pooled connections. Idempotent."""
        if self._pool and not getattr(self._pool, "closed", False):
            self._pool.closeall()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def __repr__(self) -> str:
        return (
            f"LakebaseAutoscalingClient(host={self.host!r}, database={self.database!r}, "
            f"auth_mode={self.auth_mode!r})"
        )


if __name__ == "__main__":
    # Local smoke test: static mode against the endpoint in env vars.
    #
    # Required:
    #   LAKEBASE_HOST      e.g. ep-xxxx.database.<region>.azuredatabricks.net
    #   LAKEBASE_DATABASE  e.g. databricks_postgres
    #   LAKEBASE_USER      e.g. authenticator
    #   LAKEBASE_PASSWORD
    import os

    client = LakebaseAutoscalingClient(
        host=os.environ["LAKEBASE_HOST"],
        database=os.environ.get("LAKEBASE_DATABASE", "databricks_postgres"),
        auth_mode="static",
        user=os.environ["LAKEBASE_USER"],
        password=os.environ["LAKEBASE_PASSWORD"],
    )
    try:
        print(client.fetch("SELECT current_user, current_database(), version()"))
    finally:
        client.close()
