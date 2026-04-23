"""Fetch data from the Lakebase Data API (PostgREST-compatible REST).

URL shape:  {BASE_URL}/{schema}/{table}
Auth:       Authorization: Bearer <azure-databricks-oauth-token>

The Data API accepts a plain Azure Databricks OAuth token (not an
endpoint-scoped Postgres credential). The authenticated identity must have a
corresponding Postgres role created via the `databricks_auth` extension —
otherwise PostgREST returns `PGRST301 invalid token permissions`.

One-time setup (run as a non-owner Lakebase user with DDL perms):

    CREATE EXTENSION IF NOT EXISTS databricks_auth;
    SELECT databricks_create_role('<sp-client-id-or-email>', 'SERVICE_PRINCIPAL');  -- or 'USER'
    GRANT "<identity>" TO authenticator;
    GRANT USAGE ON SCHEMA public TO "<identity>";
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "<identity>";

Do NOT use the project owner identity — the owner has elevated privileges and
`authenticator` can't assume it.

`requests` isn't preinstalled on DBR; in a notebook run `%pip install requests`.
"""

import os

import requests
from databricks.sdk import WorkspaceClient

# Base URL shape:
#   https://<lakebase-host>/api/2.0/workspace/<workspace-id>/rest/<database>
# Get it from the **Data API** page of your project in the Lakebase UI.
BASE_URL = os.environ["LAKEBASE_API_URL"].rstrip("/")
PROFILE = os.environ.get("DATABRICKS_CONFIG_PROFILE")  # None -> SDK ambient auth

_w = WorkspaceClient(profile=PROFILE) if PROFILE else WorkspaceClient()


def _headers() -> dict[str, str]:
    # Prefer an explicit token when set; otherwise mint one from the SDK auth.
    token = os.environ.get("LAKEBASE_API_TOKEN") or _w.config.authenticate()["Authorization"].split(" ", 1)[1]
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def fetch(schema: str, table: str, params: dict | None = None, timeout: int = 30) -> list[dict]:
    """GET rows from `{schema}.{table}`.

    `params` maps to PostgREST query parameters:
        {"select": "id,name"}                 # projection
        {"id": "gte.2"}                       # id >= 2
        {"order": "price_cents.desc"}         # sort
        {"limit": 10, "offset": 0}            # pagination
    """
    url = f"{BASE_URL}/{schema}/{table}"
    r = requests.get(url, headers=_headers(), params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


if __name__ == "__main__":
    rows = fetch("public", "widgets", params={"order": "id.asc", "limit": 10})
    for row in rows:
        print(row)
