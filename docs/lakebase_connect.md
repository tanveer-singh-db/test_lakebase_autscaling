# `LakebaseAutoscalingClient`

A thin client over `psycopg2` for Databricks Lakebase Autoscaling Postgres. Picks an auth mode, manages a pooled connection, refreshes OAuth tokens automatically, and exposes three query methods: `execute` (DDL/DML), `select` (Spark DataFrame), `fetch` (raw tuples).

Source: `src/lakebase_utils/lakebase_connect.py`.

## Install

DBR 16.4 LTS and Serverless ship `psycopg2` 2.9.3 and `databricks-sdk` preinstalled — no `%pip install` needed.

Locally:

```bash
pip install psycopg2-binary databricks-sdk
```

If you want `select()` to return a Spark DataFrame outside a Databricks notebook you'll also need `pyspark` and a JDK — usually not worth the trouble locally. Use `fetch()` for Python-side development.

## Quick start

```python
from lakebase_utils.lakebase_connect import LakebaseAutoscalingClient

with LakebaseAutoscalingClient(
    host="ep-xxxx.database.eastus2.azuredatabricks.net",
    database="databricks_postgres",
    auth_mode="static",
    user="authenticator",
    password="…",
) as client:
    print(client.fetch("SELECT current_user, now()"))
```

## Connection strings

Any of the four auth modes can pull connection details from a standard Postgres URL via the `connection_string` kwarg. Everything the URL carries (`host`, `port`, `database`, `user`, and — for static mode — `password`) is extracted; explicit kwargs on the constructor win over URL-parsed values.

```python
URL = "postgresql://me%40example.com@ep-xxxx.database.eastus2.azuredatabricks.net/databricks_postgres?sslmode=require"

# user_oauth — URL supplies host/database/user; you still pass endpoint_path
with LakebaseAutoscalingClient(
    auth_mode="user_oauth",
    connection_string=URL,
    endpoint_path="projects/my-proj/branches/production/endpoints/primary",
) as client:
    print(client.fetch("SELECT current_user"))

# static — include password in the URL
STATIC_URL = "postgresql://authenticator:SECRET@ep-xxxx.database.eastus2.azuredatabricks.net/databricks_postgres?sslmode=require"
with LakebaseAutoscalingClient(auth_mode="static", connection_string=STATIC_URL) as client:
    ...

# oauth_token — URL provides the user; token comes in separately
with LakebaseAutoscalingClient(
    auth_mode="oauth_token",
    connection_string=URL,
    token=os.environ["LAKEBASE_TOKEN"],
) as client:
    ...
```

URL-encode special characters in the user (e.g. `@` → `%40`). URLs that omit the password are fine for OAuth modes — the token / credential is applied at connect time.

## Auth modes

Pass one of four via `auth_mode`. Each has its own required fields:

| `auth_mode`   | Required                                       | Use when…                                                                 |
|---------------|------------------------------------------------|---------------------------------------------------------------------------|
| `static`      | `user`, `password`                             | Plain Postgres role with a password (e.g. `authenticator`). Long-lived.   |
| `oauth_token` | `oauth_user`, `token`                          | You already minted a token (CLI/UI) and want to hand it in as a string.   |
| `user_oauth`  | `oauth_user`, `endpoint_path`                  | You want the SDK to mint + auto-refresh tokens using your user identity.  |
| `sp_oauth`    | `client_id`, `client_secret`, `endpoint_path`  | Machine-to-machine: service principal mints + refreshes tokens.           |

`endpoint_path` looks like `projects/<project>/branches/<branch>/endpoints/<endpoint>` and comes from the Lakebase **Connect** dialog.

For `user_oauth` / `sp_oauth`, the client builds a `WorkspaceClient()` with ambient auth by default (notebook runtime, env vars, `~/.databrickscfg`). Only pass `workspace_host` / `profile` if you need to override that — e.g. to pin a specific CLI profile locally.

### `static`

```python
LakebaseAutoscalingClient(
    host="ep-xxxx.database.eastus2.azuredatabricks.net",
    database="databricks_postgres",
    auth_mode="static",
    user="authenticator",
    password="…",
)
```

### `oauth_token`

Mint a token elsewhere, pass it in. No SDK involvement, no refresh — the token is valid for its ~1h TTL, then rebuild the client.

```bash
TOKEN=$(databricks postgres generate-database-credential \
  projects/my-proj/branches/production/endpoints/primary \
  -p my-profile -o json | jq -r '.token')
```

```python
LakebaseAutoscalingClient(
    host="ep-xxxx.database.eastus2.azuredatabricks.net",
    database="databricks_postgres",
    auth_mode="oauth_token",
    oauth_user="me@example.com",  # MUST match the identity that minted the token
    token=os.environ["TOKEN"],
)
```

