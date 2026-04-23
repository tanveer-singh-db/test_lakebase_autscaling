# `LakebaseDataApiClient`

Thin client over the [Lakebase Data API](https://learn.microsoft.com/en-gb/azure/databricks/oltp/projects/data-api) — the PostgREST-compatible HTTP interface that Lakebase generates from your Postgres schema.

Source: `src/lakebase_utils/lakebase_api.py`.

## Install

```bash
pip install requests databricks-sdk   # both available; sdk only needed when no explicit token
```

DBR 16.4 LTS / Serverless ship `databricks-sdk` preinstalled. `requests` is not preinstalled — `%pip install requests` in a notebook.

## Setup prerequisites

The Data API needs one-time setup before any client will work:

1. **Enable the Data API** in the Lakebase UI for your project.
2. **Provision the identity** you'll authenticate as. Owners can't use the Data API — `authenticator` can't assume elevated roles. Run `src/provision_data_api_role.sql` in the SQL Editor with `<IDENTITY>` set to the user email or SP client_id. That script also grants `<IDENTITY>` to `authenticator`, which is what lets PostgREST switch into that role when serving requests.

`docs/fix_data_api_auth.md` walks through the full diagnosis + fix if you hit `PGRST301`/`42501` errors.

## Quick start

```python
from lakebase_utils.lakebase_api import LakebaseDataApiClient

# base_url + token both come from env vars by default
# (LAKEBASE_API_URL, LAKEBASE_API_TOKEN).
with LakebaseDataApiClient() as client:
    rows = client.get("public", "widgets", params={"limit": 10, "order": "id.asc"})
    for row in rows:
        print(row)
```

## Constructor

```python
LakebaseDataApiClient(
    *,
    # URL — pick one of:
    base_url: str | None = None,           # full URL (or LAKEBASE_API_URL env)
    host: str | None = None,               # or build from pieces:
    workspace_id: str | None = None,
    database: str | None = None,
    # Auth — explicit mode or auto:
    auth_mode: str | None = None,          # 'oauth_token' | 'user_oauth' | 'sp_oauth' | None
    token: str | None = None,
    profile: str | None = None,
    workspace_host: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    default_page_size: int = 1000,
    timeout: int = 30,
)
```

### URL

Three equivalent ways to tell the client where to talk:

1. Full URL: `base_url="https://ep-xxxx.../api/2.0/workspace/<ws-id>/rest/<database>"`.
2. Pieces: `host="ep-xxxx..."`, `workspace_id="<ws-id>"`, `database="databricks_postgres"`. The client joins these into the shape above.
3. Env var: leave all of the above unset and export `LAKEBASE_API_URL`.

All three show up on the project's **Data API** page in the Lakebase UI.

### Auth

Pick a mode via `auth_mode`:

| `auth_mode`     | Required                                      | Notes                                                                  |
|-----------------|-----------------------------------------------|------------------------------------------------------------------------|
| `oauth_token`   | `token` or `LAKEBASE_API_TOKEN` env           | Static Bearer. Token has ~1h TTL; rebuild the client after that.      |
| `user_oauth`    | –                                             | Optional `profile`, `workspace_host`. SDK ambient in notebooks.       |
| `sp_oauth`      | `client_id`, `client_secret`                  | Optional `workspace_host`. SDK refreshes tokens automatically.         |
| *(omitted)*     | –                                             | Auto: `token` kwarg → env → SDK ambient. Good default for notebooks.  |

For `user_oauth` and `sp_oauth`, the SDK handles token refresh internally — use those for anything that runs longer than an hour.

## Methods

### `get(schema, table, params=None, timeout=None)`

One page. `params` is forwarded as PostgREST query params.

```python
rows = client.get(
    "public", "widgets",
    params={
        "select": "id,name,price_cents",  # column projection
        "stock":  "gt.50",                # stock > 50
        "order":  "price_cents.desc",
        "limit":  10,
    },
)
```

PostgREST filter operators: `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `like`, `ilike`, `in`, `is`. Full reference at [postgrest.org](https://postgrest.org/en/stable/references/api.html).

### `paginate(schema, table, params=None, page_size=None, max_rows=None, timeout=None)`

Generator that yields all rows, one at a time, paginating via `limit`/`offset` until a short page. Any `limit`/`offset` you put in `params` is ignored — the paginator drives both.

```python
for row in client.paginate("public", "events", params={"order": "ts.asc"}):
    process(row)

# Stop after 1_000 rows no matter how large the table is
for row in client.paginate("public", "events", max_rows=1000):
    ...
```

Tune `page_size` if you hit the project's **Maximum rows** setting (Data API → Advanced settings) or want fewer round trips on small tables.

### `fetch_all(schema, table, ...)`

Convenience wrapper: `list(paginate(...))`. Use for small tables where you actually want all rows in memory.

```python
roles = client.fetch_all("public", "databricks_list_roles")
```

### `close()` / context manager

Closes the underlying `requests.Session`. Both forms work:

```python
client = LakebaseDataApiClient()
try:
    client.get(...)
finally:
    client.close()

# or
with LakebaseDataApiClient() as client:
    client.get(...)
```

## Samples

### 1. Ambient auth in a Databricks notebook — URL built from pieces

```python
from lakebase_utils.lakebase_api import LakebaseDataApiClient

with LakebaseDataApiClient(
    host="ep-xxxx.database.<region>.azuredatabricks.net",
    workspace_id="<workspace-id>",
    database="databricks_postgres",
    auth_mode="user_oauth",
) as client:
    df_rows = client.fetch_all("public", "widgets", params={"order": "id.asc"})
    display(df_rows)
```

### 2. Pre-minted token (short script)

```bash
export LAKEBASE_API_URL="https://ep-xxxx.database.<region>.azuredatabricks.net/api/2.0/workspace/<ws-id>/rest/databricks_postgres"
export LAKEBASE_API_TOKEN=$(databricks postgres generate-database-credential \
    projects/my-proj/branches/production/endpoints/primary \
    -p my-profile -o json | jq -r '.token')
```

```python
with LakebaseDataApiClient(auth_mode="oauth_token") as client:
    print(client.get("public", "widgets", params={"limit": 5}))
```

Token is valid for ~1h.

### 3. Explicit service principal (CI / scheduled job)

```python
import os
from lakebase_utils.lakebase_api import LakebaseDataApiClient

client = LakebaseDataApiClient(
    base_url=os.environ["LAKEBASE_API_URL"],
    auth_mode="sp_oauth",
    workspace_host=os.environ["DATABRICKS_HOST"],
    client_id=os.environ["DATABRICKS_CLIENT_ID"],
    client_secret=os.environ["DATABRICKS_CLIENT_SECRET"],
)
```

SP-minted workspace tokens are refreshed automatically by the SDK.

### 4. Paginate a large table with filter + projection

```python
with LakebaseDataApiClient() as client:
    total = 0
    for row in client.paginate(
        "public", "orders",
        params={"select": "id,total", "status": "eq.completed", "order": "id.asc"},
        page_size=500,
    ):
        total += row["total"]
    print(f"completed revenue: {total}")
```

### 5. Raw HTTP with `curl`

Sometimes you want to skip the Python client — e.g. debugging from a shell,
scripting in CI, or confirming a response shape. The Data API is just HTTP +
Bearer, so `curl` works.

#### Step 1 — set the REST endpoint

Copy the base URL from **Lakebase project → Data API → API URL**. Everything
after it is `/<schema>/<table>`.

```bash
export REST_ENDPOINT="https://<lakebase-host>/api/2.0/workspace/<workspace-id>/rest/<database>"
```

Example:

```bash
export REST_ENDPOINT="https://ep-xxxx.database.eastus2.azuredatabricks.net/api/2.0/workspace/984752964297111/rest/databricks_postgres"
```

#### Step 2 — mint an OAuth token

The Databricks SDK/CLI mints tokens against whatever identity the current
auth resolves to. Pick the flow that matches how you want the Data API to see
you. In every case you need the Databricks **workspace URL** so the SDK knows
where to authenticate.

**A. User OAuth (interactive developer)**

```bash
# One-off browser login → creates a user-auth profile in ~/.databrickscfg
databricks auth login \
  --host https://<workspace-url>.azuredatabricks.net \
  --profile my-user

export LAKEBASE_API_TOKEN=$(databricks postgres generate-database-credential \
  projects/<project>/branches/<branch>/endpoints/<endpoint> \
  -p my-user -o json | jq -r '.token')
```

**B. Service principal M2M via CLI profile**

```bash
# One-time: create ~/.databrickscfg entry for the SP
databricks configure --profile my-sp \
  --host https://<workspace-url>.azuredatabricks.net
# when prompted, paste client_id / client_secret

export LAKEBASE_API_TOKEN=$(databricks postgres generate-database-credential \
  projects/<project>/branches/<branch>/endpoints/<endpoint> \
  -p my-sp -o json | jq -r '.token')
```

Or, equivalently, put the three fields directly in `~/.databrickscfg`:

```ini
[my-sp]
host          = https://<workspace-url>.azuredatabricks.net
client_id     = <sp-application-id>
client_secret = <sp-secret>
```

**C. Service principal M2M via env vars (stateless, good for CI)**

No profile needed — the SDK reads these on every invocation:

```bash
export DATABRICKS_HOST=https://<workspace-url>.azuredatabricks.net
export DATABRICKS_CLIENT_ID=<sp-application-id>
export DATABRICKS_CLIENT_SECRET=<sp-secret>

export LAKEBASE_API_TOKEN=$(databricks postgres generate-database-credential \
  projects/<project>/branches/<branch>/endpoints/<endpoint> \
  -o json | jq -r '.token')
```

**D. Notebook / Databricks runtime**

Inside a Databricks notebook there's no CLI and no profile — ambient auth
already has the identity. Mint via the SDK directly:

```python
from databricks.sdk import WorkspaceClient
w = WorkspaceClient()
cred = w.postgres.generate_database_credential(
    endpoint="projects/<project>/branches/<branch>/endpoints/<endpoint>"
)
import os
os.environ["LAKEBASE_API_TOKEN"] = cred.token
```

**Verify which identity the token carries** (matters for the `oauth_token`
and direct-curl paths — the identity must own a provisioned Postgres role):

```bash
python -c "import os, base64, json; p = os.environ['LAKEBASE_API_TOKEN'].split('.')[1]; p += '=' * ((4 - len(p) % 4) % 4); print(json.loads(base64.urlsafe_b64decode(p))['sub'])"
```

Emits an email for user OAuth, a UUID for service-principal auth.

#### Step 3 — call the Data API

```bash
# Single row, column projection
curl -H "Authorization: Bearer $LAKEBASE_API_TOKEN" \
  "$REST_ENDPOINT/manual_tests/synced_cdf_source_table?select=customer_id,name"

# Filtered: customer_id >= 100, sorted desc, first 20 rows
curl -H "Authorization: Bearer $LAKEBASE_API_TOKEN" \
  "$REST_ENDPOINT/public/widgets?select=id,name,price_cents&stock=gt.50&order=price_cents.desc&limit=20"

# Paginated: PostgREST honours `Range-Unit: items` + `Range: 0-99`
curl -H "Authorization: Bearer $LAKEBASE_API_TOKEN" \
     -H "Range-Unit: items" -H "Range: 0-99" \
  "$REST_ENDPOINT/public/events?order=ts.asc"

# INSERT
curl -X POST \
  -H "Authorization: Bearer $LAKEBASE_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"Widget","price_cents":1299,"stock":10}' \
  "$REST_ENDPOINT/public/widgets"

# PATCH (update rows matching the filter)
curl -X PATCH \
  -H "Authorization: Bearer $LAKEBASE_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"stock": 0}' \
  "$REST_ENDPOINT/public/widgets?id=eq.42"

# DELETE
curl -X DELETE \
  -H "Authorization: Bearer $LAKEBASE_API_TOKEN" \
  "$REST_ENDPOINT/public/widgets?id=eq.42"

# Inspect error body (handy while debugging auth/403s)
curl -i -H "Authorization: Bearer $LAKEBASE_API_TOKEN" \
  "$REST_ENDPOINT/public/widgets?limit=1"
```

Tokens expire after ~1h — re-run step 2 when you see `PGRST301` again.

## Async client — `AsyncLakebaseDataApiClient`

The async twin of `LakebaseDataApiClient`. Same URL + auth surface, but built on `aiohttp` and adds:

- **Concurrency cap** (`asyncio.Semaphore`) — bounds in-flight requests.
- **Optional req/sec cap** (token bucket) — stops your job from hammering the endpoint even if you spawn lots of tasks.
- **Retries** on transient failures (408, 425, 429, 500, 502, 503, 504, connection errors, timeouts) with exponential backoff + jitter, honouring `Retry-After` on 429.
- **Typed exception** — non-retryable errors raise `LakebaseDataApiError(status, body)` with parsed PostgREST `.code` / `.message` / `.hint`.

Install once:

```bash
pip install aiohttp    # databricks-sdk already installed
```

### Constructor (adds to the sync surface)

```python
AsyncLakebaseDataApiClient(
    # Everything the sync client accepts (base_url / host+workspace_id+database,
    # auth_mode, token, profile, workspace_host, client_id, client_secret,
    # default_page_size, timeout) — plus:
    max_concurrency: int = 10,                # asyncio.Semaphore
    max_requests_per_second: float | None = None,  # token bucket, off by default
    max_attempts: int = 5,                    # retry attempts (including first)
    base_backoff: float = 0.5,                # seconds
    max_backoff: float = 30.0,
    retry_statuses: tuple[int, ...] = (408, 425, 429, 500, 502, 503, 504),
)
```

### Methods

```python
await client.get(schema, table, *, params=None, timeout=None)       # -> list[dict]
async for row in client.paginate(schema, table, ...):               # AsyncIterator[dict]
    ...
rows = await client.fetch_all(schema, table, ...)                   # -> list[dict]
await client.close()                                                 # idempotent
```

Use `async with AsyncLakebaseDataApiClient(...) as client:` to close automatically.

### Async samples

```python
import asyncio
import os
from lakebase_utils.lakebase_api_async import AsyncLakebaseDataApiClient, LakebaseDataApiError

async def main():
    async with AsyncLakebaseDataApiClient(
        host="ep-xxxx.database.<region>.azuredatabricks.net",
        workspace_id="<workspace-id>",
        database="databricks_postgres",
        auth_mode="sp_oauth",
        client_id=os.environ["DATABRICKS_CLIENT_ID"],
        client_secret=os.environ["DATABRICKS_CLIENT_SECRET"],
        workspace_host=os.environ["DATABRICKS_HOST"],
        max_concurrency=10,
        max_requests_per_second=50,
    ) as client:

        # 1. One page
        rows = await client.get("public", "widgets", params={"limit": 5})

        # 2. Stream — memory-bounded
        async for row in client.paginate("public", "events",
                                         params={"order": "ts.asc"},
                                         page_size=1000):
            process(row)

        # 3. Fan-out — 50 lookups in parallel, capped at max_concurrency
        results = await asyncio.gather(*[
            client.get("public", "clients", params={"id": f"eq.{i}"})
            for i in range(50)
        ])

        # 4. Handle typed errors
        try:
            await client.get("public", "missing_table")
        except LakebaseDataApiError as e:
            if e.code == "PGRST205":
                print("table or schema not exposed")

asyncio.run(main())
```

In a Databricks notebook, swap `asyncio.run(...)` for `await main()` inside a cell — notebooks run their own event loop.

### Notes on token refresh

- `oauth_token` mode uses the supplied static token for every request; rebuild the client after ~1h when it expires.
- `user_oauth` / `sp_oauth` modes call `WorkspaceClient.config.authenticate()` in a worker thread (`asyncio.to_thread`) so the event loop isn't blocked, and cache the bearer header for 10 minutes. The SDK refreshes its underlying credential silently inside that call.

## Troubleshooting

### `PGRST301 invalid token permissions` (401)

Token reached PostgREST but the identity has no Postgres role. Run `src/provision_data_api_role.sql` — see `docs/fix_data_api_auth.md`.

### `42501 permission denied to set role "<uuid>"` (HTTP 403)

PostgREST got your token but `authenticator` isn't granted the identity's
role. Run the one-liner:

```sql
GRANT "<identity>" TO authenticator;
```

Re-running `src/provision_data_api_role.sql` is idempotent and covers this.

### `ERROR: permission denied to grant role "<identity>" (SQLSTATE 42501)` — inside the SQL Editor

Hit while running the GRANT above. Means the current SQL session doesn't hold
ADMIN OPTION on the target role. Almost always caused by provisioning the
identity via the Lakebase UI's **Roles & Databases → Add Role** flow: UI-
created roles don't grant the project owner ADMIN, so the follow-up GRANT
fails. Fix: drop the role in the UI and recreate it via
`databricks_create_role(...)` in SQL — that function additionally grants the
caller ADMIN, making the GRANT succeed. See `docs/fix_data_api_auth.md`.

### `PGRST205 Could not find the table 'public.foo' in the schema cache`

Table doesn't exist *or* the schema cache is stale after you added it. Click **Refresh schema cache** on the Data API page, or re-enable the Data API toggle.

### `401 Unauthorized` with no PGRST code

You're probably not authenticated to Databricks at all. Check that one of the auth sources (explicit token, env var, SDK ambient/profile) actually resolves.

## Related files

- `src/lakebase_utils/lakebase_api.py` — the sync client.
- `src/lakebase_utils/lakebase_api_async.py` — the async client, `LakebaseDataApiError`, `_TokenBucket`.
- `src/lakebase_utils/_common.py` — shared URL/auth helpers used by both clients.
- `src/test_lakebase_api.py` — runnable demo of the sync client.
- `tests/unit/` — pytest suite (URL/auth, sync regression, async client, token bucket, retry loop).
- `tests/integration/test_lakebase_api_async_live.py` — live-endpoint smoke, gated on `LAKEBASE_API_URL`.
- `src/provision_data_api_role.sql` — template for provisioning Data API identities.
- `docs/fix_data_api_auth.md` — full auth-failure playbook.
- `docs/lakebase_connect.md` — direct-Postgres client (different surface).
