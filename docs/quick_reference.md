# Quick Reference — Lakebase access

Task-oriented lookup for this repo plus answers to the questions that actually come up. For the why-it-works narrative, read [`docs/data_access_patterns.md`](data_access_patterns.md) instead.

## Where to go for…

| I want to…                                              | Go here                                                                                                                                                               |
|---------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Query Lakebase over REST (curl / sync / async)          | [`docs/lakebase_api.md`](lakebase_api.md) · [`data_access_patterns.md §5.1`](data_access_patterns.md#51-connecting-via-the-data-api)                                    |
| Query Lakebase directly as Postgres                     | [`docs/lakebase_connect.md`](lakebase_connect.md) · [`data_access_patterns.md §5.3`](data_access_patterns.md#53-external-apps-connecting-directly-to-postgres)         |
| Network diagrams (on-prem vs Azure Private Link)         | [`data_access_patterns.md §5.2`](data_access_patterns.md#52-network-patterns--external-apps--data-api)                                                                 |
| Provision a user / SP as a Postgres role                | [`src/provision_data_api_role.sql`](../src/provision_data_api_role.sql) · [`data_access_patterns.md §4 Step B`](data_access_patterns.md#step-b--postgres-role--grants-data-layer) |
| Fix `PGRST301` / `42501` from the Data API              | [`docs/fix_data_api_auth.md`](fix_data_api_auth.md)                                                                                                                     |
| Create a Lakebase project via SDK                        | [`data_access_patterns.md §1`](data_access_patterns.md#1-create-a-lakebase-project-sdk)                                                                                |
| Sync a UC table into Lakebase                            | [`data_access_patterns.md §2`](data_access_patterns.md#2-sync-lakehouse-tables-into-lakebase-standardized-path)                                                        |
| Resize a compute / change autoscaling                    | [`data_access_patterns.md §3`](data_access_patterns.md#3-lakebase-autoscaling--computes--autoscaling-summary)                                                          |
| Understand why nothing in `authenticator` works          | [Two-layer auth model diagram](data_access_patterns.md#authentication-model-at-a-glance)                                                                                |

## External docs you'll click most

- [Lakebase Data API](https://learn.microsoft.com/en-gb/azure/databricks/oltp/projects/data-api)
- [Connect to your database](https://learn.microsoft.com/en-gb/azure/databricks/oltp/projects/connect)
- [Authentication](https://learn.microsoft.com/en-gb/azure/databricks/oltp/projects/authentication)
- [Manage database permissions](https://learn.microsoft.com/en-gb/azure/databricks/oltp/projects/manage-roles-permissions)
- [OAuth user-to-machine (U2M)](https://learn.microsoft.com/en-gb/azure/databricks/dev-tools/auth/oauth-u2m) · [OAuth machine-to-machine (M2M)](https://learn.microsoft.com/en-gb/azure/databricks/dev-tools/auth/oauth-m2m)
- [Databricks SDK for Python](https://databricks-sdk-py.readthedocs.io/en/latest/)

---

## FAQ

### Provisioning & access

**Why does `GRANT "<identity>" TO authenticator` fail with `permission denied to grant role` (42501)?**
The role was almost certainly created via the Lakebase UI's **Roles & Databases → Add Role** flow — that path doesn't grant the project owner `ADMIN OPTION`, so the follow-up GRANT is refused. Drop the role in the UI and recreate it with `databricks_create_role('<identity>', 'USER'|'SERVICE_PRINCIPAL')` from the SQL Editor; `databricks_create_role` additionally grants the caller ADMIN. Full diagnosis: [`docs/fix_data_api_auth.md`](fix_data_api_auth.md).

**Why can't I use the project owner for the Data API?**
`authenticator` isn't allowed to assume an elevated role, and owners inherit `databricks_superuser` privileges. Use a non-owner user or service principal for all app-level access. The owner keeps DDL rights in the SQL Editor — that's where you run provisioning scripts. See [Authentication (Azure Databricks docs)](https://learn.microsoft.com/en-gb/azure/databricks/oltp/projects/authentication#requirements-and-limitations).

**How do I mint a Lakebase OAuth token for a specific identity?**
Either from CLI (one-off):
```bash
databricks postgres generate-database-credential \
  projects/<project>/branches/<branch>/endpoints/<endpoint> \
  -p <profile> -o json | jq -r '.token'
```
Or from Python (auto-refreshing):
```python
from databricks.sdk import WorkspaceClient
cred = WorkspaceClient(profile="<profile>").postgres.generate_database_credential(
    endpoint="projects/<project>/branches/<branch>/endpoints/<endpoint>"
)
print(cred.token, cred.expire_time)
```
The JWT subject (`sub` claim) must match the Postgres username you're connecting as. Decode with:
```bash
python -c "import base64,json,sys; p=sys.stdin.read().split('.')[1]; p+='='*((4-len(p)%4)%4); print(json.loads(base64.urlsafe_b64decode(p))['sub'])" <<<"$LAKEBASE_API_TOKEN"
```

**How do I expose a schema beyond `public` to the Data API?**
Lakebase UI → project → **Data API → Settings → Exposed schemas**, add the schema name, click **Save**, then **Refresh schema cache**. Without this step queries against `other_schema.*` return `PGRST205 Could not find the table`. Grants on the schema/tables still have to be in place separately.

**Why doesn't my new schema show up in `information_schema.tables`?**
`information_schema` is privilege-filtered — it only surfaces rows the current Postgres role has some privilege on. Use `pg_catalog.pg_tables` to list everything regardless of grants, or GRANT `USAGE` on the schema + `SELECT` on its tables and re-run `information_schema.tables`. Unfiltered query:
```sql
SELECT schemaname, tablename FROM pg_catalog.pg_tables
WHERE schemaname NOT IN ('pg_catalog','information_schema')
ORDER BY 1, 2;
```

**How do I create a sample table to test against?**
Run [`src/create_widgets.sql`](../src/create_widgets.sql) from the SQL Editor as the project owner — it creates `public.widgets` with three rows plus the minimum grants for `authenticator`/`api_user`. Useful for validating the end-to-end path before pointing an app at real data.

### Testing the Data API

**How do I test the Data API from a Databricks notebook?**
1. `%pip install requests` (and `aiohttp` if you want the async client).
2. Paste the **Data API → API URL** into a variable — no CLI profile needed, the notebook has ambient auth.
3. Use the sync client:
   ```python
   from lakebase_utils.lakebase_api import LakebaseDataApiClient
   with LakebaseDataApiClient(
       base_url="https://ep-xxxx.../api/2.0/workspace/<ws>/rest/<db>",
       # no auth kwargs — WorkspaceClient() picks up notebook auth automatically
   ) as c:
       display(c.fetch_all("public", "widgets"))
   ```
4. Or skip the wrapper and mint a token inline:
   ```python
   from databricks.sdk import WorkspaceClient
   import requests
   tok = WorkspaceClient().config.authenticate()["Authorization"].split()[1]
   r = requests.get(f"{BASE}/public/widgets", headers={"Authorization": f"Bearer {tok}"})
   ```
See [`docs/lakebase_api.md §Ambient auth`](lakebase_api.md#1-ambient-auth-in-a-databricks-notebook--url-built-from-pieces).

**How do I test the Data API from outside Databricks?**
1. Install the clients: `pip install requests aiohttp databricks-sdk`.
2. Authenticate to the workspace first. Pick one:
   - User OAuth: `databricks auth login --host https://<workspace>.azuredatabricks.net --profile my-user` and `export DATABRICKS_CONFIG_PROFILE=my-user`.
   - Service principal via env vars: `export DATABRICKS_HOST=... DATABRICKS_CLIENT_ID=... DATABRICKS_CLIENT_SECRET=...`.
3. `export LAKEBASE_API_URL="https://ep-xxxx.../api/2.0/workspace/<ws>/rest/<db>"`.
4. Run the smoke test:
   ```bash
   python src/test_lakebase_api.py
   ```
5. For ad-hoc curl, mint a token first:
   ```bash
   export LAKEBASE_API_TOKEN=$(databricks postgres generate-database-credential \
       projects/<proj>/branches/<branch>/endpoints/<endpoint> -p my-user -o json | jq -r .token)
   curl -H "Authorization: Bearer $LAKEBASE_API_TOKEN" "$LAKEBASE_API_URL/public/widgets?limit=5"
   ```
Full walk-through in [`docs/lakebase_api.md §5 Raw HTTP with curl`](lakebase_api.md#5-raw-http-with-curl).

### Testing the Postgres client (`lakebase_connect`)

**How do I test `LakebaseAutoscalingClient` from a Databricks notebook?**
1. `%pip install psycopg2-binary` (already there on DBR 16.4 LTS; needed elsewhere).
2. Construct the client with `user_oauth` — ambient auth mints tokens and refreshes them on you:
   ```python
   from lakebase_utils.lakebase_connect import LakebaseAutoscalingClient
   client = LakebaseAutoscalingClient(
       host="ep-xxxx.database.<region>.azuredatabricks.net",
       database="databricks_postgres",
       auth_mode="user_oauth",
       oauth_user="me@example.com",                              # must match notebook identity
       endpoint_path="projects/<proj>/branches/<branch>/endpoints/<endpoint>",
       # workspace_host / profile can be omitted — SDK is ambient
   )
   display(client.select("SELECT * FROM public.widgets LIMIT 20", spark=spark))
   ```
3. For a Spark DataFrame → `client.select(sql, spark=spark)`. For plain Python rows → `client.fetch(sql)`. For DDL/DML → `client.execute(sql)`. See [`docs/lakebase_connect.md`](lakebase_connect.md).

**How do I test `LakebaseAutoscalingClient` from outside Databricks?**
1. Install deps: `pip install psycopg2-binary databricks-sdk pyspark` (`pyspark` only if you need `.select()`; `.fetch()` returns plain Python tuples).
2. Make sure `databricks-sdk` can authenticate — same three options as above (CLI profile, env vars, or static token):
   - `databricks auth login --host <workspace-url> --profile my-user` + `export DATABRICKS_CONFIG_PROFILE=my-user`, OR
   - set `DATABRICKS_HOST` / `DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET`.
3. Verify the identity has a provisioned Postgres role (run [`src/provision_data_api_role.sql`](../src/provision_data_api_role.sql) once if not).
4. Smoke-test script:
   ```bash
   python src/test_oauth_user.py
   ```
5. Or construct a client with explicit kwargs and call `client.fetch("SELECT current_user, now()")` — the returned row tells you both that auth worked and which identity you're authenticated as. For the token-identity sanity check (JWT `sub` matches your username), use the decode snippet under *"How do I mint a Lakebase OAuth token"* above.

### Connection shape

**Data API vs direct Postgres — how do I choose?**
See the decision table in [`data_access_patterns.md §5.3`](data_access_patterns.md#53-external-apps-connecting-directly-to-postgres). Short version: browser/mobile/no-5432 → Data API. Complex SQL, transactions, existing JDBC code → direct Postgres. Both surfaces share the same identity model.

**What's the difference between user OAuth, SP OAuth, and `oauth_token`?**
They all produce a Databricks OAuth bearer — the difference is *who mints it* and *how long it lives*. User OAuth = interactive developer flow, auto-refreshed by SDK. SP OAuth = `client_id` + `client_secret` for headless/CI, auto-refreshed by SDK. `oauth_token` = you minted it yourself (CLI or via the SDK) and hand it in as a string — no refresh, rebuild the client after ~1h. See [OAuth U2M](https://learn.microsoft.com/en-gb/azure/databricks/dev-tools/auth/oauth-u2m) / [OAuth M2M](https://learn.microsoft.com/en-gb/azure/databricks/dev-tools/auth/oauth-m2m).

**How do I run the network patterns on AWS / GCP / non-Azure clouds?**
The Data API is just HTTPS to `*.azuredatabricks.net` (or `*.cloud.databricks.com` on AWS, `*.gcp.databricks.com` on GCP). Pattern A ("public internet over 443") works from anywhere that can egress to the workspace domain. Pattern B ("private networking") needs a cloud-native equivalent — AWS PrivateLink, GCP Private Service Connect, Azure Private Endpoint. Whichever you pick, the identity/auth story is unchanged.