### `user_oauth`

```python
LakebaseAutoscalingClient(
    host="ep-xxxx.database.eastus2.azuredatabricks.net",
    database="databricks_postgres",
    auth_mode="user_oauth",
    oauth_user="me@example.com",
    endpoint_path="projects/my-proj/branches/production/endpoints/primary",
    # workspace_host="https://adb-….azuredatabricks.net",  # optional
    # profile="my-profile",                                 # optional
)
```

In a Databricks notebook drop both overrides — ambient auth picks you up automatically.

### `sp_oauth`

```python
LakebaseAutoscalingClient(
    host="ep-xxxx.database.eastus2.azuredatabricks.net",
    database="databricks_postgres",
    auth_mode="sp_oauth",
    client_id="<sp-client-id>",
    client_secret=os.environ["DATABRICKS_CLIENT_SECRET"],
    endpoint_path="projects/my-proj/branches/production/endpoints/primary",
    # workspace_host="https://adb-….azuredatabricks.net",  # optional in notebooks
)
```

The Postgres role for SP auth is the `client_id` UUID, not the SP's display name — make sure the SP has a corresponding Postgres role (`databricks_create_role(<client_id>, 'SERVICE_PRINCIPAL')`) before using this mode.

## API

```python
client.execute(sql)           # -> None
client.fetch(sql)             # -> list[tuple]
client.select(sql, spark=None)  # -> pyspark.sql.DataFrame
client.close()                # idempotent
```

All three execute on a pooled connection and release it back on exit.

### `execute(sql)`

Runs DDL/DML in a single transaction. Accepts multi-statement scripts — the body is split on `;` respecting quotes and comments, and each statement runs sequentially. Any failure rolls the whole batch back.

```python
client.execute(open("src/provision_data_api_role.sql").read())
```

Dollar-quoted strings (`$$ … $$`) are **not** supported by the splitter.

### `fetch(sql)`

One SELECT, returns `list[tuple]`. No Spark dependency — use this for local dev, tests, or anywhere `spark` isn't available.

```python
rows = client.fetch("SELECT table_name FROM information_schema.tables LIMIT 5")
```

### `select(sql, spark=None)`

One SELECT, returns a Spark DataFrame built via `spark.createDataFrame(rows, schema=cols)`. If `spark` is `None`, the client calls `SparkSession.builder.getOrCreate()`.

```python
df = client.select("SELECT * FROM public.widgets LIMIT 20")
df.display()  # Databricks notebook
# df.show()   # pyspark shell
```

### Context manager

```python
with LakebaseAutoscalingClient(...) as client:
    client.execute("…")
# pool closed automatically
```

## Connection pool and token refresh

- Pool: `psycopg2.pool.ThreadedConnectionPool` (`minconn=1`, `maxconn=5` by default). Override via `minconn`/`maxconn` kwargs.
- OAuth modes (`user_oauth`, `sp_oauth`) use a subclass that overrides `_connect` to stamp a fresh token on every new physical connection. The token is cached at the client level and only re-minted when within 5 minutes of `expire_time`.
- Idle pooled connections reuse their original token until psycopg2 or the server drops them; replacements get a fresh token automatically.
- `oauth_token` mode does **not** refresh — the static token you handed in is used as the password for every connection.

Long-running jobs (> ~55 min) should prefer `user_oauth` / `sp_oauth` over `oauth_token` so the SDK keeps minting new tokens as needed. Even so, Lakebase closes idle connections at 24h and caps any connection at 3 days — design your app to handle reconnects.

## Troubleshooting

### `password authentication failed for user '<email or UUID>'`

The Postgres user in your connection didn't match (or isn't provisioned as) a Postgres role on the endpoint.

- For OAuth modes, `oauth_user` / `client_id` must be the same identity that mints the token. A mismatch here (e.g. URL says `me@example.com`, SDK is authenticated as a service principal) always fails.
- Even if the identity matches, the Postgres role must exist. Non-owner identities need `databricks_create_role(<identity>, 'USER' | 'SERVICE_PRINCIPAL')` + a `GRANT` to `authenticator` (or directly on the schema/tables). See `src/provision_data_api_role.sql` for a template.
- **The project owner cannot authenticate via OAuth** — even though the owner role is auto-created, `authenticator` can't assume elevated roles, so owner tokens are rejected with `password authentication failed`. Use a non-owner user or a service principal for app/script connections; keep the owner identity for DDL in the SQL Editor.

### `ValueError: auth_mode='…' requires: …`

Self-explanatory — the error message names the fields you need to add.

### `ModuleNotFoundError: No module named 'databricks'`

