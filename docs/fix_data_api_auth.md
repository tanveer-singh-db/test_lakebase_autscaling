# Fix: Lakebase Data API returns `PGRST301 invalid token permissions`

## What the error means

Your Databricks OAuth token reached PostgREST fine, but the authenticated
identity has no corresponding **Postgres role** mapped via the
`databricks_auth` extension. Until that mapping exists, `authenticator`
has nothing to switch into, so every request 401s.

Rule from the Lakebase docs: the project **owner** can never call the Data
API because `authenticator` isn't allowed to assume an elevated role. So
provision a different, non-owner user or service principal.

## Fix steps

### 1. Enable the Data API on the project

In the Databricks UI:

1. Open your Lakebase project.
2. Go to **Data API** (under **App Backend**).
3. Click **Enable Data API** if it isn't already on.

This creates the `authenticator` role and the `pgrst` schema if missing.

### 2. Provision the identity as a Postgres role — **use SQL, not the UI**

> ⚠️ The Lakebase UI's **Roles & Databases → Add Role → OAuth** flow and the
> `databricks_create_role()` SQL function look equivalent in the docs, but
> they are **not** for the `authenticator` delegation step. Roles created
> via the UI do not grant the project owner ADMIN OPTION, so the follow-up
> `GRANT "<identity>" TO authenticator` fails with SQLSTATE `42501`:
>
> ```
> ERROR: permission denied to grant role "<identity>" (SQLSTATE 42501)
> ```
>
> `databricks_create_role()` additionally grants the caller ADMIN on the
> new role, which is what makes the GRANT possible. **Always provision via
> SQL for Data API roles.** If a role was already added via the UI and
> you're hitting 42501, drop it in the UI and recreate it with the SQL
> template below.

Open the **Lakebase SQL Editor** (authenticated as the owner, which has DDL
rights) and run the template at `src/provision_data_api_role.sql`. Before
running, replace:

- `<IDENTITY>` — the user's email (e.g. `alice@example.com`) **or** the
  service principal's application id (UUID).
- `<IDENTITY_TYPE>` — either `'USER'` or `'SERVICE_PRINCIPAL'`.

The script:

```sql
CREATE EXTENSION IF NOT EXISTS databricks_auth;

SELECT databricks_create_role(
    '<IDENTITY>',
    '<IDENTITY_TYPE>'
);

GRANT "<IDENTITY>" TO authenticator;

GRANT USAGE ON SCHEMA public TO "<IDENTITY>";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "<IDENTITY>";

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "<IDENTITY>";
```

### 3. Create a real table to query (optional)

If `public` is empty, the Data API has nothing interesting to return. Run
`src/create_widgets.sql` from the SQL Editor for a sample table with grants.

### 4. Re-test the Data API

```bash
export LAKEBASE_API_URL="https://<lakebase-host>/api/2.0/workspace/<workspace-id>/rest/<database>"
.venv/bin/python src/test_lakebase_api.py
```

`src/lakebase_utils/lakebase_api.py` pulls the workspace OAuth token from the Databricks SDK
(ambient auth by default, `DATABRICKS_CONFIG_PROFILE=<name>` to pin a profile).
Expected output is a JSON list of rows.

## Verifying each layer independently

- **Data API is enabled** → `GET /` on the base URL returns `PGRST205`
  ("Could not find the table"), meaning PostgREST is serving requests.
- **Token reaches PostgREST** → 401 body contains `PGRST301`, not a raw
  Databricks auth error.
- **Role mapping worked** → same request now returns 200 JSON.

## Identity selection notes

- **Don't use the project owner.** The owner has elevated privileges and
  `authenticator` can't assume an owner role.
- **User identity** → `databricks_create_role('<email>', 'USER')`. The
  Postgres role name is the email string. Use it verbatim (with quoted
  identifier syntax if it contains `@` or `.`).
- **Service principal** → `databricks_create_role('<application-id-uuid>',
  'SERVICE_PRINCIPAL')`. The Postgres role name is the UUID, not the SP's
  display name.
- Either way, the identity also needs to be a member of the workspace that
  owns the Lakebase project — OAuth tokens are workspace-scoped.