You picked `user_oauth` or `sp_oauth` but the SDK isn't installed. `pip install databricks-sdk`, or switch to `static` / `oauth_token` which don't need the SDK.

### `pool is exhausted` under load

Bump `maxconn` at construction. The pool is thread-safe (`ThreadedConnectionPool`) and holds connections until returned.

## Samples

### 1. Read a table into a Spark DataFrame (notebook)

```python
from lakebase_utils.lakebase_connect import LakebaseAutoscalingClient

client = LakebaseAutoscalingClient(
    auth_mode="user_oauth",
    connection_string="postgresql://me%40example.com@ep-xxxx.database.eastus2.azuredatabricks.net/databricks_postgres?sslmode=require",
    endpoint_path="projects/my-proj/branches/production/endpoints/primary",
)

df = client.select(
    """
    SELECT table_schema, table_name, table_type
    FROM information_schema.tables
    WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
    ORDER BY table_schema, table_name
    """,
    spark=spark,
)
df.display()
```

`spark` defaults to `SparkSession.builder.getOrCreate()` when omitted, which works both in a Databricks notebook (picks up the bound `spark` global) and any PySpark environment.

### 2. Local dev without Spark

```python
# fetch() returns list[tuple] — good for unit tests, REPL, scripts
with LakebaseAutoscalingClient(
    auth_mode="static",
    connection_string="postgresql://authenticator:$PW@ep-xxxx.database.eastus2.azuredatabricks.net/databricks_postgres?sslmode=require",
) as client:
    for row in client.fetch("SELECT id, name FROM public.widgets LIMIT 10"):
        print(row)
```

### 3. Run a provisioning SQL script (multi-statement, transactional)

```python
with LakebaseAutoscalingClient(
    auth_mode="user_oauth",
    connection_string=OWNER_URL,   # owner has DDL; authenticator doesn't
    endpoint_path=ENDPOINT_PATH,
) as client:
    with open("src/provision_data_api_role.sql") as f:
        client.execute(f.read())
```

`execute()` splits on `;`, respects quotes/comments, and rolls the whole batch back on any failure.

### 4. Service-principal automation (CI / scheduled job)

```python
import os
from lakebase_utils.lakebase_connect import LakebaseAutoscalingClient

with LakebaseAutoscalingClient(
    host="ep-xxxx.database.eastus2.azuredatabricks.net",
    database="databricks_postgres",
    auth_mode="sp_oauth",
    client_id=os.environ["DATABRICKS_CLIENT_ID"],
    client_secret=os.environ["DATABRICKS_CLIENT_SECRET"],
    endpoint_path=os.environ["LAKEBASE_ENDPOINT_PATH"],
    # workspace_host is optional when ambient auth resolves
    workspace_host=os.environ.get("DATABRICKS_HOST"),
) as client:
    client.execute("INSERT INTO public.events (kind, payload) VALUES ('run', '{}')")
```

SP OAuth tokens are minted and refreshed automatically via the SDK, so a job that runs longer than an hour keeps working without extra plumbing.

### 5. Pre-minted token (short-lived script)

```bash
export LAKEBASE_TOKEN=$(databricks postgres generate-database-credential \
    projects/my-proj/branches/production/endpoints/primary \
    -p my-profile -o json | jq -r '.token')
```

```python
with LakebaseAutoscalingClient(
    auth_mode="oauth_token",
    connection_string="postgresql://me%40example.com@ep-xxxx.database.eastus2.azuredatabricks.net/databricks_postgres?sslmode=require",
    token=os.environ["LAKEBASE_TOKEN"],
) as client:
    print(client.fetch("SELECT current_user, now()"))
```

Token is valid for ~1 hour. For longer-running work use `user_oauth` or `sp_oauth` instead.

### 6. Higher-concurrency pool

```python
client = LakebaseAutoscalingClient(
    ...,
    minconn=2,   # warmed up on construction
    maxconn=20,  # upper bound under load
)
```

The pool is `psycopg2.pool.ThreadedConnectionPool` — thread-safe, grows on demand, and for OAuth modes mints a fresh token for each new physical connection.

## Related files

- `src/lakebase_utils/lakebase_connect.py` — the module.
- `src/lakebase_utils/lakebase_api.py` — separate HTTP client for the Lakebase Data API (PostgREST). Different auth story; see `docs/fix_data_api_auth.md`.
- `src/create_widgets.sql` — sample DDL used by `execute` examples.
- `src/provision_data_api_role.sql` — template for provisioning Data API identities.
- `src/test_user_oauth.py` — runnable smoke test for `user_oauth` mode.
- `src/test_oauth_user.py` — runnable smoke test for `oauth_token` mode.
